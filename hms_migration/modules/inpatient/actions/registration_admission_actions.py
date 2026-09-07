"""
Registration Inpatient admission and discharge actions.

Separated from admission_actions.py to conform to the UltrionTech-Backend-Template
rule that every file must remain strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.beds.db.beds_repository import BedsRepository
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.contracts.inpatient_contracts import (
    AdmissionSummary,
    AdmitPatientRequest,
    DischargeResponse,
)
from hms_migration.modules.inpatient.db.admissions_repository import AdmissionsRepository
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.inpatient.services.inpatient_billing_service import (
    InpatientBillingService,
    next_ip_encounter_id,
)
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus
from hms_migration.shared.audit.service import write_audit_log


class RegistrationAdmitAction:
    """Action for /api/registration/patients/{patient_id}/admit endpoint."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.beds_repo = BedsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        payload: AdmitPatientRequest,
        actor: dict[str, Any],
    ) -> AdmissionSummary:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        active = (
            self.db.query(Admission)
            .filter(
                Admission.patient_id == patient_id,
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
        adm = refreshed or admission
        return AdmissionSummary(
            id=adm.id,
            ward_id=adm.ward_id,
            room_id=adm.room_id,
            bed_id=adm.bed_id,
            ward_name=adm.ward.name if adm.ward else None,
            room_code=adm.room.room_code if adm.room else None,
            bed_code=adm.bed.bed_code if adm.bed else None,
            doctor_name=adm.doctor.name if adm.doctor else None,
            status=adm.status,
            admitted_at=adm.admitted_at,
            discharged_at=adm.discharged_at,
            notes=adm.notes,
            ip_id=getattr(adm, "ip_id", None),
        )


class RegistrationDischargeAction:
    """Action for /api/registration/admissions/{admission_id}/discharge endpoint."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.admissions_repo = AdmissionsRepository(db)
        self.billing_svc = InpatientBillingService(db)

    def execute(
        self,
        hospital_id: UUID,
        admission_id: UUID,
        actor: dict[str, Any],
    ) -> DischargeResponse:
        admission = self.admissions_repo.get_admission_by_id(hospital_id, admission_id)
        if not admission:
            raise HTTPException(status_code=404, detail="Admission not found")
        if admission.status != AdmissionStatus.admitted:
            raise HTTPException(status_code=400, detail="Admission already discharged")

        now = datetime.now(timezone.utc)
        admission.status = AdmissionStatus.discharged
        admission.discharged_at = now
        if admission.bed:
            admission.bed.is_occupied = False
        if admission.patient:
            admission.patient.status = PatientStatus.active

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
            summary=f"Discharged patient admission {admission_id}",
        )
        self.db.commit()
        return DischargeResponse(
            id=admission.id,
            status=admission.status,
            discharged_at=admission.discharged_at,
        )
