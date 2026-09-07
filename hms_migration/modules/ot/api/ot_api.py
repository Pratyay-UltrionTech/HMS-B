"""
FastAPI router for Operation Theatre (OT) domain.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.ot.actions.ot_actions import (
    CancelSurgeryAction,
    CompleteSurgeryAction,
    ConfirmSurgeryAction,
    CreateSurgeryAction,
    GetDashboardAction,
    GetSurgeryAction,
    ListCalendarAction,
    ListSurgeriesAction,
    RescheduleSurgeryAction,
    SaveNotesAction,
    StartSurgeryAction,
    UpdateSurgeryAction,
)
from hms_migration.modules.ot.contracts.ot_contracts import (
    OtCalendarEntry,
    OtCompleteRequest,
    OtDashboardResponse,
    OtNotesRequest,
    OtRescheduleRequest,
    OtSurgeryCreate,
    OtSurgeryResponse,
    OtSurgeryUpdate,
)
from hms_migration.modules.ot.db.ot_repository import OtRepository
from hms_migration.modules.ot.services.ot_service import (
    generate_ot_summary_html,
    stream_ot_file,
)
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/ot", tags=["ot"])


@router.get("/calendar", response_model=list[OtCalendarEntry])
def ot_calendar(
    date_from: date = Query(...),
    date_to: date = Query(...),
    ot_room_id: UUID | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[OtCalendarEntry]:
    return ListCalendarAction(db, hospital_id).execute(
        date_from=date_from, date_to=date_to, ot_room_id=ot_room_id
    )


@router.get("/dashboard", response_model=OtDashboardResponse)
def dashboard(
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtDashboardResponse:
    return GetDashboardAction(db, hospital_id).execute()


@router.get("/surgeries", response_model=list[OtSurgeryResponse])
def list_surgeries(
    status_filter: str | None = Query(None, alias="status"),
    patient_id: UUID | None = None,
    search: str | None = None,
    schedule_only: bool | None = Query(None),
    ongoing_only: bool | None = Query(None),
    history_only: bool | None = Query(None),
    notes_pending: bool | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[OtSurgeryResponse]:
    return ListSurgeriesAction(db, hospital_id).execute(
        status_filter=status_filter,
        patient_id=patient_id,
        search=search,
        schedule_only=schedule_only,
        ongoing_only=ongoing_only,
        history_only=history_only,
        notes_pending=notes_pending,
    )


@router.get("/surgeries/{surgery_id}", response_model=OtSurgeryResponse)
def get_surgery(
    surgery_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtSurgeryResponse:
    return GetSurgeryAction(db, hospital_id).execute(surgery_id)


@router.post("/surgeries", response_model=OtSurgeryResponse, status_code=status.HTTP_201_CREATED)
def create_surgery(
    payload: OtSurgeryCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtSurgeryResponse:
    return CreateSurgeryAction(db, hospital_id, user).execute(payload)


@router.put("/surgeries/{surgery_id}", response_model=OtSurgeryResponse)
def update_surgery(
    surgery_id: UUID,
    payload: OtSurgeryUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtSurgeryResponse:
    return UpdateSurgeryAction(db, hospital_id, user).execute(surgery_id, payload)


@router.post("/surgeries/{surgery_id}/confirm", response_model=OtSurgeryResponse)
def confirm_surgery(
    surgery_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtSurgeryResponse:
    return ConfirmSurgeryAction(db, hospital_id, user).execute(surgery_id)


@router.post("/surgeries/{surgery_id}/reschedule", response_model=OtSurgeryResponse)
def reschedule_surgery(
    surgery_id: UUID,
    payload: OtRescheduleRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtSurgeryResponse:
    return RescheduleSurgeryAction(db, hospital_id, user).execute(surgery_id, payload)


@router.post("/surgeries/{surgery_id}/start", response_model=OtSurgeryResponse)
def start_surgery(
    surgery_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtSurgeryResponse:
    return StartSurgeryAction(db, hospital_id, user).execute(surgery_id)


@router.post("/surgeries/{surgery_id}/complete", response_model=OtSurgeryResponse)
def complete_surgery(
    surgery_id: UUID,
    payload: OtCompleteRequest | None = None,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtSurgeryResponse:
    return CompleteSurgeryAction(db, hospital_id, user).execute(surgery_id, payload)


@router.post("/surgeries/{surgery_id}/cancel", response_model=OtSurgeryResponse)
def cancel_surgery(
    surgery_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtSurgeryResponse:
    return CancelSurgeryAction(db, hospital_id, user).execute(surgery_id)


@router.post("/surgeries/{surgery_id}/notes", response_model=OtSurgeryResponse)
def save_notes(
    surgery_id: UUID,
    payload: OtNotesRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> OtSurgeryResponse:
    return SaveNotesAction(db, hospital_id, user).execute(surgery_id, payload)


@router.get("/surgeries/{surgery_id}/summary-view")
def summary_html(
    surgery_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    repo = OtRepository(db, hospital_id)
    item = repo.get_surgery(surgery_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    html = generate_ot_summary_html(item, hospital.name if hospital else None)
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{item.surgery_no}-ot-summary.html"'},
    )


@router.get("/surgeries/{surgery_id}/file/{kind}")
def download_file(
    surgery_id: UUID,
    kind: str,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    repo = OtRepository(db, hospital_id)
    item = repo.get_surgery(surgery_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
    return stream_ot_file(item, kind)
