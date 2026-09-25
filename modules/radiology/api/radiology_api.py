"""
FastAPI router for Radiology domain.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from infrastructure.postgres.session import get_transitional_sync_session
from modules.radiology.actions.radiology_actions import (
    AddAttachmentAction,
    CancelOrderAction,
    CancelRadPrescriptionRequestAction,
    CompleteScanAction,
    CreateOrdersAction,
    CreateScanAction,
    DeleteAttachmentAction,
    DeleteScanAction,
    GetDashboardAction,
    GetOrderAction,
    GetRadPrescriptionRequestAction,
    ListOrdersAction,
    ListRadPrescriptionRequestsAction,
    ListScansAction,
    MarkRadRequestItemUnavailableAction,
    ScheduleOrderAction,
    SeedStandardCatalogueAction,
    StartScanAction,
    UpdateScanAction,
    UploadReportAction,
)
from modules.radiology.contracts.radiology_contracts import (
    RadAttachmentResponse,
    RadAttachmentUploadRequest,
    RadCatalogueSeedResult,
    RadDashboardResponse,
    RadOrderCreate,
    RadOrderResponse,
    RadPrescriptionRequestResponse,
    RadReportRequest,
    RadRequestCancelBody,
    RadScanCreate,
    RadScanResponse,
    RadScanUpdate,
    RadScheduleRequest,
)
from modules.radiology.db.radiology_repository import RadiologyRepository
from modules.radiology.entities.radiology_entities import (
    RadiologyOrderStatus,
    RadPrescriptionRequestStatus,
)
from modules.radiology.services.radiology_service import (
    generate_radiology_report_html,
    stream_radiology_file,
)
from modules.tenancy.entities.hospital import Hospital
from shared.auth import get_hospital_context, require_hospital_user, require_permission

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


@router.post("/catalogue/seed-standard", response_model=RadCatalogueSeedResult, dependencies=[Depends(require_permission("radiology", "edit"))])
def seed_standard_radiology_catalogue(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadCatalogueSeedResult:
    return SeedStandardCatalogueAction(db, hospital_id, user).execute()


@router.post("/scans", response_model=RadScanResponse, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("radiology", "edit"))])
def create_scan(
    payload: RadScanCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadScanResponse:
    scan = CreateScanAction(db, hospital_id, user).execute(payload)
    return RadScanResponse.model_validate(scan)


@router.put("/scans/{scan_id}", response_model=RadScanResponse, dependencies=[Depends(require_permission("radiology", "edit"))])
def update_scan(
    scan_id: UUID,
    payload: RadScanUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadScanResponse:
    scan = UpdateScanAction(db, hospital_id, user).execute(scan_id, payload)
    return RadScanResponse.model_validate(scan)


@router.delete("/scans/{scan_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_permission("radiology", "edit"))])
def delete_scan(
    scan_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    DeleteScanAction(db, hospital_id, user).execute(scan_id)


# ── Prescription requests ──────────────────────────────────────────────────────


@router.get("/prescription-requests", response_model=list[RadPrescriptionRequestResponse])
def list_prescription_requests(
    status: RadPrescriptionRequestStatus | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    released_only: bool | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[RadPrescriptionRequestResponse]:
    return ListRadPrescriptionRequestsAction(db, hospital_id).execute(
        status=status, patient_id=patient_id, doctor_id=doctor_id, released_only=released_only
    )


@router.get("/prescription-requests/{request_id}", response_model=RadPrescriptionRequestResponse)
def get_prescription_request(
    request_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadPrescriptionRequestResponse:
    return GetRadPrescriptionRequestAction(db, hospital_id).execute(request_id)


@router.post("/prescription-requests/{request_id}/cancel", response_model=RadPrescriptionRequestResponse,
    dependencies=[Depends(require_permission("radiology", "edit"))])
def cancel_prescription_request(
    request_id: UUID,
    payload: RadRequestCancelBody,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadPrescriptionRequestResponse:
    return CancelRadPrescriptionRequestAction(db, hospital_id).execute(request_id, payload.reason, user)


@router.post(
    "/prescription-requests/{request_id}/items/{item_id}/unavailable",
    response_model=RadPrescriptionRequestResponse,

    dependencies=[Depends(require_permission("radiology", "edit"))])
def mark_request_item_unavailable(
    request_id: UUID,
    item_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadPrescriptionRequestResponse:
    return MarkRadRequestItemUnavailableAction(db, hospital_id).execute(request_id, item_id, user)


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


@router.post("/orders", response_model=list[RadOrderResponse], status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("radiology", "edit"))])
def create_orders(
    payload: RadOrderCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[RadOrderResponse]:
    return CreateOrdersAction(db, hospital_id, user).execute(payload)


@router.post("/orders/{order_id}/cancel", response_model=RadOrderResponse, dependencies=[Depends(require_permission("radiology", "edit"))])
def cancel_order(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return CancelOrderAction(db, hospital_id, user).execute(order_id)


@router.post("/orders/{order_id}/schedule", response_model=RadOrderResponse, dependencies=[Depends(require_permission("radiology", "edit"))])
def schedule_order(
    order_id: UUID,
    payload: RadScheduleRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return ScheduleOrderAction(db, hospital_id, user).execute(order_id, payload)


@router.post("/orders/{order_id}/start", response_model=RadOrderResponse, dependencies=[Depends(require_permission("radiology", "edit"))])
def start_scan(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return StartScanAction(db, hospital_id, user).execute(order_id)


@router.post("/orders/{order_id}/complete-scan", response_model=RadOrderResponse,
    dependencies=[Depends(require_permission("radiology", "edit"))])
def complete_scan(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadOrderResponse:
    return CompleteScanAction(db, hospital_id, user).execute(order_id)


@router.post("/orders/{order_id}/report", response_model=RadOrderResponse, dependencies=[Depends(require_permission("radiology", "release"))])
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

    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.service_financial_clearance import assert_service_financially_cleared
    assert_service_financially_cleared(
        db,
        hospital_id,
        BillingSourceType.radiology,
        order.id,
        action_description="view radiology report",
    )

    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    html = generate_radiology_report_html(
        order,
        hospital.name if hospital else None,
        hospital.address if hospital else None,
        hospital.phone if hospital else None,
        hospital.email if hospital else None,
    )
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
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    return stream_radiology_file(
        order,
        kind,
        hospital.name if hospital else None,
        hospital.address if hospital else None,
        hospital.phone if hospital else None,
        hospital.email if hospital else None,
    )


# ── Attachments (Dedicated Acquisition Flow) ──────────────────────────────────
@router.post(
    "/orders/{order_id}/attachments",
    response_model=RadAttachmentResponse,
    dependencies=[Depends(require_permission("radiology", "edit"))],
)
def upload_order_attachment(
    order_id: UUID,
    payload: RadAttachmentUploadRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RadAttachmentResponse:
    import base64
    original_name = payload.file_name.strip()
    mime_type = payload.mime_type.strip()

    # Decode base64 data (strip optional data URI prefix).
    # file_data is coalesced from the legacy file_data_base64 alias by validator.
    raw_b64 = (payload.file_data or payload.file_data_base64 or "").strip()
    if not raw_b64:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="file_data is required")
    if "," in raw_b64:
        raw_b64 = raw_b64.split(",", 1)[1]

    try:
        file_bytes = base64.b64decode(raw_b64)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid base64 attachment data")

    file_size = len(file_bytes)

    # 15MB limit for attachments
    if file_size > 15 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size exceeds the 15MB attachment limit.",
        )

    from shared.storage.local_storage import save_attachment_file
    storage_path = save_attachment_file(
        module="radiology",
        hospital_id=hospital_id,
        order_id=order_id,
        file_id=uuid.uuid4(),
        file_name=original_name,
        data=file_bytes,
    )

    action = AddAttachmentAction(db, hospital_id, user)
    return action.execute(
        order_id=order_id,
        file_name=original_name,
        mime_type=mime_type,
        file_size=file_size,
        storage_path=storage_path,
        attachment_type=payload.attachment_type,
    )


@router.get("/orders/{order_id}/attachments")
def list_order_attachments(
    order_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[RadAttachmentResponse]:
    repo = RadiologyRepository(db, hospital_id)
    attachments = repo.list_attachments_for_order(order_id)
    return [RadAttachmentResponse.model_validate(a) for a in attachments]


@router.get("/orders/{order_id}/attachments/{attachment_id}/file")
def get_order_attachment_file(
    order_id: UUID,
    attachment_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    repo = RadiologyRepository(db, hospital_id)
    att = repo.get_attachment(attachment_id)
    if not att or att.order_id != order_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    from shared.storage.local_storage import read_attachment_file
    try:
        file_bytes = read_attachment_file(att.storage_path)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment data missing on disk")

    return StreamingResponse(
        BytesIO(file_bytes),
        media_type=att.mime_type,
        headers={"Content-Disposition": f'inline; filename="{att.file_name}"'},
    )


@router.delete(
    "/orders/{order_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("radiology", "edit"))],
)
def delete_order_attachment(
    order_id: UUID,
    attachment_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> Response:
    action = DeleteAttachmentAction(db, hospital_id, user)
    action.execute(order_id, attachment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

