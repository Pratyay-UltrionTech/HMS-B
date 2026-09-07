"""
Database repository for Inpatient Admissions.

Conforms to UltrionTech-Backend-Template modules/inpatient/db/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.beds.entities.bed import Bed
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus


class AdmissionsRepository:
    """Repository handling database operations for Inpatient Admissions."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_admission_by_id(
        self,
        hospital_id: UUID,
        admission_id: UUID,
        statuses: Sequence[AdmissionStatus] | None = None,
    ) -> Admission | None:
        """Fetch admission by id, optionally filtering by statuses."""
        q = (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(Admission.id == admission_id, Admission.hospital_id == hospital_id)
        )
        if statuses:
            q = q.filter(Admission.status.in_(statuses))
        return q.first()

    def get_admission_by_patient_id(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        statuses: Sequence[AdmissionStatus] | None = None,
    ) -> Admission | None:
        """Fetch admission by patient_id, optionally filtering by statuses."""
        q = (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(Admission.patient_id == patient_id, Admission.hospital_id == hospital_id)
        )
        if statuses:
            q = q.filter(Admission.status.in_(statuses))
        return q.first()

    def get_active_admission(
        self,
        hospital_id: UUID,
        admission_id: UUID | None,
        patient_id: UUID | None,
        statuses: Sequence[AdmissionStatus] = (AdmissionStatus.admitted,),
    ) -> Admission | None:
        """Resolve active admission by admission_id or patient_id."""
        if admission_id:
            return self.get_admission_by_id(hospital_id, admission_id, statuses)
        if patient_id:
            return self.get_admission_by_patient_id(hospital_id, patient_id, statuses)
        return None

    def list_active_admissions(
        self,
        hospital_id: UUID,
        search: str | None = None,
        doctor_id: UUID | None = None,
    ) -> list[Admission]:
        """List currently active or discharge-requested admissions."""
        q = (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(
                Admission.hospital_id == hospital_id,
                Admission.status.in_([AdmissionStatus.admitted, AdmissionStatus.discharge_requested]),
            )
        )
        if doctor_id:
            q = q.filter(Admission.doctor_id == doctor_id)
        if search and search.strip():
            term = f"%{search.strip()}%"
            q = q.filter(
                Admission.patient_id.in_(
                    self.db.query(Patient.id).filter(
                        Patient.hospital_id == hospital_id,
                        or_(
                            Patient.name.ilike(term),
                            Patient.uhid.ilike(term),
                            Patient.mobile.ilike(term),
                        ),
                    )
                )
            )
        return q.order_by(Admission.admitted_at.desc()).all()

    def list_discharge_requests(self, hospital_id: UUID) -> list[Admission]:
        """List admissions awaiting nurse discharge completion."""
        return (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(
                Admission.hospital_id == hospital_id,
                Admission.status == AdmissionStatus.discharge_requested,
            )
            .order_by(Admission.admitted_at.asc())
            .all()
        )

    def create_admission(
        self,
        hospital_id: UUID,
        patient: Patient,
        bed: Bed,
        ward_id: UUID,
        room_id: UUID,
        doctor_id: UUID | None,
        ip_id: str,
        notes: str | None = None,
        admitted_at: datetime | None = None,
        source_appointment_id: UUID | None = None,
    ) -> Admission:
        """Create new admission and set bed and patient occupancy state."""
        admission = Admission(
            hospital_id=hospital_id,
            patient_id=patient.id,
            ward_id=ward_id,
            room_id=room_id,
            bed_id=bed.id,
            doctor_id=doctor_id,
            status=AdmissionStatus.admitted,
            notes=notes.strip() if notes else None,
            admitted_at=admitted_at or datetime.now(timezone.utc),
            ip_id=ip_id,
            source_appointment_id=source_appointment_id,
        )
        bed.is_occupied = True
        patient.status = PatientStatus.admitted
        self.db.add(admission)
        self.db.flush()
        return admission
