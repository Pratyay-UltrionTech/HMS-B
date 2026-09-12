"""
Database repository for Nursing Care Plans, Merged Clinical Notes, and Shift Handovers.

Conforms to UltrionTech-Backend-Template modules/inpatient/db/ specification.
All operations scoped strictly to hospital_id.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.inpatient.entities.nursing_entities import (
    ClinicalNoteType,
    IpdClinicalNote,
    MedicationAdminStatus,
    MedicationAdministrationRecord,
    NursingCarePlan,
    NursingShiftHandover,
)


class NursingRepository:
    """Encapsulated data access for nursing domain entities."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    # --- Feature 12: Nursing Care Plan ---
    def get_care_plan_by_id(self, plan_id: UUID) -> NursingCarePlan | None:
        return (
            self.db.query(NursingCarePlan)
            .filter(
                NursingCarePlan.id == plan_id,
                NursingCarePlan.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_care_plans(self, admission_id: UUID) -> list[NursingCarePlan]:
        return (
            self.db.query(NursingCarePlan)
            .filter(
                NursingCarePlan.admission_id == admission_id,
                NursingCarePlan.hospital_id == self.hospital_id,
            )
            .order_by(NursingCarePlan.created_at.desc())
            .all()
        )

    # --- Feature 13: Merged IPD Clinical Notes ---
    def get_clinical_note_by_id(self, note_id: UUID) -> IpdClinicalNote | None:
        return (
            self.db.query(IpdClinicalNote)
            .filter(
                IpdClinicalNote.id == note_id,
                IpdClinicalNote.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_clinical_notes(
        self,
        admission_id: UUID,
        note_types: Sequence[ClinicalNoteType] | None = None,
    ) -> list[IpdClinicalNote]:
        q = (
            self.db.query(IpdClinicalNote)
            .filter(
                IpdClinicalNote.admission_id == admission_id,
                IpdClinicalNote.hospital_id == self.hospital_id,
            )
        )
        if note_types:
            q = q.filter(IpdClinicalNote.note_type.in_(note_types))
        return q.order_by(IpdClinicalNote.created_at.desc()).all()

    # --- Feature 14: Shift Handovers ---
    def get_handover_by_id(self, handover_id: UUID) -> NursingShiftHandover | None:
        return (
            self.db.query(NursingShiftHandover)
            .filter(
                NursingShiftHandover.id == handover_id,
                NursingShiftHandover.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_handovers(self, admission_id: UUID) -> list[NursingShiftHandover]:
        return (
            self.db.query(NursingShiftHandover)
            .filter(
                NursingShiftHandover.admission_id == admission_id,
                NursingShiftHandover.hospital_id == self.hospital_id,
            )
            .order_by(NursingShiftHandover.created_at.desc())
            .all()
        )

    # --- Feature 15: Electronic Medication Administration Record (eMAR) ---
    def get_emar_record_by_id(self, record_id: UUID) -> MedicationAdministrationRecord | None:
        return (
            self.db.query(MedicationAdministrationRecord)
            .filter(
                MedicationAdministrationRecord.id == record_id,
                MedicationAdministrationRecord.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_emar_records(
        self,
        admission_id: UUID,
        status: MedicationAdminStatus | None = None,
    ) -> list[MedicationAdministrationRecord]:
        q = (
            self.db.query(MedicationAdministrationRecord)
            .filter(
                MedicationAdministrationRecord.admission_id == admission_id,
                MedicationAdministrationRecord.hospital_id == self.hospital_id,
            )
        )
        if status:
            q = q.filter(MedicationAdministrationRecord.status == status)
        return q.order_by(MedicationAdministrationRecord.scheduled_time.asc()).all()

    def add(self, entity: object) -> None:
        self.db.add(entity)

    def commit(self) -> None:
        self.db.commit()

    def flush(self) -> None:
        self.db.flush()

    def refresh(self, entity: object) -> None:
        self.db.refresh(entity)
