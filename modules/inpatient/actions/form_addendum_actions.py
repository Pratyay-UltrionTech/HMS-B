"""Actions for IPD Form Addenda."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.inpatient.contracts.inpatient_contracts import (
    IpdFormAddendumCreate,
    IpdFormAddendumResponse,
)
from modules.inpatient.db.ipd_submissions_repository import IpdSubmissionsRepository
from modules.inpatient.entities.admission import IpdFormSubmissionStatus
from modules.inpatient.entities.form_addendum import IpdFormAddendum
from shared.audit.service import write_audit_log
from shared.exceptions.base import NotFoundError, ValidationError


def to_addendum_response(addendum: IpdFormAddendum) -> IpdFormAddendumResponse:
    return IpdFormAddendumResponse(
        id=addendum.id,
        hospital_id=addendum.hospital_id,
        submission_id=addendum.submission_id,
        admission_id=addendum.admission_id,
        patient_id=addendum.patient_id,
        author_id=addendum.author_id,
        author_name=addendum.author_name,
        author_role=addendum.author_role,
        reason=addendum.reason,
        content=addendum.content,
        created_at=addendum.created_at,
    )


class CreateFormAddendumAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = IpdSubmissionsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        submission_id: UUID,
        payload: IpdFormAddendumCreate,
        actor: dict[str, Any],
    ) -> IpdFormAddendumResponse:
        sub = self.repo.get_by_id(hospital_id, submission_id)
        if not sub:
            raise NotFoundError("IPD form submission not found")

        if sub.status != IpdFormSubmissionStatus.final:
            raise ValidationError(
                "Addenda can only be appended to finalized clinical forms. Draft forms should be edited directly."
            )

        actor_id_raw = actor.get("id") or actor.get("sub") or actor.get("user_id")
        if not actor_id_raw:
            raise ValidationError("Valid authenticated author required to sign addendum")
        author_id = UUID(str(actor_id_raw))
        author_name = str(actor.get("name") or actor.get("email") or "Clinician")
        author_role = str(actor.get("role") or "Clinician")

        addendum = self.repo.create_addendum(
            hospital_id=hospital_id,
            submission_id=submission_id,
            admission_id=sub.admission_id,
            patient_id=sub.patient_id,
            author_id=author_id,
            author_name=author_name,
            author_role=author_role,
            reason=payload.reason.strip(),
            content=payload.content.strip(),
        )
        self.db.commit()

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="ipd_form_addendum",
            entity_id=str(addendum.id),
            summary=f"Added addendum to finalized form '{sub.form_title}': {addendum.reason}",
            details={"submission_id": str(submission_id), "admission_id": str(sub.admission_id) if sub.admission_id else None},
        )
        self.db.commit()
        return to_addendum_response(addendum)


class ListFormAddendaAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = IpdSubmissionsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        submission_id: UUID,
    ) -> list[IpdFormAddendumResponse]:
        sub = self.repo.get_by_id(hospital_id, submission_id)
        if not sub:
            raise NotFoundError("IPD form submission not found")

        rows = self.repo.list_addenda(hospital_id, submission_id)
        return [to_addendum_response(r) for r in rows]
