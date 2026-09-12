"""
Laboratory domain API router.

Exposes all 24 laboratory management and diagnostic endpoints matching HMS-B behavior.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.shared.auth import (
    get_hospital_context,
    require_hospital_user,
)
from hms_migration.modules.laboratory.actions.laboratory_actions import (
    CancelLabOrderAction,
    CancelLabPrescriptionRequestAction,
    CollectSampleAction,
    CreateLabOrderAction,
    CreateLabPanelAction,
    CreateLabTestAction,
    DeleteLabPanelAction,
    DeleteLabTestAction,
    GetLabDashboardAction,
    GetLabOrderAction,
    GetLabPanelAction,
    GetLabPrescriptionRequestAction,
    GetLabReportHtmlAction,
    ListLabOrdersAction,
    ListLabPanelsAction,
    ListLabPrescriptionRequestsAction,
    ListLabTestsAction,
    MarkLabRequestItemUnavailableAction,
    SaveLabResultsAction,
    SeedStandardCatalogueAction,
    UpdateItemStatusAction,
    UpdateLabPanelAction,
    UpdateLabTestAction,
)
from hms_migration.modules.laboratory.contracts.lab_contracts import (
    ItemStatusUpdate,
    LabCatalogueSeedResult,
    LabDashboardResponse,
    LabOrderCreate,
    LabOrderResponse,
    LabPanelCreate,
    LabPanelResponse,
    LabPanelUpdate,
    LabPrescriptionRequestResponse,
    LabReportSaveRequest,
    LabRequestCancelBody,
    LabTestCreate,
    LabTestResponse,
    LabTestUpdate,
    SampleCollectRequest,
)
from hms_migration.modules.laboratory.entities.lab_entities import (
    LabOrderSource,
    LabOrderStatus,
    LabPrescriptionRequestStatus,
)

router = APIRouter(prefix="/laboratory", tags=["laboratory"])


# ── 1. Dashboard ───────────────────────────────────────────────────────────────
@router.get("/dashboard", response_model=LabDashboardResponse)
def get_dashboard(
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabDashboardResponse:
    return GetLabDashboardAction(db, hospital_id).execute()


# ── 2-6. Catalogue Tests ───────────────────────────────────────────────────────
@router.get("/tests", response_model=list[LabTestResponse])
def list_tests(
    department: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    search: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[LabTestResponse]:
    return ListLabTestsAction(db, hospital_id).execute(
        department=department, is_active=is_active, search=search
    )


@router.post("/catalogue/seed-standard", response_model=LabCatalogueSeedResult)
def seed_standard_catalogue(
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabCatalogueSeedResult:
    return SeedStandardCatalogueAction(db, hospital_id).execute(user)


@router.post("/tests", response_model=LabTestResponse, status_code=status.HTTP_201_CREATED)
def create_test(
    payload: LabTestCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabTestResponse:
    return CreateLabTestAction(db, hospital_id).execute(payload, user)


@router.put("/tests/{test_id}", response_model=LabTestResponse)
def update_test(
    test_id: UUID,
    payload: LabTestUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabTestResponse:
    return UpdateLabTestAction(db, hospital_id).execute(test_id, payload, user)


@router.delete("/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_test(
    test_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> Response:
    DeleteLabTestAction(db, hospital_id).execute(test_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── 7-12. Panels ───────────────────────────────────────────────────────────────
@router.get("/panels", response_model=list[LabPanelResponse])
def list_panels(
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[LabPanelResponse]:
    return ListLabPanelsAction(db, hospital_id).execute(is_active=is_active)


@router.post("/panels/seed-defaults", response_model=list[LabPanelResponse])
def seed_default_panels(
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[LabPanelResponse]:
    SeedStandardCatalogueAction(db, hospital_id).execute(user)
    return ListLabPanelsAction(db, hospital_id).execute(is_active=True)


@router.get("/panels/{panel_id}", response_model=LabPanelResponse)
def get_panel(
    panel_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabPanelResponse:
    return GetLabPanelAction(db, hospital_id).execute(panel_id)


@router.post("/panels", response_model=LabPanelResponse, status_code=status.HTTP_201_CREATED)
def create_panel(
    payload: LabPanelCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabPanelResponse:
    return CreateLabPanelAction(db, hospital_id).execute(payload, user)


@router.put("/panels/{panel_id}", response_model=LabPanelResponse)
def update_panel(
    panel_id: UUID,
    payload: LabPanelUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabPanelResponse:
    return UpdateLabPanelAction(db, hospital_id).execute(panel_id, payload, user)


@router.delete("/panels/{panel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_panel(
    panel_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    DeleteLabPanelAction(db, hospital_id).execute(panel_id, user)


# ── 13-16. Prescription Requests ───────────────────────────────────────────────
@router.get("/prescription-requests", response_model=list[LabPrescriptionRequestResponse])
def list_prescription_requests(
    status: LabPrescriptionRequestStatus | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[LabPrescriptionRequestResponse]:
    return ListLabPrescriptionRequestsAction(db, hospital_id).execute(
        status=status, patient_id=patient_id, doctor_id=doctor_id
    )


@router.get("/prescription-requests/{request_id}", response_model=LabPrescriptionRequestResponse)
def get_prescription_request_endpoint(
    request_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabPrescriptionRequestResponse:
    return GetLabPrescriptionRequestAction(db, hospital_id).execute(request_id)


@router.post("/prescription-requests/{request_id}/cancel", response_model=LabPrescriptionRequestResponse)
def cancel_prescription_request(
    request_id: UUID,
    body: LabRequestCancelBody | None = None,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabPrescriptionRequestResponse:
    reason = body.reason if body else None
    return CancelLabPrescriptionRequestAction(db, hospital_id).execute(request_id, reason, user)


@router.post(
    "/prescription-requests/{request_id}/items/{item_id}/unavailable",
    response_model=LabPrescriptionRequestResponse,
)
def mark_request_item_unavailable(
    request_id: UUID,
    item_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabPrescriptionRequestResponse:
    return MarkLabRequestItemUnavailableAction(db, hospital_id).execute(request_id, item_id, user)


# ── 17-24. Orders & Reports ────────────────────────────────────────────────────
@router.get("/orders", response_model=list[LabOrderResponse])
def list_orders(
    status: LabOrderStatus | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    order_date: date | None = Query(default=None),
    order_source: LabOrderSource | None = Query(default=None),
    search: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[LabOrderResponse]:
    return ListLabOrdersAction(db, hospital_id).execute(
        status=status,
        patient_id=patient_id,
        doctor_id=doctor_id,
        order_date=order_date,
        order_source=order_source,
        search=search,
    )


@router.get("/orders/{order_id}", response_model=LabOrderResponse)
def get_order(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabOrderResponse:
    return GetLabOrderAction(db, hospital_id).execute(order_id)


@router.post("/orders", response_model=LabOrderResponse, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: LabOrderCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabOrderResponse:
    return CreateLabOrderAction(db, hospital_id).execute(payload, user)


@router.post("/orders/{order_id}/cancel", response_model=LabOrderResponse)
def cancel_order(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabOrderResponse:
    return CancelLabOrderAction(db, hospital_id).execute(order_id, user)


@router.post("/orders/{order_id}/collect-sample", response_model=LabOrderResponse)
def collect_sample(
    order_id: UUID,
    payload: SampleCollectRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabOrderResponse:
    return CollectSampleAction(db, hospital_id).execute(order_id, payload, user)


@router.put("/orders/{order_id}/items/{item_id}/status", response_model=LabOrderResponse)
def update_item_status(
    order_id: UUID,
    item_id: UUID,
    payload: ItemStatusUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabOrderResponse:
    return UpdateItemStatusAction(db, hospital_id).execute(order_id, item_id, payload, user)


@router.post("/orders/{order_id}/results", response_model=LabOrderResponse)
def save_results(
    order_id: UUID,
    payload: LabReportSaveRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> LabOrderResponse:
    return SaveLabResultsAction(db, hospital_id).execute(order_id, payload, user)


@router.get("/orders/{order_id}/report")
def report_html(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    html, filename = GetLabReportHtmlAction(db, hospital_id).execute(order_id)
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
