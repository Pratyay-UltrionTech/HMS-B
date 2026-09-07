"""
Actions for IPD Clinical / Consent Form Submissions.

Conforms to UltrionTech-Backend-Template modules/inpatient/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from hms_migration.modules.inpatient.contracts.inpatient_contracts import (
    IpdFormSubmissionCreate,
    IpdFormSubmissionResponse,
    IpdFormSubmissionUpdate,
)
from hms_migration.modules.inpatient.db.ipd_submissions_repository import IpdSubmissionsRepository
from hms_migration.modules.inpatient.entities.admission import (
    Admission,
    IpdFormSubmission,
    IpdFormSubmissionStatus,
)
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.shared.audit.service import write_audit_log


def to_form_response(sub: IpdFormSubmission) -> IpdFormSubmissionResponse:
    return IpdFormSubmissionResponse(
        id=sub.id,
        hospital_id=sub.hospital_id,
        patient_id=sub.patient_id,
        admission_id=sub.admission_id,
        form_id=sub.form_id,
        form_title=sub.form_title,
        form_data=sub.form_data or {},
        status=sub.status,
        filled_by_id=sub.filled_by_id,
        filled_by_name=sub.filled_by_name,
        filled_by_role=sub.filled_by_role,
        medical_record_id=sub.medical_record_id,
        patient_document_id=sub.patient_document_id,
        has_html_snapshot=bool(sub.html_snapshot),
        created_at=sub.created_at,
        updated_at=sub.updated_at,
        patient_name=sub.patient.name if sub.patient else None,
        patient_uhid=sub.patient.uhid if sub.patient else None,
    )


def resolve_actor_id(user: dict[str, Any]) -> UUID | None:
    raw = user.get("user_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


class CreateFormSubmissionAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = IpdSubmissionsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        payload: IpdFormSubmissionCreate,
        actor: dict[str, Any],
    ) -> IpdFormSubmissionResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        if payload.admission_id:
            adm = (
                self.db.query(Admission)
                .filter(
                    Admission.id == payload.admission_id,
                    Admission.hospital_id == hospital_id,
                    Admission.patient_id == payload.patient_id,
                )
                .first()
            )
            if not adm:
                raise HTTPException(status_code=400, detail="Invalid admission for patient")

        actor_id = resolve_actor_id(actor)
        actor_name = str(actor.get("name") or actor.get("sub") or "Staff")
        actor_role = str(actor.get("staff_role_name") or actor.get("role") or "")

        sub = IpdFormSubmission(
            hospital_id=hospital_id,
            patient_id=payload.patient_id,
            admission_id=payload.admission_id,
            form_id=payload.form_id.strip(),
            form_title=payload.form_title.strip(),
            form_data=payload.form_data or {},
            html_snapshot=payload.html_snapshot,
            status=payload.status,
            filled_by_id=actor_id,
            filled_by_name=actor_name,
            filled_by_role=actor_role,
        )
        self.db.add(sub)
        self.db.flush()

        if sub.status == IpdFormSubmissionStatus.final:
            self.repo.sync_final_to_patient_record(sub, actor_id)

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="ipd_form_submission",
            entity_id=str(sub.id),
            summary=f"Saved IPD form '{sub.form_title}' ({sub.status.value}) for patient",
            details={"form_id": sub.form_id, "patient_id": str(sub.patient_id)},
        )
        self.db.commit()
        refreshed = self.repo.get_by_id(hospital_id, sub.id)
        return to_form_response(refreshed or sub)


class UpdateFormSubmissionAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = IpdSubmissionsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        submission_id: UUID,
        payload: IpdFormSubmissionUpdate,
        actor: dict[str, Any],
    ) -> IpdFormSubmissionResponse:
        sub = self.repo.get_by_id(hospital_id, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Submission not found")

        if payload.admission_id is not None:
            adm = (
                self.db.query(Admission)
                .filter(
                    Admission.id == payload.admission_id,
                    Admission.hospital_id == hospital_id,
                    Admission.patient_id == sub.patient_id,
                )
                .first()
            )
            if not adm:
                raise HTTPException(status_code=400, detail="Invalid admission for patient")
            sub.admission_id = payload.admission_id

        if payload.form_data is not None:
            sub.form_data = payload.form_data
        if payload.html_snapshot is not None:
            sub.html_snapshot = payload.html_snapshot
        if payload.form_title is not None:
            sub.form_title = payload.form_title.strip()
        if payload.status is not None:
            sub.status = payload.status

        actor_id = resolve_actor_id(actor)
        sub.filled_by_id = actor_id or sub.filled_by_id
        sub.filled_by_name = str(actor.get("name") or actor.get("sub") or "Staff")
        sub.filled_by_role = str(actor.get("staff_role_name") or actor.get("role") or "")
        sub.updated_at = datetime.now(timezone.utc)

        if sub.status == IpdFormSubmissionStatus.final:
            self.repo.sync_final_to_patient_record(sub, actor_id)

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="ipd_form_submission",
            entity_id=str(sub.id),
            summary=f"Updated IPD form '{sub.form_title}' ({sub.status.value})",
            details={"form_id": sub.form_id, "patient_id": str(sub.patient_id)},
        )
        self.db.commit()
        refreshed = self.repo.get_by_id(hospital_id, sub.id)
        return to_form_response(refreshed or sub)


class ListFormSubmissionsAction:
    def __init__(self, db: Session) -> None:
        self.repo = IpdSubmissionsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        patient_id: UUID | None = None,
        admission_id: UUID | None = None,
        form_id: str | None = None,
        status_filter: IpdFormSubmissionStatus | None = None,
    ) -> list[IpdFormSubmissionResponse]:
        rows = self.repo.list_submissions(
            hospital_id, patient_id, admission_id, form_id, status_filter
        )
        return [to_form_response(r) for r in rows]


class GetFormSubmissionAction:
    def __init__(self, db: Session) -> None:
        self.repo = IpdSubmissionsRepository(db)

    def execute(self, hospital_id: UUID, submission_id: UUID) -> IpdFormSubmissionResponse:
        sub = self.repo.get_by_id(hospital_id, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Submission not found")
        return to_form_response(sub)


class ViewFormSubmissionHtmlAction:
    def __init__(self, db: Session) -> None:
        self.repo = IpdSubmissionsRepository(db)

    def execute(self, hospital_id: UUID, submission_id: UUID) -> StreamingResponse:
        sub = self.repo.get_by_id(hospital_id, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Submission not found")
        if not sub.html_snapshot:
            raise HTTPException(status_code=404, detail="No HTML snapshot for this form")
        patient_label = sub.patient.uhid if sub.patient else "patient"
        return StreamingResponse(
            BytesIO(sub.html_snapshot.encode("utf-8")),
            media_type="text/html",
            headers={"Content-Disposition": f'inline; filename="{sub.form_id}-{patient_label}.html"'},
        )
