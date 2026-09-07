"""
Data access repository for the Patient domain.

Conforms to UltrionTech-Backend-Template modules/patients/db/ specification.
Handles database queries, multi-tenant isolation, UHID generation,
and persistence for the independent Patient entity.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.base_repository import BaseRepository
from hms_migration.modules.patients.db.profile_reader import PatientProfileReader
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus


class PatientRepository(BaseRepository[Patient]):
    """Repository handling SQL queries and persistence for Patient domain records."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        super().__init__(session=db, hospital_id=hospital_id)
        self.db = db
        self.profile_reader = PatientProfileReader(db=db, hospital_id=hospital_id)

    def get_by_id(self, patient_id: UUID | str) -> Patient | None:
        """Fetch a patient by primary key, strictly scoped to this hospital tenant."""
        pid = patient_id if isinstance(patient_id, UUID) else UUID(str(patient_id))
        return (
            self.db.query(Patient)
            .filter(
                Patient.id == pid,
                Patient.hospital_id == self.hospital_id,
            )
            .first()
        )

    def find_by_mobile(self, mobile: str, exclude_id: UUID | str | None = None) -> Patient | None:
        """Find an existing patient with the given mobile within the hospital tenant."""
        q = self.db.query(Patient).filter(
            Patient.hospital_id == self.hospital_id,
            Patient.mobile == mobile,
        )
        if exclude_id is not None:
            ex_id = exclude_id if isinstance(exclude_id, UUID) else UUID(str(exclude_id))
            q = q.filter(Patient.id != ex_id)
        return q.first()

    def find_by_uhid(self, uhid: str) -> Patient | None:
        """Find an existing patient with the given UHID within the hospital tenant."""
        return (
            self.db.query(Patient)
            .filter(
                Patient.hospital_id == self.hospital_id,
                Patient.uhid == uhid,
            )
            .first()
        )

    def generate_next_uhid(self) -> str:
        """Generate the next unique sequential UHID for this hospital tenant (P0001 format)."""
        count = (
            self.db.query(func.count(Patient.id))
            .filter(Patient.hospital_id == self.hospital_id)
            .scalar()
            or 0
        )
        for i in range(1, 100_000):
            uhid = f"P{count + i:04d}"
            exists = (
                self.db.query(Patient.id)
                .filter(
                    Patient.hospital_id == self.hospital_id,
                    Patient.uhid == uhid,
                )
                .first()
            )
            if not exists:
                return uhid
        raise HTTPException(status_code=500, detail="Unable to generate UHID")

    def list_patients(
        self,
        search: str | None = None,
        status_filter: PatientStatus | None = None,
    ) -> list[Patient]:
        """List patients filtered by tenant, status, and search string ordered by created_at desc."""
        q = self.db.query(Patient).filter(Patient.hospital_id == self.hospital_id)
        if status_filter:
            q = q.filter(Patient.status == status_filter)
        if search:
            term = f"%{search.strip()}%"
            q = q.filter(
                or_(
                    Patient.name.ilike(term),
                    Patient.uhid.ilike(term),
                    Patient.mobile.ilike(term),
                    Patient.first_name.ilike(term),
                    Patient.last_name.ilike(term),
                )
            )
        return q.order_by(Patient.created_at.desc()).all()
