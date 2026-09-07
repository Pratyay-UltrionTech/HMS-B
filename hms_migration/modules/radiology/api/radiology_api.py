"""
FastAPI router for Radiology domain.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.radiology.actions.radiology_actions import (
    CancelOrderAction,
    CompleteScanAction,
    CreateOrdersAction,
    CreateScanAction,
    DeleteScanAction,
    GetDashboardAction,
    GetOrderAction,
    ListOrdersAction,
    ListScansAction,
    ScheduleOrderAction,
    SeedStandardCatalogueAction,
    StartScanAction,
    UpdateScanAction,
    UploadReportAction,
)
from hms_migration.modules.radiology.contracts.radiology_contracts import (
    RadCatalogueSeedResult,
    RadDashboardResponse,
    RadOrderCreate,
    RadOrderResponse,
    RadReportRequest,
    RadScanCreate,
    RadScanResponse,
    RadScanUpdate,
    RadScheduleRequest,
)
from hms_migration.modules.radiology.db.radiology_repository import RadiologyRepository
from hms_migration.modules.radiology.entities.radiology_entities import RadiologyOrderStatus
from hms_migration.modules.radiology.services.radiology_service import (
    generate_radiology_report_html,
    stream_radiology_file,
)
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/radiology", tags=["radiology"])


@router.get("/dashboard", response_model=RadDashboardResponse)
def dashboard(
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadDashboardResponse:
    return GetDashboardAction(db, hospital_id).execute()


# ── Catalogue ──────────────────────────────────────────────────────────────────


@router.get("/scans", response_model=list[RadScanResponse])
def list_scans(
    active_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[RadScanResponse]:
    scans = ListScansAction(db, hospital_id).execute(active_only=active_only, search=search)
    return [RadScanResponse.model_validate(s) for s in scans]


@router.post("/catalogue/seed-standard", response_model=RadCatalogueSeedResult)
def seed_standard_radiology_catalogue(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadCatalogueSeedResult:
    return SeedStandardCatalogueAction(db, hospital_id, user).execute()


@router.post("/scans", response_model=RadScanResponse, status_code=status.HTTP_201_CREATED)
def create_scan(
    payload: RadScanCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadScanResponse:
    scan = CreateScanAction(db, hospital_id, user).execute(payload)
    return RadScanResponse.model_validate(scan)


@router.put("/scans/{scan_id}", response_model=RadScanResponse)
def update_scan(
    scan_id: UUID,
    payload: RadScanUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadScanResponse:
    scan = UpdateScanAction(db, hospital_id, user).execute(scan_id, payload)
    return RadScanResponse.model_validate(scan)


@router.delete("/scans/{scan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scan(
    scan_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> None:
    DeleteScanAction(db, hospital_id, user).execute(scan_id)


# ── Orders ─────────────────────────────────────────────────────────────────────


@router.get("/orders", response_model=list[RadOrderResponse])
def list_orders(
    status_filter: RadiologyOrderStatus | None = Query(default=None, alias="status"),
    patient_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None),
    scheduled_only: bool = Query(default=False),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[RadOrderResponse]:
    return ListOrdersAction(db, hospital_id).execute(
        status_filter=status_filter,
        patient_id=patient_id,
        search=search,
        scheduled_only=scheduled_only,
    )


@router.get("/orders/{order_id}", response_model=RadOrderResponse)
def get_order(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return GetOrderAction(db, hospital_id).execute(order_id)


@router.post("/orders", response_model=list[RadOrderResponse], status_code=status.HTTP_201_CREATED)
def create_orders(
    payload: RadOrderCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[RadOrderResponse]:
    return CreateOrdersAction(db, hospital_id, user).execute(payload)


@router.post("/orders/{order_id}/cancel", response_model=RadOrderResponse)
def cancel_order(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return CancelOrderAction(db, hospital_id, user).execute(order_id)


@router.post("/orders/{order_id}/schedule", response_model=RadOrderResponse)
def schedule_order(
    order_id: UUID,
    payload: RadScheduleRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return ScheduleOrderAction(db, hospital_id, user).execute(order_id, payload)


@router.post("/orders/{order_id}/start", response_model=RadOrderResponse)
def start_scan(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return StartScanAction(db, hospital_id, user).execute(order_id)


@router.post("/orders/{order_id}/complete-scan", response_model=RadOrderResponse)
def complete_scan(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return CompleteScanAction(db, hospital_id, user).execute(order_id)


@router.post("/orders/{order_id}/report", response_model=RadOrderResponse)
def upload_report(
    order_id: UUID,
    payload: RadReportRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return UploadReportAction(db, hospital_id, user).execute(order_id, payload)


@router.get("/orders/{order_id}/report-view")
def report_html(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    repo = RadiologyRepository(db, hospital_id)
    order = repo.get_order(order_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    html = generate_radiology_report_html(order, hospital.name if hospital else None)
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{order.order_no}-radiology.html"'},
    )


@router.get("/orders/{order_id}/file/{kind}")
def download_file(
    order_id: UUID,
    kind: str,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    repo = RadiologyRepository(db, hospital_id)
    order = repo.get_order(order_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Radiology order not found")
    return stream_radiology_file(order, kind)
