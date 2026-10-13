"""
Actions for Inpatient Admissions lifecycle and bed allotment.

Conforms to UltrionTech-Backend-Template modules/inpatient/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from shared.exceptions.base import NotFoundError, ValidationError

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


def to_admission_detail(
    a: Admission,
    source_appointment_map: dict[UUID, Any] | None = None,
) -> AdmissionDetail:
    """Format Admission entity into AdmissionDetail response contract.

    FLAW-010: prefer the immutable demographic snapshot captured at admission
    time (and refreshed via amend_demographics for active admissions) so
    wristbands / discharge summaries render the identity that was in effect for
    this encounter. Falls back to the live Patient row for legacy rows created
    before snapshot columns existed.
    """
    from modules.inpatient.contracts.inpatient_contracts import AdmissionCareTeamResponse
    from modules.inpatient.entities.care_team import AdmissionCareTeamRole

    ward = a.ward
    snap_name = getattr(a, "patient_name", None) or (a.patient.name if a.patient else None)
    status = a.status

    care_team_dtos: list[AdmissionCareTeamResponse] = []
    primary_doc_name = a.doctor.name if a.doctor else None
    care_members = getattr(a, "care_team", None) or []
    for m in care_members:
        doc = getattr(m, "doctor", None)
        d_name = getattr(doc, "name", None) or getattr(doc, "full_name", None)
        if m.role == AdmissionCareTeamRole.primary_consultant and m.is_active and d_name:
            primary_doc_name = d_name
        care_team_dtos.append(
            AdmissionCareTeamResponse(
                id=m.id,
                hospital_id=m.hospital_id,
                admission_id=m.admission_id,
                doctor_id=m.doctor_id,
                doctor_name=d_name,
                doctor_department=getattr(doc, "department", None),
                role=m.role,
                is_active=m.is_active,
                assigned_at=m.assigned_at,
                assigned_by_id=m.assigned_by_id,
                assigned_by_name=m.assigned_by_name,
                ended_at=m.ended_at,
                notes=m.notes,
            )
        )

    if not care_team_dtos and a.doctor_id and a.doctor:
        care_team_dtos.append(
            AdmissionCareTeamResponse(
                id=a.id,
                hospital_id=a.hospital_id,
                admission_id=a.id,
                doctor_id=a.doctor_id,
                doctor_name=a.doctor.name,
                doctor_department=getattr(a.doctor, "department", None),
                role=AdmissionCareTeamRole.primary_consultant,
                is_active=True,
                assigned_at=a.admitted_at,
                notes="Legacy primary consultant",
            )
        )

    # Requirement 22: OP Number belongs to OPD; only present if transferred from an OPD appointment.
    op_id = None
    if getattr(a, "source_appointment", None):
        op_id = a.source_appointment.op_id
    elif source_appointment_map and getattr(a, "source_appointment_id", None) in source_appointment_map:
        src = source_appointment_map.get(a.source_appointment_id)
        if src:
            op_id = src.op_id
    elif getattr(a, "source_appointment_id", None):
        from sqlalchemy.orm import object_session
        from modules.appointments.entities.appointment import Appointment
        s = object_session(a)
        if s:
            src_appt = s.query(Appointment).filter(Appointment.id == a.source_appointment_id).first()
            if src_appt:
                op_id = src_appt.op_id

    return AdmissionDetail(
        id=a.id,
        hospital_id=a.hospital_id,
        patient_id=a.patient_id,
        patient_name=snap_name,
        patient_current_name=a.patient.name if a.patient else None,
        patient_date_of_birth=a.patient.date_of_birth if a.patient else None,
        patient_current_age=a.patient.age if a.patient else None,
        patient_current_gender=a.patient.gender if a.patient else None,
        gender_at_admission=getattr(a, "gender", None),
        age_at_admission=getattr(a, "age_at_admission", None),
        patient_uhid=getattr(a.patient, "uhid", None) if a.patient else None,
        patient_mobile=a.patient.mobile if a.patient else None,
        ward_id=a.ward_id,
        room_id=a.room_id,
        bed_id=a.bed_id,
        ward_name=ward.name if ward else None,
        room_code=a.room.room_code if a.room else None,
        bed_code=a.bed.bed_code if a.bed else None,
        doctor_id=a.doctor_id,
        doctor_name=primary_doc_name,
        primary_doctor_name=primary_doc_name,
        department_id=getattr(a, "department_id", None),
        department_name=getattr(a, "department_name", None),
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
        op_id=op_id,
        source_appointment_id=getattr(a, "source_appointment_id", None),
        no_discharge_meds=bool(getattr(a, "no_discharge_meds", False)),
        no_discharge_meds_reason=getattr(a, "no_discharge_meds_reason", None),
        no_discharge_meds_doctor_id=getattr(a, "no_discharge_meds_doctor_id", None),
        care_team=care_team_dtos,
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

        from modules.inpatient.utils.admission_conflicts import (
            admission_conflict as _admission_conflict,
            bed_conflict as _bed_conflict,
        )

        active = self.admissions_repo.get_open_admission(
            hospital_id, payload.patient_id, for_update=True
        )
        if active:
            raise _admission_conflict(
                f"Patient already has an {active.status.value} admission episode",
                admission_id=active.id,
                admission_status=active.status,
                bed_id=active.bed_id,
                doctor_id=active.doctor_id,
            )

        bed = (
            self.db.query(Bed)
            .filter(Bed.id == payload.bed_id, Bed.hospital_id == hospital_id)
            .with_for_update()
            .first()
        )
        if not bed:
            raise NotFoundError("Bed not found")
        if bed.is_occupied:
            raise _bed_conflict("Bed is already occupied", bed_id=bed.id)
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

        if getattr(payload, "department_id", None):
            from modules.masters.entities.organization_entities import Department
            dept = self.db.query(Department).filter(
                Department.id == payload.department_id,
                Department.hospital_id == hospital_id,
            ).first()
            if not dept:
                raise ValidationError("Referenced department not found in this hospital")
            admission.department_id = dept.id
            admission.department_name = dept.name
        elif getattr(payload, "department_name", None):
            from modules.masters.entities.organization_entities import Department
            dept = self.db.query(Department).filter(
                Department.name == payload.department_name,
                Department.hospital_id == hospital_id,
            ).first()
            if dept:
                admission.department_id = dept.id
                admission.department_name = dept.name
            else:
                admission.department_name = payload.department_name

        actor_name = str(actor.get("name") or "System")
        self.billing_svc.ensure_financial_account(
            hospital_id=hospital_id,
            patient_id=patient.id,
            admission_id=admission.id,
            created_by_name=actor_name,
        )

        # ── Authoritative Backend Financial Clearance Check for Bed Allocation ──
        from modules.billing.services.service_financial_clearance import enforce_bed_allocation_clearance
        enforce_bed_allocation_clearance(
            self.db,
            hospital_id,
            admission,
            ward_id=payload.ward_id,
            bed_id=payload.bed_id,
            actor=actor,
            is_emergency_override=bool(getattr(payload, "is_emergency_override", False)),
            emergency_override_reason=getattr(payload, "emergency_override_reason", None),
        )

        self.billing_svc.ensure_admission_charge(
            hospital_id=hospital_id,
            patient_id=patient.id,
            admission_id=admission.id,
            ward_name=bed.ward.name if bed.ward else None,
            admission_fee=float(getattr(bed.ward, "admission_fee", 0) or 0) if bed.ward else 0.0,
            created_by_name=actor_name,
        )

        if payload.doctor_id:
            from modules.inpatient.db.care_team_repository import CareTeamRepository
            from modules.inpatient.entities.care_team import AdmissionCareTeamRole
            actor_id_str = actor.get("id") or actor.get("sub")
            actor_id = None
            if actor_id_str:
                try:
                    actor_id = UUID(str(actor_id_str))
                except (ValueError, TypeError):
                    actor_id = None
            ct_repo = CareTeamRepository(self.db, hospital_id)
            # Section 13: Preserve admitting doctor as permanent record
            ct_repo.add_member(
                admission_id=admission.id,
                doctor_id=payload.doctor_id,
                role=AdmissionCareTeamRole.admitting_doctor,
                assigned_by_id=actor_id,
                assigned_by_name=actor_name,
                notes="Admitting doctor preserved at admission",
            )
            # Assign initial primary consultant
            ct_repo.add_member(
                admission_id=admission.id,
                doctor_id=payload.doctor_id,
                role=AdmissionCareTeamRole.primary_consultant,
                assigned_by_id=actor_id,
                assigned_by_name=actor_name,
                notes="Primary consultant assigned at admission",
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
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="bed",
            entity_id=str(bed.id),
            summary=f"Bed {bed.bed_code} occupied by admission {admission.id}",
        )
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="patient",
            entity_id=str(patient.id),
            summary="Patient admitted",
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise _admission_conflict(
                "Patient already admitted or bed just taken",
                bed_id=payload.bed_id,
                doctor_id=payload.doctor_id,
            ) from exc
        refreshed = self.admissions_repo.get_admission_by_id(hospital_id, admission.id)
        return to_admission_detail(refreshed or admission)


class AllocateBedAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.beds_repo = BedsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(
        self,
        hospital_id: UUID,
        payload: AllocateRequest,
        actor: dict[str, Any],
    ) -> AdmissionDetail:
        from modules.inpatient.utils.admission_conflicts import (
            admission_conflict as _alloc_conflict,
            bed_conflict as _alloc_bed_conflict,
        )

        # Locked admission load: allocate mutates location + occupancy.
        # Bedless ("awaiting bed") episodes only — bedded episodes must use
        # /transfer so the outgoing stay segment is closed (Invariant 7).
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.hospital_id == hospital_id,
                (Admission.id == payload.admission_id)
                if payload.admission_id
                else (Admission.patient_id == payload.patient_id),
                Admission.status.in_(
                    [AdmissionStatus.admitted, AdmissionStatus.discharge_requested]
                ),
            )
            .with_for_update()
            .first()
        )
        if not admission:
            raise NotFoundError("Active admission not found")
        if admission.bed_id is not None:
            if admission.bed_id == payload.bed_id:
                return to_admission_detail(admission)
            raise _alloc_conflict(
                "Admission already has a bed — use /transfer to move beds",
                admission_id=admission.id,
                admission_status=admission.status,
                bed_id=admission.bed_id,
                code="WRONG_STATE",
            )

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
            raise _alloc_bed_conflict("Bed is already occupied", bed_id=new_bed.id)

        from datetime import datetime as _dt
        from datetime import timezone as _tz
        now = _dt.now(_tz.utc)

        # ── Authoritative Backend Financial Clearance Check for First Bed Allocation ──
        if admission.bed_id is None:
            from modules.billing.services.service_financial_clearance import enforce_bed_allocation_clearance

            enforce_bed_allocation_clearance(
                self.db,
                hospital_id,
                admission,
                ward_id=payload.ward_id,
                bed_id=payload.bed_id,
                actor=actor,
                is_emergency_override=bool(getattr(payload, "is_emergency_override", False)),
                emergency_override_reason=getattr(payload, "emergency_override_reason", None),
            )

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

        self.billing_svc.ensure_admission_charge(
            hospital_id=hospital_id,
            patient_id=admission.patient_id,
            admission_id=admission.id,
            ward_name=new_bed.ward.name if new_bed.ward else None,
            admission_fee=float(getattr(new_bed.ward, "admission_fee", 0) or 0)
            if new_bed.ward
            else 0.0,
            created_by_name=str(actor.get("name") or "System"),
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
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="bed",
            entity_id=str(new_bed.id),
            summary=f"Bed {new_bed.bed_code} allocated to admission {admission.id}",
        )
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="patient",
            entity_id=str(admission.patient_id),
            summary="Patient bed allocation updated",
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise _alloc_bed_conflict(
                "Bed was just taken or segment changed concurrently",
                bed_id=payload.bed_id,
                admission_id=admission.id,
                admission_status=admission.status,
            ) from exc
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
        from modules.inpatient.utils.admission_conflicts import (
            admission_conflict as _xfer_conflict,
            bed_conflict as _xfer_bed_conflict,
        )

        # Spec §10: lock admission + old bed + new bed, single commit.
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.hospital_id == hospital_id,
                (Admission.id == payload.admission_id)
                if payload.admission_id
                else (Admission.patient_id == payload.patient_id),
                Admission.status.in_(
                    [AdmissionStatus.admitted, AdmissionStatus.discharge_requested]
                ),
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
            raise _xfer_bed_conflict(
                "Bed is already occupied",
                bed_id=new_bed.id,
                admission_id=admission.id,
                admission_status=admission.status,
            )

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
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="bed",
            entity_id=str(new_bed.id),
            summary=f"Bed {new_bed.bed_code} received transfer of admission {admission.id}",
        )
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="patient",
            entity_id=str(admission.patient_id),
            summary="Patient bed transfer updated",
        )
        if admission.source_appointment_id:
            write_audit_log(
                self.db,
                hospital_id=hospital_id,
                actor=actor,
                action="update",
                entity_type="appointment",
                entity_id=str(admission.source_appointment_id),
                summary="Appointment linkage carried with bed transfer",
            )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise _xfer_bed_conflict(
                "Bed transfer conflicted concurrently; please retry",
                bed_id=payload.to_bed_id,
                admission_id=admission.id,
                admission_status=admission.status,
            ) from exc
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
                Admission.status.in_(
                    [AdmissionStatus.admitted, AdmissionStatus.discharge_requested]
                ),
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
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="patient",
            entity_id=str(admission.patient_id),
            summary="Patient discharge requested",
        )
        if admission.source_appointment_id:
            write_audit_log(
                self.db,
                hospital_id=hospital_id,
                actor=actor,
                action="update",
                entity_type="appointment",
                entity_id=str(admission.source_appointment_id),
                summary="Appointment linkage carried with discharge request",
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
        if rows:
            self.billing_svc.ensure_bed_charges_bulk(hospital_id, rows, now)
        ledgers = self.billing_svc.get_ledger_totals_bulk(
            hospital_id,
            [a.patient_id for a in rows],
            admission_ids=[a.id for a in rows],
        )
        from modules.inpatient.db.discharge_exception_repository import DischargeExceptionRepository
        exc_repo = DischargeExceptionRepository(self.db, hospital_id)
        approved_exc_map = exc_repo.get_active_approved_exceptions_bulk([a.id for a in rows]) if rows else {}

        src_appt_ids = [
            a.source_appointment_id
            for a in rows
            if getattr(a, "source_appointment_id", None) and not getattr(a, "source_appointment", None)
        ]
        src_appt_map: dict[UUID, Any] = {}
        if src_appt_ids:
            from modules.appointments.entities.appointment import Appointment
            appts = self.db.query(Appointment).filter(Appointment.id.in_(src_appt_ids)).all()
            src_appt_map = {appt.id: appt for appt in appts}

        for a in rows:
            fin = ledgers.get(a.patient_id, {})
            outstanding = float(fin.get("outstanding") or 0)
            deposits_avail = float(fin.get("total_deposits_available") or 0)
            net_bal = float(fin.get("net_patient_balance") if fin.get("net_patient_balance") is not None else max(0.0, outstanding - deposits_avail))
            approved_exc = approved_exc_map.get(a.id)
            can_discharge = (net_bal <= 0.009) or (approved_exc is not None)

            base = to_admission_detail(a, source_appointment_map=src_appt_map)
            items.append(
                DischargeQueueItem(
                    **base.model_dump(),
                    total_charges=float(fin.get("total_charges") or 0),
                    total_paid=float(fin.get("total_paid") or 0),
                    total_deposits_available=deposits_avail,
                    outstanding=outstanding,
                    net_patient_balance=net_bal,
                    can_discharge=can_discharge,
                    financial_exception_approved=approved_exc is not None,
                    financial_exception_id=approved_exc.id if approved_exc else None,
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

        # 1. Check required discharge documentation (Section 21.1)
        req_doc = getattr(payload, "require_discharge_summary", None)
        if req_doc is None:
            from modules.tenancy.entities.hospital import Hospital
            hosp = self.db.query(Hospital).filter(Hospital.id == hospital_id).first()
            if hosp and hosp.facility_settings:
                req_doc = bool(hosp.facility_settings.get("require_discharge_summary", False))

        if req_doc:
            from modules.inpatient.entities.admission import IpdFormSubmission, IpdFormSubmissionStatus
            final_summary = (
                self.db.query(IpdFormSubmission)
                .filter(
                    IpdFormSubmission.hospital_id == hospital_id,
                    IpdFormSubmission.admission_id == admission.id,
                    IpdFormSubmission.status == IpdFormSubmissionStatus.final,
                    or_(
                        IpdFormSubmission.form_id.in_(["discharge_summary", "discharge_treatment", "discharge_sheet"]),
                        IpdFormSubmission.form_title.ilike("%discharge%"),
                    ),
                )
                .first()
            )
            if not final_summary:
                raise ValidationError("Discharge documentation incomplete: a finalized discharge summary is required before discharge.")

        # 2. Discharge Prescription / Take-Home Medication Gate (Section 2 & 2.2)
        if getattr(payload, "no_discharge_meds", False):
            admission.no_discharge_meds = True
            if getattr(payload, "no_discharge_meds_reason", None):
                admission.no_discharge_meds_reason = payload.no_discharge_meds_reason
            actor_id = actor.get("doctor_id") or actor.get("id")
            if actor_id:
                try:
                    admission.no_discharge_meds_doctor_id = UUID(str(actor_id))
                except (ValueError, TypeError):
                    pass

        if not admission.no_discharge_meds:
            from modules.clinical_records.entities.clinical_record import Prescription
            has_rx = (
                self.db.query(Prescription)
                .filter(
                    Prescription.hospital_id == hospital_id,
                    Prescription.admission_id == admission.id,
                    Prescription.status.in_(["issued", "dispensed", "completed"]),
                )
                .first()
            )
            if not has_rx:
                raise ValidationError(
                    "Discharge prescription required: Please prescribe discharge medications or explicitly record 'No discharge medicines required' before discharge."
                )

        # 3. Canonical Financial Clearance & Discharge Reconciliation (Phase 4, XMOD-016)
        from modules.billing.services.financial_clearance_service import FinancialClearanceService
        fin_clearance_svc = FinancialClearanceService(self.db)
        fin_clearance_res = fin_clearance_svc.reconcile_and_clear_for_discharge(hospital_id, admission, actor)

        admission.status = AdmissionStatus.discharged
        # Invariant: discharged ⇔ discharged_at set (spec §23).
        admission.discharged_at = discharged_at
        if payload.discharge_notes and payload.discharge_notes.strip():
            admission.discharge_notes = payload.discharge_notes.strip()

        if admission.bed_id:
            locked_bed = (
                self.db.query(Bed).filter(Bed.id == admission.bed_id, Bed.hospital_id == hospital_id).with_for_update().first()
            )
            if locked_bed:
                locked_bed.is_occupied = False
        elif admission.bed:
            admission.bed.is_occupied = False
        if admission.patient:
            admission.patient.status = PatientStatus.active

        # Close any open stay segments at discharge so no open segment survives
        # the episode (Invariant 7) and free associated beds atomically.
        open_segments = (
            self.db.query(BedStaySegment)
            .filter(
                BedStaySegment.hospital_id == hospital_id,
                BedStaySegment.admission_id == admission.id,
                BedStaySegment.ended_at.is_(None),
            )
            .with_for_update()
            .all()
        )
        for open_segment in open_segments:
            open_segment.ended_at = discharged_at
            if open_segment.bed_id and open_segment.bed_id != admission.bed_id:
                seg_bed = (
                    self.db.query(Bed)
                    .filter(Bed.id == open_segment.bed_id, Bed.hospital_id == hospital_id)
                    .with_for_update()
                    .first()
                )
                if seg_bed:
                    seg_bed.is_occupied = False

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Discharged {admission.patient.name if admission.patient else 'patient'} — bed freed",
        )
        if admission.bed_id:
            write_audit_log(
                self.db,
                hospital_id=hospital_id,
                actor=actor,
                action="update",
                entity_type="bed",
                entity_id=str(admission.bed_id),
                summary=f"Bed freed by discharge of admission {admission.id}",
            )
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="patient",
            entity_id=str(admission.patient_id),
            summary="Patient discharged to active",
        )
        if admission.source_appointment_id:
            write_audit_log(
                self.db,
                hospital_id=hospital_id,
                actor=actor,
                action="update",
                entity_type="appointment",
                entity_id=str(admission.source_appointment_id),
                summary="Appointment linkage carried with discharge",
            )

        # Close FinancialAccount and refresh invoice status at successful discharge
        from modules.billing.entities.billing_entities import FinancialAccount, FinancialAccountStatus
        from modules.billing.services.invoice_service import refresh_invoice_paid_status
        acc = (
            self.db.query(FinancialAccount)
            .filter(
                FinancialAccount.hospital_id == hospital_id,
                FinancialAccount.admission_id == admission.id,
            )
            .first()
        )
        if acc:
            if fin_clearance_res.status not in ("exception_approved", "emergency_override"):
                acc.status = FinancialAccountStatus.closed
            refresh_invoice_paid_status(self.db, hospital_id, admission.patient_id)

        self.db.commit()
        refreshed = self.admissions_repo.get_admission_by_id(hospital_id, admission.id)
        return to_admission_detail(refreshed or admission)


class CancelAdmissionAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        admission_id: UUID,
        reason: str | None,
        actor: dict[str, Any],
    ) -> AdmissionDetail:
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.id == admission_id,
                Admission.hospital_id == hospital_id,
            )
            .with_for_update()
            .first()
        )
        if not admission:
            raise NotFoundError("Admission not found")

        if admission.status in (AdmissionStatus.discharged, AdmissionStatus.cancelled):
            raise ValidationError(f"Cannot cancel admission in '{admission.status.value}' state")

        now = datetime.now(timezone.utc)
        admission.status = AdmissionStatus.cancelled
        if reason and reason.strip():
            admission.notes = f"{admission.notes or ''}\nCancelled: {reason.strip()}".strip()

        # Free bed if occupied
        if admission.bed_id:
            locked_bed = (
                self.db.query(Bed).filter(Bed.id == admission.bed_id, Bed.hospital_id == hospital_id).with_for_update().first()
            )
            if locked_bed:
                locked_bed.is_occupied = False
        elif admission.bed:
            admission.bed.is_occupied = False

        # Close all open stay segments and free associated beds atomically
        open_segments = (
            self.db.query(BedStaySegment)
            .filter(
                BedStaySegment.hospital_id == hospital_id,
                BedStaySegment.admission_id == admission.id,
                BedStaySegment.ended_at.is_(None),
            )
            .with_for_update()
            .all()
        )
        for open_segment in open_segments:
            open_segment.ended_at = now
            if open_segment.bed_id and open_segment.bed_id != admission.bed_id:
                seg_bed = (
                    self.db.query(Bed)
                    .filter(Bed.id == open_segment.bed_id, Bed.hospital_id == hospital_id)
                    .with_for_update()
                    .first()
                )
                if seg_bed:
                    seg_bed.is_occupied = False

        # Revert/cancel admission fee and any provisional bed charges
        from modules.billing.entities.billing_entities import BillingSourceType
        from modules.billing.services.charge_reversal_service import cancel_charges_for_source
        cancel_reason = f"Admission cancelled: {reason.strip()}" if reason and reason.strip() else "Admission cancelled"
        for source_type in (BillingSourceType.admission, BillingSourceType.bed):
            cancel_charges_for_source(
                self.db, hospital_id, source_type, admission.id, reason=cancel_reason, actor=actor
            )

        # Restore patient state
        if admission.patient:
            admission.patient.status = PatientStatus.active

        # Revert appointment if applicable
        if admission.source_appointment_id:
            from modules.appointments.entities.appointment import Appointment, AppointmentStatus
            appt = (
                self.db.query(Appointment)
                .filter(Appointment.id == admission.source_appointment_id, Appointment.hospital_id == hospital_id)
                .first()
            )
            if appt and appt.status in (AppointmentStatus.ipd_transfer_requested, AppointmentStatus.transferred_to_inpatient):
                appt.status = AppointmentStatus.scheduled

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Cancelled admission {admission.id} — reason: {reason or 'Not specified'}",
        )

        # Close FinancialAccount on admission cancellation
        from modules.billing.entities.billing_entities import FinancialAccount, FinancialAccountStatus
        acc = (
            self.db.query(FinancialAccount)
            .filter(
                FinancialAccount.hospital_id == hospital_id,
                FinancialAccount.admission_id == admission.id,
            )
            .first()
        )
        if acc:
            acc.status = FinancialAccountStatus.closed

        self.db.commit()
        refreshed = self.admissions_repo.get_admission_by_id(hospital_id, admission.id)
        return to_admission_detail(refreshed or admission)

