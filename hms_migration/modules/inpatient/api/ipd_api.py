"""
FastAPI router for IPD Form Submissions under /ipd.

Conforms to UltrionTech-Backend-Template modules/inpatient/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.inpatient.actions.ipd_actions import (
    CreateFormSubmissionAction,
    GetFormSubmissionAction,
    ListFormSubmissionsAction,
    UpdateFormSubmissionAction,
    ViewFormSubmissionHtmlAction,
)
from hms_migration.modules.inpatient.contracts.inpatient_contracts import (
    IpdFormSubmissionCreate,
    IpdFormSubmissionResponse,
    IpdFormSubmissionUpdate,
)
from hms_migration.modules.inpatient.entities.admission import IpdFormSubmissionStatus
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/ipd", tags=["ipd"])


@router.post(
    "/form-submissions",
    response_model=IpdFormSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_form_submission(
    payload: IpdFormSubmissionCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return CreateFormSubmissionAction(db).execute(hospital_id, payload, user)


@router.put("/form-submissions/{submission_id}", response_model=IpdFormSubmissionResponse)
def update_form_submission(
    submission_id: UUID,
    payload: IpdFormSubmissionUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return UpdateFormSubmissionAction(db).execute(hospital_id, submission_id, payload, user)


@router.get("/form-submissions", response_model=list[IpdFormSubmissionResponse])
def list_form_submissions(
    patient_id: UUID | None = Query(default=None),
    admission_id: UUID | None = Query(default=None),
    form_id: str | None = Query(default=None),
    status_filter: IpdFormSubmissionStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListFormSubmissionsAction(db).execute(
        hospital_id, patient_id, admission_id, form_id, status_filter
    )


@router.get("/form-submissions/{submission_id}", response_model=IpdFormSubmissionResponse)
def get_form_submission(
    submission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetFormSubmissionAction(db).execute(hospital_id, submission_id)


@router.get("/form-submissions/{submission_id}/view")
def view_form_submission_html(
    submission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ViewFormSubmissionHtmlAction(db).execute(hospital_id, submission_id)
