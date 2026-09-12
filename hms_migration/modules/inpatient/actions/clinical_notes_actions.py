"""
Actions for Feature 13: Merged IPD Clinical Notes.

Integrates nursing shift progress notes and doctor notes into the canonical IPD clinical record.
Conforms to UltrionTech-Backend-Template modules/inpatient/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any, Sequence
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.inpatient.contracts.nursing_contracts import (
    IpdClinicalNoteCreate,
    IpdClinicalNoteResponse,
)
from hms_migration.modules.inpatient.db.admissions_repository import AdmissionsRepository
from hms_migration.modules.inpatient.db.nursing_repository import NursingRepository
from hms_migration.modules.inpatient.entities.nursing_entities import (
    ClinicalNoteType,
    IpdClinicalNote,
)
from hms_migration.shared.audit.service import write_audit_log


class CreateIpdClinicalNoteAction:
    """Record a clinical progress note (nursing note, doctor note, or shift summary)."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.adm_repo = AdmissionsRepository(db)
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(
        self,
        admission_id: UUID,
        payload: IpdClinicalNoteCreate,
        actor: dict[str, Any],
    ) -> IpdClinicalNoteResponse:
        adm = self.adm_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not adm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        author_name = str(actor.get("name") or "Clinical Staff")
        author_role = str(actor.get("staff_role_name") or actor.get("role") or "nurse")
        author_id_raw = actor.get("user_id")
        author_id = UUID(str(author_id_raw)) if author_id_raw else None

        note = IpdClinicalNote(
            hospital_id=self.hospital_id,
            admission_id=admission_id,
            patient_id=adm.patient_id,
            author_id=author_id,
            author_name=author_name,
            author_role=author_role,
            note_type=payload.note_type,
            shift_type=payload.shift_type,
            subjective=payload.subjective.strip() if payload.subjective else None,
            objective_vitals=payload.objective_vitals or {},
            assessment=payload.assessment.strip() if payload.assessment else None,
            plan=payload.plan.strip() if payload.plan else None,
            note_content=payload.note_content.strip(),
            signature_data=payload.signature_data,
        )
        self.nursing_repo.add(note)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="create_clinical_note",
            entity_type="ipd_clinical_note",
            entity_id=note.id,
            summary=f"Recorded {payload.note_type.value} for admission {admission_id} by {author_name}",
            details={"note_type": payload.note_type.value, "author_role": author_role},
        )
        self.nursing_repo.commit()
        self.nursing_repo.refresh(note)
        return IpdClinicalNoteResponse.model_validate(note)


class ListIpdClinicalNotesAction:
    """Retrieve chronological clinical notes (doctor + nursing) for an admission."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(
        self,
        admission_id: UUID,
        note_type: ClinicalNoteType | None = None,
        note_types: Sequence[ClinicalNoteType] | None = None,
    ) -> list[IpdClinicalNoteResponse]:
        filters = list(note_types) if note_types else []
        if note_type and note_type not in filters:
            filters.append(note_type)
        notes = self.nursing_repo.list_clinical_notes(admission_id, filters if filters else None)
        return [IpdClinicalNoteResponse.model_validate(n) for n in notes]


class GetIpdClinicalNoteAction:
    """Retrieve single clinical note by ID."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(self, note_id: UUID) -> IpdClinicalNoteResponse:
        note = self.nursing_repo.get_clinical_note_by_id(note_id)
        if not note:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clinical note not found")
        return IpdClinicalNoteResponse.model_validate(note)


CreateClinicalNoteAction = CreateIpdClinicalNoteAction
ListClinicalNotesAction = ListIpdClinicalNotesAction
GetClinicalNoteAction = GetIpdClinicalNoteAction
