"""
Actions for Inpatient Admissions lifecycle and bed allotment.

Conforms to UltrionTech-Backend-Template modules/inpatient/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from shared.exceptions.base import ConflictError, NotFoundError, ValidationError

from modules.beds.db.beds_repository import BedsRepository
from modules.beds.entities.bed import Bed
from modules.doctors.entities.doctor import HospitalUser
from modules.inpatient.contracts.inpatient_contracts import (
    AdmissionDetail,
    AdmissionSummary,
    AdmitPatientRequest,
    AdmitRequest,
    AllocateRequest,
    DischargeQueueItem,
    DischargeRequest,
    DischargeRequestCreate,
    DischargeResponse,
    TransferRequest,
)
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.entities.admission import Admission, AdmissionStatus, BedStaySegment
from modules.inpatient.services.inpatient_billing_service import (
    InpatientBillingService,
    next_ip_encounter_id,
)
from modules.patients.entities.patient import Patient, PatientStatus
from shared.audit.service import write_audit_log


def to_admission_detail(a: Admission) -> AdmissionDetail:
    """Format Admission entity into AdmissionDetail response contract.

    FLAW-010: prefer the immutable demographic snapshot captured at admission
    time (and refreshed via amend_demographics for active admissions) so
    wristbands / discharge summaries render the identity that was in effect for
    this encounter. Falls back to the live Patient row for legacy rows created
    before snapshot columns existed.
    """
    ward = a.ward
    snap_name = getattr(a, "patient_name", None) or (a.patient.name if a.patient else None)
    status = a.status
    return AdmissionDetail(
        id=a.id,
        patient_id=a.patient_id,
        patient_name=snap_name,
        patient_uhid=getattr(a.patient, "uhid", None) if a.patient else None,
        patient_mobile=a.patient.mobile if a.patient else None,
        ward_id=a.ward_id,
        room_id=a.room_id,
        bed_id=a.bed_id,
        ward_name=ward.name if ward else None,
        room_code=a.room.room_code if a.room else None,
        bed_code=a.bed.bed_code if a.bed else None,
        doctor_id=a.doctor_id,
        doctor_name=a.doctor.name if a.doctor else None,
        status=status,
        # Canonical "Admitted — Awaiting Bed": admitted without a bed (§2).
        is_awaiting_bed=status == AdmissionStatus.admitted and a.bed_id is None,
        notes=a.notes,
        discharge_notes=getattr(a, "discharge_notes", None),
        admitted_at=a.admitted_at,
        discharged_at=a.discharged_at,
        admission_fee=float(getattr(ward, "admission_fee", 0) or 0) if ward else 0.0,
        bed_charge_per_day=float(getattr(ward, "bed_charge_per_day", 0) or 0) if ward else 0.0,
        ip_id=getattr(a, "ip_id", None),
        source_appointment_id=getattr(a, "source_appointment_id", None),
    )


class AdmitPatientAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.beds_repo = BedsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(
        self,
        hospital_id: UUID,
        payload: AdmitRequest,
        actor: dict[str, Any],
    ) -> AdmissionDetail:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise NotFoundError("Patient not found")

        active = (
            self.db.query(Admission)
            .filter(
                Admission.patient_id == payload.patient_id,
                Admission.hospital_id == hospital_id,
                # Invariant 1: requested/admitted/discharge_requested all block
                # a second episode (DB partial index is the backstop).
                Admission.status.in_(
                    [
                        AdmissionStatus.requested,
                        AdmissionStatus.admitted,
                        AdmissionStatus.discharge_requested,
                    ]
                ),
            )
            .first()
        )
        if active:
            raise ConflictError("Patient is already admitted")

        bed = (
            self.db.query(Bed)
            .filter(Bed.id == payload.bed_id, Bed.hospital_id == hospital_id)
            .with_for_update()
            .first()
        )
        if not bed:
            raise NotFoundError("Bed not found")
        if bed.is_occupied:
            raise ConflictError("Bed is already occupied")
        if bed.ward_id != payload.ward_id or bed.room_id != payload.room_id:
            raise ValidationError("Ward/Room does not match selected bed")

        if payload.doctor_id:
            doc = (
                self.db.query(HospitalUser)
                .filter(HospitalUser.id == payload.doctor_id, HospitalUser.hospital_id == hospital_id)
                .first()
            )
            if not doc:
                raise NotFoundError("Doctor not found")

        admitted_at = datetime.now(timezone.utc)
        if payload.admission_date:
            admitted_at = datetime.combine(payload.admission_date, time(9, 0), tzinfo=timezone.utc)

        ip_id = next_ip_encounter_id(self.db, hospital_id)
        admission = self.admissions_repo.create_admission(
            hospital_id=hospital_id,
            patient=patient,
            bed=bed,
            ward_id=payload.ward_id,
            room_id=payload.room_id,
            doctor_id=payload.doctor_id,
            ip_id=ip_id,
            notes=payload.notes,
            admitted_at=admitted_at,
        )

        actor_name = str(actor.get("name") or "System")
        self.billing_svc.ensure_admission_charge(
            hospital_id=hospital_id,
            patient_id=patient.id,
            admission_id=admission.id,
            ward_name=bed.ward.name if bed.ward else None,
            admission_fee=float(getattr(bed.ward, "admission_fee", 0) or 0) if bed.ward else 0.0,
            created_by_name=actor_name,
        )

        initial_segment = BedStaySegment(
            hospital_id=hospital_id,
            admission_id=admission.id,
            ward_id=bed.ward_id,
            room_id=bed.room_id,
            bed_id=bed.id,
            rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0) if bed.ward else 0.0,
            started_at=admitted_at,
            ended_at=None,
        )
        self.db.add(initial_segment)

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Admitted {patient.uhid} {patient.name} ({admission.ip_id})",
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError("Patient already admitted or bed just taken") from exc
        refreshed = self.admissions_repo.get_admission_by_id(hospital_id, admission.id)
        return to_admission_detail(refreshed or admission)


class AllocateBedAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.beds_repo = BedsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        payload: AllocateRequest,
        actor: dict[str, Any],
    ) -> AdmissionDetail:
        # Locked admission load: allocate mutates location + occupancy.
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.hospital_id == hospital_id,
                (Admission.id == payload.admission_id)
                if payload.admission_id
                else (Admission.patient_id == payload.patient_id),
                Admission.status == AdmissionStatus.admitted,
            )
            .with_for_update()
            .first()
        )
        if not admission:
            raise NotFoundError("Active admission not found")

        if admission.bed_id == payload.bed_id:
            return to_admission_detail(admission)

        new_bed = (
            self.db.query(Bed)
            .filter(Bed.id == payload.bed_id, Bed.hospital_id == hospital_id)
            .with_for_update()
            .first()
        )
        if not new_bed:
            raise NotFoundError("Bed not found")
        if new_bed.ward_id != payload.ward_id or new_bed.room_id != payload.room_id:
            raise ValidationError("Ward/Room does not match selected bed")
        if new_bed.is_occupied:
            raise ConflictError("Bed is already occupied")

        from datetime import datetime as _dt
        from datetime import timezone as _tz

        old_bed = (
            self.db.query(Bed).filter(Bed.id == admission.bed_id).with_for_update().first()
            if admission.bed_id
            else None
        )
        if old_bed:
            old_bed.is_occupied = False

        admission.ward_id = payload.ward_id
        admission.room_id = payload.room_id
        admission.bed_id = payload.bed_id
        new_bed.is_occupied = True

        # Allocate opens a stay segment when none is open (awaiting-bed case);
        # a spurious extra open segment can never survive thanks to the
        # partial-unique open-segment index (Invariant 7 backstop).
        now = _dt.now(_tz.utc)
        open_segment = (
            self.db.query(BedStaySegment)
            .filter(
                BedStaySegment.hospital_id == hospital_id,
                BedStaySegment.admission_id == admission.id,
                BedStaySegment.ended_at.is_(None),
            )
            .with_for_update()
            .first()
        )
        if open_segment is None:
            self.db.add(
                BedStaySegment(
                    hospital_id=hospital_id,
                    admission_id=admission.id,
                    ward_id=new_bed.ward_id,
                    room_id=new_bed.room_id,
                    bed_id=new_bed.id,
                    rate_per_day=float(getattr(new_bed.ward, "bed_charge_per_day", 0) or 0)
                    if new_bed.ward
                    else 0.0,
                    started_at=now,
                    ended_at=None,
                )
            )

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Allocated bed {new_bed.bed_code} to {admission.patient.name if admission.patient else 'patient'}",
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError("Bed was just taken or segment changed concurrently") from exc
        refreshed = self.admissions_repo.get_admission_by_id(hospital_id, admission.id)
        return to_admission_detail(refreshed or admission)


class TransferBedAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.beds_repo = BedsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        payload: TransferRequest,
        actor: dict[str, Any],
    ) -> AdmissionDetail:
        # Spec §10: lock admission + old bed + new bed, single commit.
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.hospital_id == hospital_id,
                (Admission.id == payload.admission_id)
                if payload.admission_id
                else (Admission.patient_id == payload.patient_id),
                Admission.status == AdmissionStatus.admitted,
            )
            .with_for_update()
            .first()
        )
        if not admission:
            raise NotFoundError("Active admission not found")

        if admission.bed_id == payload.to_bed_id:
            raise ValidationError("Patient is already on this bed")
        if not admission.bed_id:
            raise ValidationError("Admission has no bed to transfer from — allocate a bed first")

        new_bed = (
            self.db.query(Bed)
            .filter(Bed.id == payload.to_bed_id, Bed.hospital_id == hospital_id)
            .with_for_update()
            .first()
        )
        if not new_bed:
            raise NotFoundError("Bed not found")
        if new_bed.ward_id != payload.to_ward_id or new_bed.room_id != payload.to_room_id:
            raise ValidationError("Ward/Room does not match selected bed")
        if new_bed.is_occupied:
            raise ConflictError("Bed is already occupied")

        old_bed = (
            self.db.query(Bed).filter(Bed.id == admission.bed_id).with_for_update().first()
        )
        from_label = (
            f"{old_bed.ward.name if old_bed and old_bed.ward else '?'} → {old_bed.room.room_code if old_bed and old_bed.room else '?'} → {old_bed.bed_code if old_bed else '?'}"
        )
        to_label = (
            f"{new_bed.ward.name if new_bed.ward else '?'} → {new_bed.room.room_code if new_bed.room else '?'} → {new_bed.bed_code}"
        )

        if old_bed:
            old_bed.is_occupied = False
        admission.ward_id = payload.to_ward_id
        admission.room_id = payload.to_room_id
        admission.bed_id = payload.to_bed_id
        new_bed.is_occupied = True

        # Close current active BedStaySegment and start new segment for destination bed
        transfer_now = datetime.now(timezone.utc)
        current_segment = (
            self.db.query(BedStaySegment)
            .filter(
                BedStaySegment.hospital_id == hospital_id,
                BedStaySegment.admission_id == admission.id,
                BedStaySegment.ended_at.is_(None),
            )
            .with_for_update()
            .order_by(BedStaySegment.started_at.desc())
            .first()
        )
        if current_segment:
            current_segment.ended_at = transfer_now
        
        new_segment = BedStaySegment(
            hospital_id=hospital_id,
            admission_id=admission.id,
            ward_id=new_bed.ward_id,
            room_id=new_bed.room_id,
            bed_id=new_bed.id,
            rate_per_day=float(getattr(new_bed.ward, "bed_charge_per_day", 0) or 0) if new_bed.ward else 0.0,
            started_at=transfer_now,
            ended_at=None,
        )
        self.db.add(new_segment)

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Transferred {admission.patient.name if admission.patient else 'patient'}: {from_label} → {to_label}",
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError("Bed transfer conflicted concurrently; please retry") from exc
        refreshed = self.admissions_repo.get_admission_by_id(hospital_id, admission.id)
        return to_admission_detail(refreshed or admission)


class RequestDischargeAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(
        self,
        hospital_id: UUID,
        payload: DischargeRequestCreate,
        actor: dict[str, Any],
    ) -> AdmissionDetail:
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.hospital_id == hospital_id,
                (Admission.id == payload.admission_id)
                if payload.admission_id
                else (Admission.patient_id == payload.patient_id),
                Admission.status == AdmissionStatus.admitted,
            )
            .with_for_update()
            .first()
        )
        if not admission:
            raise NotFoundError("Active admission not found")

        now = datetime.now(timezone.utc)
        admission.status = AdmissionStatus.discharge_requested
        if payload.discharge_notes and payload.discharge_notes.strip():
            admission.discharge_notes = payload.discharge_notes.strip()

        ward = admission.ward
        self.billing_svc.ensure_bed_charge(
            hospital_id=hospital_id,
            patient_id=admission.patient_id,
            admission_id=admission.id,
            admitted_at=admission.admitted_at,
            discharged_at=now,
            ward_name=ward.name if ward else None,
            room_code=admission.room.room_code if admission.room else None,
            bed_code=admission.bed.bed_code if admission.bed else None,
            bed_charge_per_day=float(getattr(ward, "bed_charge_per_day", 0) or 0) if ward else 0.0,
            created_by_name=actor.get("name") or "System",
        )

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Discharge requested for {admission.patient.name if admission.patient else 'patient'}",
        )
        self.db.commit()
        refreshed = self.admissions_repo.get_admission_by_id(
            hospital_id, admission.id, statuses=(AdmissionStatus.discharge_requested,)
        )
        return to_admission_detail(refreshed or admission)


class ListDischargeRequestsAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(self, hospital_id: UUID) -> list[DischargeQueueItem]:
        rows = self.admissions_repo.list_discharge_requests(hospital_id)
        now = datetime.now(timezone.utc)
        items: list[DischargeQueueItem] = []
        for a in rows:
            ward = a.ward
            self.billing_svc.ensure_bed_charge(
                hospital_id=hospital_id,
                patient_id=a.patient_id,
                admission_id=a.id,
                admitted_at=a.admitted_at,
                discharged_at=now,
                ward_name=ward.name if ward else None,
                room_code=a.room.room_code if a.room else None,
                bed_code=a.bed.bed_code if a.bed else None,
                bed_charge_per_day=float(getattr(ward, "bed_charge_per_day", 0) or 0) if ward else 0.0,
                created_by_name="System",
            )
        ledgers = self.billing_svc.get_ledger_totals_bulk(
            hospital_id, [a.patient_id for a in rows]
        )
        for a in rows:
            fin = ledgers.get(a.patient_id, {})
            outstanding = float(fin.get("outstanding") or 0)
            base = to_admission_detail(a)
            items.append(
                DischargeQueueItem(
                    **base.model_dump(),
                    total_charges=float(fin.get("total_charges") or 0),
                    total_paid=float(fin.get("total_paid") or 0),
                    outstanding=outstanding,
                    can_discharge=outstanding <= 0.009,
                )
            )
        if rows:
            self.db.commit()
        return items


class DischargePatientAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(
        self,
        hospital_id: UUID,
        payload: DischargeRequest,
        actor: dict[str, Any],
    ) -> AdmissionDetail:
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.hospital_id == hospital_id,
                (Admission.id == payload.admission_id)
                if payload.admission_id
                else (Admission.patient_id == payload.patient_id),
                Admission.status == AdmissionStatus.discharge_requested,
            )
            .with_for_update()
            .first()
        )
        if not admission:
            raise NotFoundError("Active admission not found")

        d_date = payload.discharge_date or date.today()
        d_time = payload.discharge_time or datetime.now(timezone.utc).time().replace(microsecond=0)
        discharged_at = datetime.combine(d_date, d_time, tzinfo=timezone.utc)

        ward = admission.ward
        self.billing_svc.ensure_bed_charge(
            hospital_id=hospital_id,
            patient_id=admission.patient_id,
            admission_id=admission.id,
            admitted_at=admission.admitted_at,
            discharged_at=discharged_at,
            ward_name=ward.name if ward else None,
            room_code=admission.room.room_code if admission.room else None,
            bed_code=admission.bed.bed_code if admission.bed else None,
            bed_charge_per_day=float(getattr(ward, "bed_charge_per_day", 0) or 0) if ward else 0.0,
            created_by_name=actor.get("name") or "System",
        )
        fin = self.billing_svc.get_ledger_totals(hospital_id, admission.patient_id)
        outstanding = float(fin.get("outstanding") or 0)
        if outstanding > 0.009:
            raise ValidationError(f"Cannot discharge — outstanding balance ₹{outstanding:,.2f}. Clear all dues before discharging.")

        admission.status = AdmissionStatus.discharged
        # Invariant: discharged ⇔ discharged_at set (spec §23).
        admission.discharged_at = discharged_at
        if payload.discharge_notes and payload.discharge_notes.strip():
            admission.discharge_notes = payload.discharge_notes.strip()

        if admission.bed_id:
            locked_bed = (
                self.db.query(Bed).filter(Bed.id == admission.bed_id).with_for_update().first()
            )
            if locked_bed:
                locked_bed.is_occupied = False
        elif admission.bed:
            admission.bed.is_occupied = False
        if admission.patient:
            admission.patient.status = PatientStatus.active

        # Close any open stay segment at discharge so no open segment survives
        # the episode (Invariant 7).
        open_segment = (
            self.db.query(BedStaySegment)
            .filter(
                BedStaySegment.hospital_id == hospital_id,
                BedStaySegment.admission_id == admission.id,
                BedStaySegment.ended_at.is_(None),
            )
            .with_for_update()
            .first()
        )
        if open_segment:
            open_segment.ended_at = discharged_at

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Discharged {admission.patient.name if admission.patient else 'patient'} — bed freed",
        )
        self.db.commit()
        refreshed = self.admissions_repo.get_admission_by_id(hospital_id, admission.id)
        return to_admission_detail(refreshed or admission)

