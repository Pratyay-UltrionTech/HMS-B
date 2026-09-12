"""
Database repository for Patient Allergies (Feature 16).

Conforms to UltrionTech-Backend-Template modules/patients/db/ specification.
All operations scoped strictly to hospital_id.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from hms_migration.modules.patients.entities.allergy import PatientAllergy


class AllergyRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_allergy_by_id(self, allergy_id: UUID) -> PatientAllergy | None:
        return (
            self.db.query(PatientAllergy)
            .filter(
                PatientAllergy.id == allergy_id,
                PatientAllergy.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_active_allergies(self, patient_id: UUID) -> list[PatientAllergy]:
        return (
            self.db.query(PatientAllergy)
            .filter(
                PatientAllergy.patient_id == patient_id,
                PatientAllergy.hospital_id == self.hospital_id,
                PatientAllergy.is_active.is_(True),
            )
            .order_by(PatientAllergy.created_at.desc())
            .all()
        )

    def list_all_allergies(self, patient_id: UUID) -> list[PatientAllergy]:
        return (
            self.db.query(PatientAllergy)
            .filter(
                PatientAllergy.patient_id == patient_id,
                PatientAllergy.hospital_id == self.hospital_id,
            )
            .order_by(PatientAllergy.created_at.desc())
            .all()
        )

    def add(self, entity: object) -> None:
        self.db.add(entity)

    def commit(self) -> None:
        self.db.commit()

    def refresh(self, entity: object) -> None:
        self.db.refresh(entity)
