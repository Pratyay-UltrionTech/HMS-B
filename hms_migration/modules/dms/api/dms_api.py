"""
DMS domain API router.

Exposes all 9 document and electronic patient file management endpoints matching HMS-B behavior.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.dms.actions.dms_actions import (
    DeleteDmsDocumentAction,
    DownloadDmsFileAction,
    DownloadMedicalRecordFileAction,
    GetCompletePatientFileHtmlAction,
    GetDmsPatientFileAction,
    GetDmsTimelineAction,
    ListDmsDocumentsAction,
    ListDmsPatientsAction,
    UploadDmsDocumentAction,
)
from hms_migration.modules.dms.contracts.dms_contracts import (
    DmsDocumentCreate,
    DmsDocumentResponse,
    DmsPatientFile,
    DmsPatientItem,
    DmsTimelineEvent,
)
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/dms", tags=["dms"])


# ── 1. Patients List ───────────────────────────────────────────────────────────
@router.get("/patients", response_model=list[DmsPatientItem])
def list_patients(
    search: str | None = None,
    care_status: str | None = Query(None, description="OPD | IPD | Inactive"),
    doctor_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[DmsPatientItem]:
    return ListDmsPatientsAction(db, hospital_id).execute(
        search=search,
        care_status=care_status,
        doctor_id=doctor_id,
        date_from=date_from,
        date_to=date_to,
    )


# ── 2. Patient Electronic File ─────────────────────────────────────────────────
@router.get("/patients/{patient_id}/file", response_model=DmsPatientFile)
def get_patient_file(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> DmsPatientFile:
    return GetDmsPatientFileAction(db, hospital_id).execute(patient_id)


# ── 3. Patient Clinical Timeline ───────────────────────────────────────────────
@router.get("/patients/{patient_id}/timeline", response_model=list[DmsTimelineEvent])
def get_timeline(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[DmsTimelineEvent]:
    return GetDmsTimelineAction(db, hospital_id).execute(patient_id)


# ── 4. Patient Documents List ──────────────────────────────────────────────────
@router.get("/patients/{patient_id}/documents", response_model=list[DmsDocumentResponse])
def list_documents(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[DmsDocumentResponse]:
    return ListDmsDocumentsAction(db, hospital_id).execute(patient_id)


# ── 5. Upload Patient Document ─────────────────────────────────────────────────
@router.post(
    "/patients/{patient_id}/documents",
    response_model=DmsDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_document(
    patient_id: UUID,
    payload: DmsDocumentCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> DmsDocumentResponse:
    return UploadDmsDocumentAction(db, hospital_id).execute(patient_id, payload, user)


# ── 6. Delete Document ─────────────────────────────────────────────────────────
@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> None:
    DeleteDmsDocumentAction(db, hospital_id).execute(document_id, user)


# ── 7. Download DMS Document File ──────────────────────────────────────────────
@router.get("/documents/{document_id}/file")
def download_dms_file(
    document_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    return DownloadDmsFileAction(db, hospital_id).execute(document_id)


# ── 8. Download Medical Record File ────────────────────────────────────────────
@router.get("/medical-records/{record_id}/file")
def download_medical_record_file(
    record_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    return DownloadMedicalRecordFileAction(db, hospital_id).execute(record_id)


# ── 9. Complete Patient File HTML ──────────────────────────────────────────────
@router.get("/patients/{patient_id}/complete-file")
def complete_patient_file_html(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    html, filename = GetCompletePatientFileHtmlAction(db, hospital_id).execute(patient_id)
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
