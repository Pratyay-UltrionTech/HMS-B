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

from infrastructure.postgres.base_repository import BaseRepository
from modules.patients.db.profile_reader import PatientProfileReader
from modules.patients.entities.patient import Patient, PatientStatus
from shared.database.sequences import next_uhid


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
        """Generate the next unique sequential UHID for this hospital tenant (P0001 format).

        Atomic: advances a tenant-scoped counter in a single upsert statement
        (HMS-FLAW-028), so concurrent registrations cannot collide.
        """
        return next_uhid(self.db, self.hospital_id)

    def soft_delete_patient(self, patient: Patient) -> Patient:
        """Soft-delete a patient, anonymizing PII while preserving financial/clinical records.

        The patient row is retained (with ``is_deleted=True``) so every
        referenced invoice, receipt, payment, prescription, medical record and
        admission stays referentially intact (HMS-FLAW-015). PII is anonymized
        to honour erasure/privacy while keeping the audit-linked records valid.
        """
        patient.is_deleted = True
        patient.status = PatientStatus.inactive
        patient.first_name = "Deleted"
        patient.last_name = "Patient"
        patient.name = "Deleted Patient"
        patient.mobile = f"deleted-{str(patient.id)[:8]}"
        patient.email = None
        patient.address = None
        patient.emergency_contact = None
        patient.emergency_contact_name = None
        patient.emergency_contact_relation = None
        return patient

    def list_patients(
        self,
        search: str | None = None,
        status_filter: PatientStatus | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Patient]:
        """List patients filtered by tenant, status, and search string ordered by created_at desc.

        Bounded by limit/offset: an unbounded .all() here previously pulled every
        patient row on every directory load, which is why the screen got slower
        as the hospital's patient count grew.
        """
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
        return q.order_by(Patient.created_at.desc()).limit(limit).offset(offset).all()
