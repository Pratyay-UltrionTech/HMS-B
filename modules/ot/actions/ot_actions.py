"""
Business use-case actions for Operation Theatre (OT) domain.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError as _OTIntegrityError
from sqlalchemy.orm import Session

from modules.inpatient.utils.admission_conflicts import (
    admission_conflict as _ot_conflict,
    bed_conflict as _ot_bed_conflict,
)

from modules.billing.entities.billing_entities import BillingSourceType
from modules.billing.services.billing_service import (
    cancel_charge_for_source,
    ensure_charge,
)
from modules.ot.contracts.ot_contracts import (
    OtCalendarEntry,
    OtCompleteRequest,
    OtDashboardResponse,
    OtNotesRequest,
    OtRescheduleRequest,
    OtSurgeryCreate,
    OtSurgeryResponse,
    OtSurgeryUpdate,
)
from modules.ot.db.ot_repository import OtRepository
from modules.ot.entities.ot_entities import (
    OtRoom,
    OtSurgery,
    OtSurgeryStatus,
)
from modules.ot.services.ot_service import (
    room_label,
    surgery_end,
    surgery_to_response,
    sync_ot_surgery_medical_record,
    sync_time_based_status,
)
from shared.audit import write_audit_log


def _actor_name(user: dict[str, Any]) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _actor_role(user: dict[str, Any]) -> str:
    return str(user.get("staff_role_name") or user.get("role") or "")


class ListCalendarAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = OtRepository(db, hospital_id)

    def execute(
        self, date_from: date, date_to: date, ot_room_id: UUID | None = None
    ) -> list[OtCalendarEntry]:
        if date_to < date_from:
            raise HTTPException(status_code=400, detail="date_to must be on or after date_from")

        start = datetime.combine(date_from, time.min).replace(tzinfo=timezone.utc)
        end = datetime.combine(date_to, time.max).replace(tzinfo=timezone.utc)

        rows = self.repo.list_calendar_surgeries(end=end, start=start, ot_room_id=ot_room_id)
        out: list[OtCalendarEntry] = []
        for item in rows:
            ends = surgery_end(item)
            if ends < start:
                continue
            room = item.ot_room_ref
            if not item.ot_room_id and not room:
                continue
            label = room_label(room) if room else (item.ot_room or "OT")
            out.append(
                OtCalendarEntry(
                    id=item.id,
                    ot_room_id=item.ot_room_id or room.id,
                    ot_room_label=label,
                    scheduled_at=item.scheduled_at,
                    ends_at=ends,
                    surgery_type=item.surgery_type,
                    status=item.status,
                    patient_name=item.patient.name if item.patient else None,
                    surgeon_name=item.surgeon.name if item.surgeon else None,
                )
            )
        return out


class GetDashboardAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = OtRepository(db, hospital_id)

    def execute(self) -> OtDashboardResponse:
        counts = self.repo.get_dashboard_metrics()
        return OtDashboardResponse(
            todays_surgeries=counts["todays_surgeries"],
            completed=counts["completed"],
            ongoing=counts["ongoing"],
            scheduled=counts["scheduled"],
            cancelled=counts["cancelled"],
        )


class ListSurgeriesAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = OtRepository(db, hospital_id)

    def execute(
        self,
        status_filter: str | None = None,
        patient_id: UUID | None = None,
        search: str | None = None,
        schedule_only: bool | None = None,
        ongoing_only: bool | None = None,
        history_only: bool | None = None,
        notes_pending: bool | None = None,
    ) -> list[OtSurgeryResponse]:
        status_enum = None
        if status_filter:
            try:
                status_enum = OtSurgeryStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")

        rows = self.repo.list_surgeries(
            status_filter=status_enum,
            patient_id=patient_id,
            search=search,
            schedule_only=schedule_only,
            ongoing_only=ongoing_only,
            history_only=history_only,
            notes_pending=notes_pending,
        )

        # sync_time_based_status only returns True when it actually moved a
        # row from scheduled/confirmed into in_progress. Committing here is a
        # write side-effect on what is otherwise a read endpoint — skip it
        # entirely when nothing changed, which is the common case. This also
        # avoids expiring the eager-loaded patient/surgeon/department/room
        # relations on every row (expire_on_commit=True on this sync session),
        # which previously forced a fresh SELECT per relation per row in
        # surgery_to_response() below even when no status transition occurred.
        now = datetime.now(timezone.utc)
        changed = False
        for r in rows:
            if sync_time_based_status(r, now):
                changed = True
        if changed:
            self.db.commit()

        return [surgery_to_response(r) for r in rows]


class GetSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        return surgery_to_response(item)


class CreateSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, payload: OtSurgeryCreate) -> OtSurgeryResponse:
        patient = self.repo.get_patient(payload.patient_id)
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        if not self.repo.check_active_admission(payload.patient_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transfer the patient to IPD and admit them before booking OT",
            )

        if not self.repo.check_prescription_exists(payload.patient_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Write a prescription for this patient before booking OT",
            )

        if payload.surgeon_id:
            surgeon = self.repo.get_surgeon(payload.surgeon_id)
            if not surgeon:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgeon not found")

        dept = self.repo.get_department(payload.department_id)
        if not dept:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

        ot_room = self.repo.resolve_ot_room(payload.ot_room_id, payload.department_id, for_update=True)
        if not ot_room:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selected OT room is invalid or does not belong to the selected department",
            )

        start = payload.scheduled_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = start + timedelta(minutes=int(payload.duration_minutes or 60))

        if self.repo.check_ot_room_conflict(ot_room.id, start, end):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="OT room is already booked for this time range",
            )

        surgery_no = self.repo.next_surgery_no()
        actor_name = _actor_name(self.user)
        actor_role = _actor_role(self.user)

        admission_id = payload.admission_id
        if admission_id:
            from modules.inpatient.entities.admission import Admission
            admission = (
                self.db.query(Admission)
                .filter(
                    Admission.id == admission_id,
                    Admission.hospital_id == self.hospital_id,
                )
                .first()
            )
            if not admission:
                raise HTTPException(status_code=404, detail="Admission not found")
            if admission.patient_id != payload.patient_id:
                raise HTTPException(status_code=400, detail="Admission does not belong to the selected patient")

        item = OtSurgery(
            hospital_id=self.hospital_id,
            surgery_no=surgery_no,
            patient_id=payload.patient_id,
            admission_id=admission_id,
            surgeon_id=payload.surgeon_id,
            assistant_surgeon=payload.assistant_surgeon.strip() if payload.assistant_surgeon else None,
            surgery_type=payload.surgery_type.strip(),
            surgery_category=payload.surgery_category.strip() or "General",
            priority=payload.priority,
            department_id=payload.department_id,
            ot_room_id=ot_room.id,
            ot_room=room_label(ot_room),
            ot_charge_amount=float(getattr(ot_room, "base_ot_charge", 0) or 0),
            scheduled_at=payload.scheduled_at,
            duration_minutes=payload.duration_minutes,
            anaesthetist=payload.anaesthetist.strip() if payload.anaesthetist else None,
            remarks=payload.remarks.strip() if payload.remarks else None,
            booked_by_name=actor_name,
            booked_by_role=actor_role,
            status=OtSurgeryStatus.scheduled,
        )
        self.db.add(item)
        self.db.flush()

        # Target-native billing integration
        account_id = None
        if admission_id:
            from modules.billing.entities.billing_entities import FinancialAccountType
            from modules.billing.services.billing_service import get_or_create_financial_account
            acc = get_or_create_financial_account(
                self.db,
                hospital_id=self.hospital_id,
                patient_id=patient.id,
                account_type=FinancialAccountType.ipd,
                admission_id=admission_id,
                created_by_name=actor_name,
            )
            account_id = acc.id

        ensure_charge(
            self.db,
            hospital_id=self.hospital_id,
            patient_id=patient.id,
            source_type=BillingSourceType.ot,
            source_id=item.id,
            description=f"OT Charge — {item.surgery_no} · {room_label(ot_room)}"[:512],
            charge_amount=float(item.ot_charge_amount or 0),
            account_id=account_id,
            created_by_name=actor_name,
        )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Booked surgery {item.surgery_no} — {item.surgery_type} for {patient.name}",
        )
        self.db.commit()

        return surgery_to_response(self.repo.get_surgery(item.id))


class UpdateSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID, payload: OtSurgeryUpdate) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot edit completed/cancelled surgery")

        data = payload.model_dump(exclude_unset=True)
        if "surgeon_id" in data and data["surgeon_id"]:
            surgeon = self.repo.get_surgeon(data["surgeon_id"])
            if not surgeon:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgeon not found")

        for key, value in data.items():
            if isinstance(value, str):
                value = value.strip() or None
            setattr(item, key, value)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Updated surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class ConfirmSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status != OtSurgeryStatus.scheduled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only scheduled surgeries can be confirmed",
            )

        item.status = OtSurgeryStatus.confirmed
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Confirmed surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class RescheduleSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID, payload: OtRescheduleRequest) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot reschedule this surgery")

        item.scheduled_at = payload.scheduled_at
        if payload.ot_room_id:
            dept_id = payload.department_id or item.department_id
            if payload.department_id:
                dept = self.repo.get_department(payload.department_id)
                if not dept:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
                item.department_id = payload.department_id
            ot_room = self.repo.resolve_ot_room(payload.ot_room_id, dept_id)
            if not ot_room:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OT room not found")
            item.ot_room_id = ot_room.id
            item.ot_room = room_label(ot_room)
            if not item.department_id:
                item.department_id = ot_room.department_id
        elif payload.ot_room:
            item.ot_room = payload.ot_room.strip()

        if payload.duration_minutes:
            item.duration_minutes = payload.duration_minutes
        if payload.remarks is not None:
            item.remarks = payload.remarks.strip() or None

        if item.status == OtSurgeryStatus.confirmed:
            item.status = OtSurgeryStatus.scheduled

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Rescheduled surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class StartSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status not in (OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Surgery cannot be started from current status",
            )

        item.status = OtSurgeryStatus.in_progress
        item.started_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Started surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


def sync_ot_icu_transfer(
    db: Session,
    hospital_id: UUID,
    surgery: OtSurgery,
    actor: dict[str, Any],
    target_ward_id: UUID | None = None,
    target_bed_id: UUID | None = None,
    ventilator_mode: str | None = None,
    peep: float | None = None,
    fio2_percent: float | None = None,
    icu_notes: str | None = None,
) -> None:
    """Synchronize post-operative patient transfer to ICU ward, bed, and ICU profile."""
    shifted = (surgery.shifted_to or "").strip().lower()
    if not ("icu" in shifted or "intensive" in shifted or "critical" in shifted):
        return

    import uuid
    from fastapi import HTTPException as _HTTPException
    from sqlalchemy import func, or_
    from sqlalchemy.exc import IntegrityError as _IntegrityError
    from modules.beds.entities.bed import Bed, Room, Ward, WardType
    from modules.critical_care.entities.critical_care_entities import IcuPatientProfile
    from modules.inpatient.entities.admission import Admission, AdmissionStatus, BedStaySegment
    from modules.inpatient.services.inpatient_billing_service import next_ip_encounter_id

    # 1. Resolve Bed (locked: post-op transfer mutates occupancy)
    bed = None
    if target_bed_id:
        bed = (
            db.query(Bed)
            .filter(Bed.id == target_bed_id, Bed.hospital_id == hospital_id)
            .with_for_update()
            .first()
        )

    if not bed:
        # Find an unoccupied bed in an ICU ward
        bed = (
            db.query(Bed)
            .join(Bed.ward)
            .filter(
                Bed.hospital_id == hospital_id,
                Bed.is_occupied == False,
                or_(
                    Ward.ward_type == WardType.icu,
                    func.lower(Ward.name).like("%icu%"),
                    func.lower(Ward.name).like("%intensive%"),
                    func.lower(Ward.name).like("%critical%"),
                ),
            )
            .first()
        )

    # If all ICU beds are occupied or none exist, find or create an ICU ward & bed
    if not bed:
        icu_ward = (
            db.query(Ward)
            .filter(
                Ward.hospital_id == hospital_id,
                or_(
                    Ward.ward_type == WardType.icu,
                    func.lower(Ward.name).like("%icu%"),
                    func.lower(Ward.name).like("%intensive%"),
                    func.lower(Ward.name).like("%critical%"),
                ),
            )
            .first()
        )
        if not icu_ward:
            icu_ward = Ward(
                id=uuid.uuid4(),
                hospital_id=hospital_id,
                name="Main ICU",
                ward_type=WardType.icu,
            )
            db.add(icu_ward)
            db.flush()

        room = db.query(Room).filter(Room.ward_id == icu_ward.id, Room.hospital_id == hospital_id).first()
        if not room:
            room = Room(
                id=uuid.uuid4(),
                hospital_id=hospital_id,
                ward_id=icu_ward.id,
                room_code="ICU-101",
                name="Intensive Care Room 1",
            )
            db.add(room)
            db.flush()

        bed = db.query(Bed).filter(Bed.room_id == room.id, Bed.hospital_id == hospital_id).first()
        if not bed:
            bed = Bed(
                id=uuid.uuid4(),
                hospital_id=hospital_id,
                ward_id=icu_ward.id,
                room_id=room.id,
                bed_code="ICU-01",
                is_occupied=False,
            )
            db.add(bed)
            db.flush()

    # 2. Open-episode guard (requested/admitted/discharge_requested), locked.
    # requested → accept with the ICU bed; admitted/discharge_requested →
    # bed transfer; no open episode → create a fresh ICU admission.
    adm = (
        db.query(Admission)
        .filter(
            Admission.patient_id == surgery.patient_id,
            Admission.hospital_id == hospital_id,
            Admission.status.in_(
                [
                    AdmissionStatus.requested,
                    AdmissionStatus.admitted,
                    AdmissionStatus.discharge_requested,
                ]
            ),
        )
        .with_for_update()
        .first()
    )

    if bed.is_occupied and (adm is None or adm.bed_id != bed.id):
        raise _ot_bed_conflict(
            "Target ICU bed is already occupied",
            bed_id=bed.id,
            admission_id=adm.id if adm else None,
            admission_status=adm.status if adm else None,
        )

    now = datetime.now(timezone.utc)
    try:
        if adm is not None and adm.status != AdmissionStatus.requested:
            # Patient is already an active inpatient: transfer bed to ICU.
            # Lock the outgoing bed, close the outgoing segment (Invariant 7),
            # then open the ICU segment.
            if adm.bed_id and adm.bed_id != bed.id:
                _old_bed = (
                    db.query(Bed).filter(Bed.id == adm.bed_id).with_for_update().first()
                )
                if _old_bed:
                    _old_bed.is_occupied = False
            adm.ward_id = bed.ward_id
            adm.room_id = bed.room_id
            adm.bed_id = bed.id
            bed.is_occupied = True
            _open_seg = (
                db.query(BedStaySegment)
                .filter(
                    BedStaySegment.hospital_id == hospital_id,
                    BedStaySegment.admission_id == adm.id,
                    BedStaySegment.ended_at.is_(None),
                )
                .with_for_update()
                .order_by(BedStaySegment.started_at.desc())
                .first()
            )
            if _open_seg:
                _open_seg.ended_at = now
            segment = BedStaySegment(
                hospital_id=hospital_id,
                admission_id=adm.id,
                ward_id=bed.ward_id,
                room_id=bed.room_id,
                bed_id=bed.id,
                rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0) if bed.ward else 0.0,
                started_at=now,
                ended_at=None,
            )
            db.add(segment)
        elif adm is not None:
            # Canonical request exists: accept it with the ICU bed instead of
            # creating a duplicate episode.
            adm.status = AdmissionStatus.admitted
            adm.admitted_at = now
            adm.ward_id = bed.ward_id
            adm.room_id = bed.room_id
            adm.bed_id = bed.id
            if not adm.ip_id:
                adm.ip_id = next_ip_encounter_id(db, hospital_id)
            if surgery.surgeon_id and not adm.doctor_id:
                adm.doctor_id = surgery.surgeon_id
            bed.is_occupied = True
            from modules.patients.entities.patient import Patient as _Pat
            from modules.patients.entities.patient import PatientStatus as _PatStatus

            _pat = (
                db.query(_Pat)
                .filter(
                    _Pat.id == surgery.patient_id,
                    _Pat.hospital_id == hospital_id,
                )
                .first()
            )
            if _pat is not None:
                _pat.status = _PatStatus.admitted
            from modules.appointments.entities.appointment import (
                Appointment as _Appt,
            )
            from modules.appointments.entities.appointment import (
                AppointmentStatus as _ApptStatus,
            )

            for _p in (
                db.query(_Appt)
                .filter(
                    _Appt.hospital_id == hospital_id,
                    _Appt.patient_id == surgery.patient_id,
                    _Appt.status == _ApptStatus.ipd_transfer_requested,
                )
                .all()
            ):
                _p.status = _ApptStatus.transferred_to_inpatient
                _p.admission_id = adm.id
            segment = BedStaySegment(
                hospital_id=hospital_id,
                admission_id=adm.id,
                ward_id=bed.ward_id,
                room_id=bed.room_id,
                bed_id=bed.id,
                rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0) if bed.ward else 0.0,
                started_at=now,
                ended_at=None,
            )
            db.add(segment)
        else:
            # Patient was an outpatient / emergency case: create new ICU admission
            # via the canonical repository (snapshot + occupancy + appointment
            # reconcile, spec §18) instead of an inline constructor.
            from modules.inpatient.db.admissions_repository import AdmissionsRepository as _AdmRepo
            from modules.patients.entities.patient import Patient as _Patient

            ip_id = next_ip_encounter_id(db, hospital_id)
            _patient = (
                db.query(_Patient)
                .filter(
                    _Patient.id == surgery.patient_id,
                    _Patient.hospital_id == hospital_id,
                )
                .first()
            )
            if _patient is None:
                raise _HTTPException(
                    status_code=404, detail="Patient not found"
                )
            adm = _AdmRepo(db).create_admission(
                hospital_id=hospital_id,
                patient=_patient,
                bed=bed,
                ward_id=bed.ward_id,
                room_id=bed.room_id,
                doctor_id=surgery.surgeon_id,
                ip_id=ip_id,
                notes=icu_notes or f"Transferred from OT post-surgery: {surgery.procedure_performed or surgery.surgery_type}",
                admitted_at=now,
            )

            segment = BedStaySegment(
                hospital_id=hospital_id,
                admission_id=adm.id,
                ward_id=bed.ward_id,
                room_id=bed.room_id,
                bed_id=bed.id,
                rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0) if bed.ward else 0.0,
                started_at=now,
                ended_at=None,
            )
            db.add(segment)
        db.flush()
    except _IntegrityError as exc:
        db.rollback()
        raise _ot_conflict(
            "OT post-op transfer collided concurrently; please retry",
            admission_id=adm.id if adm else None,
            admission_status=adm.status if adm else None,
            bed_id=bed.id if bed else None,
        ) from exc

    # 3. Create or update ICU Patient Profile
    profile = (
        db.query(IcuPatientProfile)
        .filter(
            IcuPatientProfile.admission_id == adm.id,
            IcuPatientProfile.hospital_id == hospital_id,
        )
        .first()
    )
    v_mode = ventilator_mode or "Post-Op Recovery"
    if not profile:
        profile = IcuPatientProfile(
            hospital_id=hospital_id,
            admission_id=adm.id,
            patient_id=surgery.patient_id,
            ventilator_mode=v_mode,
            peep=peep or 5.0,
            fio2_percent=fio2_percent or 40.0,
            invasive_line_days={"ett": 1, "arterial_line": 1, "foley": 1},
            inotrope_support=False,
            care_indicators={
                "source": "OT Post-Op Handover",
                "surgery_no": surgery.surgery_no,
                "procedure": surgery.procedure_performed or surgery.surgery_type,
                "acuity": "critical" if "vent" in v_mode.lower() else "high",
            },
        )
        db.add(profile)
    else:
        if ventilator_mode:
            profile.ventilator_mode = ventilator_mode
        if peep is not None:
            profile.peep = peep
        if fio2_percent is not None:
            profile.fio2_percent = fio2_percent

    write_audit_log(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="transfer_icu",
        entity_type="admission",
        entity_id=adm.id,
        summary=f"Transferred patient from OT ({surgery.surgery_no}) to ICU Bed {bed.bed_code}",
    )
    write_audit_log(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="update",
        entity_type="bed",
        entity_id=bed.id,
        summary=f"Bed {bed.bed_code} received OT post-op transfer of admission {adm.id}",
    )
    write_audit_log(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="update",
        entity_type="patient",
        entity_id=surgery.patient_id,
        summary="Patient transferred from OT to ICU",
    )
    if adm.source_appointment_id:
        write_audit_log(
            db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="appointment",
            entity_id=adm.source_appointment_id,
            summary="Appointment linkage carried with OT post-op transfer",
        )


class CompleteSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID, payload: OtCompleteRequest | None = None) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")

        now = datetime.now(timezone.utc)
        sync_time_based_status(item, now)
        if item.status in (OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed):
            start = item.scheduled_at
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if now >= start:
                item.status = OtSurgeryStatus.in_progress
                item.started_at = item.started_at or start

        if item.status != OtSurgeryStatus.in_progress:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Surgery is not ongoing yet")

        item.status = OtSurgeryStatus.completed
        item.completed_at = now
        started = item.started_at
        if started and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)

        if payload:
            if payload.shifted_to:
                item.shifted_to = payload.shifted_to.strip()
            if payload.actual_duration_minutes:
                item.actual_duration_minutes = payload.actual_duration_minutes
            elif started:
                item.actual_duration_minutes = max(1, int((now - started).total_seconds() // 60))
        elif started:
            item.actual_duration_minutes = max(1, int((now - started).total_seconds() // 60))

        if payload and payload.shifted_to:
            sync_ot_icu_transfer(
                self.db,
                self.hospital_id,
                item,
                self.user,
                target_ward_id=payload.target_ward_id,
                target_bed_id=payload.target_bed_id,
                ventilator_mode=payload.ventilator_mode,
                peep=payload.peep,
                fio2_percent=payload.fio2_percent,
                icu_notes=payload.icu_notes,
            )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Completed surgery {item.surgery_no}",
        )
        try:
            self.db.commit()
        except _IntegrityError as exc:
            self.db.rollback()
            raise _ot_conflict(
                "Surgery completion collided concurrently; please retry",
            ) from exc
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class CancelSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Surgery cannot be cancelled")

        item.status = OtSurgeryStatus.cancelled
        cancel_charge_for_source(self.db, self.hospital_id, BillingSourceType.ot, item.id)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Cancelled surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class SaveNotesAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID, payload: OtNotesRequest) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status == OtSurgeryStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot add notes to a cancelled surgery")

        sync_time_based_status(item)
        item.pre_op_diagnosis = payload.pre_op_diagnosis.strip()
        item.procedure_performed = payload.procedure_performed.strip()
        item.findings = payload.findings.strip() if payload.findings else None
        item.implants_used = payload.implants_used.strip() if payload.implants_used else None
        item.complications = payload.complications.strip() if payload.complications else None
        item.post_op_instructions = payload.post_op_instructions.strip() if payload.post_op_instructions else None
        item.follow_up_notes = payload.follow_up_notes.strip() if payload.follow_up_notes else None
        if payload.shifted_to:
            item.shifted_to = payload.shifted_to.strip()
        item.notes_recorded_by = _actor_name(self.user)
        item.notes_recorded_at = datetime.now(timezone.utc)
        if payload.ot_report_file_data:
            item.ot_report_file_name = payload.ot_report_file_name
            item.ot_report_file_data = payload.ot_report_file_data
        if payload.consent_file_data:
            item.consent_file_name = payload.consent_file_name
            item.consent_file_data = payload.consent_file_data
        if payload.image_file_data:
            item.image_file_name = payload.image_file_name
            item.image_file_data = payload.image_file_data

        if item.status == OtSurgeryStatus.in_progress:
            item.status = OtSurgeryStatus.completed
            item.completed_at = item.completed_at or datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_notes",
            entity_id=item.id,
            summary=f"Saved operation notes for {item.surgery_no}",
        )
        sync_ot_surgery_medical_record(self.db, item)
        if payload.shifted_to:
            sync_ot_icu_transfer(
                self.db,
                self.hospital_id,
                item,
                self.user,
            )
        try:
            self.db.commit()
        except _OTIntegrityError as exc:
            self.db.rollback()
            raise _ot_conflict(
                "Surgery notes save collided concurrently; please retry",
            ) from exc

        return surgery_to_response(self.repo.get_surgery(surgery_id))
