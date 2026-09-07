"""
Database repository for Prescriptions and Medical Records.

Conforms to UltrionTech-Backend-Template modules/clinical_records/db/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    Prescription,
)


class ClinicalRecordsRepository:
    """Repository handling database queries for prescriptions and medical records."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_prescription(
        self, hospital_id: UUID, doctor_id: UUID, prescription_id: UUID
    ) -> Prescription | None:
        """Fetch prescription by id for a doctor."""
        return (
            self.db.query(Prescription)
            .options(joinedload(Prescription.patient), joinedload(Prescription.doctor))
            .filter(
                Prescription.id == prescription_id,
                Prescription.doctor_id == doctor_id,
                Prescription.hospital_id == hospital_id,
            )
            .first()
        )

    def list_prescriptions(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        patient_id: UUID | None = None,
    ) -> list[Prescription]:
        """List prescriptions for a doctor, optionally filtered by patient."""
        q = (
            self.db.query(Prescription)
            .options(joinedload(Prescription.patient), joinedload(Prescription.doctor))
            .filter(
                Prescription.doctor_id == doctor_id,
                Prescription.hospital_id == hospital_id,
            )
        )
        if patient_id:
            q = q.filter(Prescription.patient_id == patient_id)
        return q.order_by(Prescription.created_at.desc()).all()

    def get_medical_record(
        self, hospital_id: UUID, doctor_id: UUID, record_id: UUID
    ) -> MedicalRecord | None:
        """Fetch medical record by id for a doctor."""
        return (
            self.db.query(MedicalRecord)
            .options(joinedload(MedicalRecord.patient), joinedload(MedicalRecord.doctor))
            .filter(
                MedicalRecord.id == record_id,
                MedicalRecord.doctor_id == doctor_id,
                MedicalRecord.hospital_id == hospital_id,
            )
            .first()
        )

    def list_medical_records(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        patient_id: UUID | None = None,
    ) -> list[MedicalRecord]:
        """List medical records for a doctor, optionally filtered by patient."""
        q = (
            self.db.query(MedicalRecord)
            .options(joinedload(MedicalRecord.patient), joinedload(MedicalRecord.doctor))
            .filter(
                MedicalRecord.doctor_id == doctor_id,
                MedicalRecord.hospital_id == hospital_id,
            )
        )
        if patient_id:
            q = q.filter(MedicalRecord.patient_id == patient_id)
        return q.order_by(MedicalRecord.created_at.desc()).all()
