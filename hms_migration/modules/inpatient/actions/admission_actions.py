"""
Actions for Inpatient Admissions lifecycle and bed allotment.

Conforms to UltrionTech-Backend-Template modules/inpatient/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.beds.db.beds_repository import BedsRepository
from hms_migration.modules.beds.entities.bed import Bed
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.contracts.inpatient_contracts import (
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
from hms_migration.modules.inpatient.db.admissions_repository import AdmissionsRepository
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.inpatient.services.inpatient_billing_service import (
    InpatientBillingService,
    next_ip_encounter_id,
)
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus
from hms_migration.shared.audit.service import write_audit_log


def to_admission_detail(a: Admission) -> AdmissionDetail:
    """Format Admission entity into AdmissionDetail response contract."""
    ward = a.ward
    return AdmissionDetail(
        id=a.id,
        patient_id=a.patient_id,
        patient_name=a.patient.name if a.patient else None,
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
        status=a.status,
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
            raise HTTPException(status_code=404, detail="Patient not found")

        active = (
            self.db.query(Admission)
            .filter(
                Admission.patient_id == payload.patient_id,
                Admission.hospital_id == hospital_id,
                Admission.status == AdmissionStatus.admitted,
            )
            .first()
        )
        if active:
            raise HTTPException(status_code=409, detail="Patient is already admitted")

        bed = self.beds_repo.get_bed_by_id(hospital_id, payload.bed_id)
        if not bed:
            raise HTTPException(status_code=404, detail="Bed not found")
        if bed.is_occupied:
            raise HTTPException(status_code=409, detail="Bed is already occupied")
        if bed.ward_id != payload.ward_id or bed.room_id != payload.room_id:
            raise HTTPException(status_code=400, detail="Ward/Room does not match selected bed")

        if payload.doctor_id:
            doc = (
                self.db.query(HospitalUser)
                .filter(HospitalUser.id == payload.doctor_id, HospitalUser.hospital_id == hospital_id)
                .first()
            )
            if not doc:
                raise HTTPException(status_code=404, detail="Doctor not found")

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

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Admitted {patient.uhid} {patient.name} ({admission.ip_id})",
        )
        self.db.commit()
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
        admission = self.admissions_repo.get_active_admission(
            hospital_id, payload.admission_id, payload.patient_id
        )
        if not admission:
            raise HTTPException(status_code=404, detail="Active admission not found")

        if admission.bed_id == payload.bed_id:
            return to_admission_detail(admission)

        new_bed = self.beds_repo.get_bed_by_id(hospital_id, payload.bed_id)
        if not new_bed:
            raise HTTPException(status_code=404, detail="Bed not found")
        if new_bed.ward_id != payload.ward_id or new_bed.room_id != payload.room_id:
            raise HTTPException(status_code=400, detail="Ward/Room does not match selected bed")
        if new_bed.is_occupied:
            raise HTTPException(status_code=409, detail="Bed is already occupied")

        old_bed = self.db.query(Bed).filter(Bed.id == admission.bed_id).first()
        if old_bed:
            old_bed.is_occupied = False

        admission.ward_id = payload.ward_id
        admission.room_id = payload.room_id
        admission.bed_id = payload.bed_id
        new_bed.is_occupied = True

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Allocated bed {new_bed.bed_code} to {admission.patient.name if admission.patient else 'patient'}",
        )
        self.db.commit()
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
        admission = self.admissions_repo.get_active_admission(
            hospital_id, payload.admission_id, payload.patient_id
        )
        if not admission:
            raise HTTPException(status_code=404, detail="Active admission not found")

        if admission.bed_id == payload.to_bed_id:
            raise HTTPException(status_code=400, detail="Patient is already on this bed")

        new_bed = self.beds_repo.get_bed_by_id(hospital_id, payload.to_bed_id)
        if not new_bed:
            raise HTTPException(status_code=404, detail="Bed not found")
        if new_bed.ward_id != payload.to_ward_id or new_bed.room_id != payload.to_room_id:
            raise HTTPException(status_code=400, detail="Ward/Room does not match selected bed")
        if new_bed.is_occupied:
            raise HTTPException(status_code=409, detail="Bed is already occupied")

        old_bed = self.db.query(Bed).filter(Bed.id == admission.bed_id).first()
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

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Transferred {admission.patient.name if admission.patient else 'patient'}: {from_label} → {to_label}",
        )
        self.db.commit()
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
        admission = self.admissions_repo.get_active_admission(
            hospital_id, payload.admission_id, payload.patient_id
        )
        if not admission:
            raise HTTPException(status_code=404, detail="Active admission not found")

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
            fin = self.billing_svc.get_ledger_totals(hospital_id, a.patient_id)
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
        admission = self.admissions_repo.get_active_admission(
            hospital_id,
            payload.admission_id,
            payload.patient_id,
            statuses=(AdmissionStatus.discharge_requested,),
        )
        if not admission:
            raise HTTPException(status_code=404, detail="Active admission not found")

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
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot discharge — outstanding balance ₹{outstanding:,.2f}. Clear all dues before discharging.",
            )

        admission.status = AdmissionStatus.discharged
        admission.discharged_at = discharged_at
        if payload.discharge_notes and payload.discharge_notes.strip():
            admission.discharge_notes = payload.discharge_notes.strip()

        if admission.bed:
            admission.bed.is_occupied = False
        if admission.patient:
            admission.patient.status = PatientStatus.active

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

