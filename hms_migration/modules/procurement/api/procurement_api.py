"""
Procurement Lifecycle API endpoints (Features 49-55).

Prefix: /procurement
Tags: ["procurement"]
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.procurement.actions.procurement_actions import ProcurementActions
from hms_migration.modules.procurement.contracts.procurement_contracts import (
    ApprovalActionResponse,
    ApprovalDecisionRequest,
    ApprovalThresholdCreate,
    ApprovalThresholdResponse,
    GrnCreate,
    GrnResponse,
    MaterialRequisitionAction,
    MaterialRequisitionCreate,
    MaterialRequisitionResponse,
    ProcurementAnalyticsResponse,
    PurchaseOrderCreate,
    PurchaseOrderResponse,
    SupplierPerformanceResponse,
)
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/procurement", tags=["procurement"])


# ── Feature 49: Material Requisition ─────────────────────────────────────────

@router.get("/requisitions", response_model=list[MaterialRequisitionResponse])
def list_requisitions(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[MaterialRequisitionResponse]:
    return ProcurementActions(db, hospital_id, user).list_requisitions(status_filter=status_filter)


@router.post("/requisitions", response_model=MaterialRequisitionResponse, status_code=status.HTTP_201_CREATED)
def create_requisition(
    payload: MaterialRequisitionCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> MaterialRequisitionResponse:
    return ProcurementActions(db, hospital_id, user).create_requisition(payload)


@router.post("/requisitions/{requisition_id}/approve", response_model=MaterialRequisitionResponse)
def approve_requisition(
    requisition_id: UUID,
    payload: MaterialRequisitionAction | None = None,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> MaterialRequisitionResponse:
    return ProcurementActions(db, hospital_id, user).approve_requisition(requisition_id, payload)


@router.post("/requisitions/{requisition_id}/reject", response_model=MaterialRequisitionResponse)
def reject_requisition(
    requisition_id: UUID,
    payload: MaterialRequisitionAction | None = None,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> MaterialRequisitionResponse:
    return ProcurementActions(db, hospital_id, user).reject_requisition(requisition_id, payload)


@router.post("/requisitions/{requisition_id}/fulfill", response_model=MaterialRequisitionResponse)
def fulfill_requisition(
    requisition_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> MaterialRequisitionResponse:
    return ProcurementActions(db, hospital_id, user).fulfill_requisition(requisition_id)


# ── Feature 52: Approval Workflow ────────────────────────────────────────────

@router.get("/approval-thresholds", response_model=list[ApprovalThresholdResponse])
def list_thresholds(
    applies_to: str | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[ApprovalThresholdResponse]:
    return ProcurementActions(db, hospital_id, user).list_thresholds(applies_to=applies_to)


@router.post("/approval-thresholds", response_model=ApprovalThresholdResponse, status_code=status.HTTP_201_CREATED)
def create_threshold(
    payload: ApprovalThresholdCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> ApprovalThresholdResponse:
    return ProcurementActions(db, hospital_id, user).create_threshold(payload)


@router.get("/approval-history/{entity_type}/{entity_id}", response_model=list[ApprovalActionResponse])
def list_approval_history(
    entity_type: str,
    entity_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[ApprovalActionResponse]:
    return ProcurementActions(db, hospital_id, user).list_approval_history(entity_type, entity_id)


# ── Feature 50: Purchase Order Processing ────────────────────────────────────

@router.get("/purchase-orders", response_model=list[PurchaseOrderResponse])
def list_pos(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[PurchaseOrderResponse]:
    return ProcurementActions(db, hospital_id, user).list_pos(status_filter=status_filter)


@router.post("/purchase-orders", response_model=PurchaseOrderResponse, status_code=status.HTTP_201_CREATED)
def create_po(
    payload: PurchaseOrderCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PurchaseOrderResponse:
    return ProcurementActions(db, hospital_id, user).create_po(payload)


@router.get("/purchase-orders/{po_id}", response_model=PurchaseOrderResponse)
def get_po(
    po_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PurchaseOrderResponse:
    return ProcurementActions(db, hospital_id, user).get_po(po_id)


@router.post("/purchase-orders/{po_id}/submit", response_model=PurchaseOrderResponse)
def submit_po(
    po_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PurchaseOrderResponse:
    return ProcurementActions(db, hospital_id, user).submit_po_for_approval(po_id)


@router.post("/purchase-orders/{po_id}/approve", response_model=PurchaseOrderResponse)
def approve_po(
    po_id: UUID,
    payload: ApprovalDecisionRequest | None = None,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PurchaseOrderResponse:
    return ProcurementActions(db, hospital_id, user).approve_po(po_id, payload)


@router.post("/purchase-orders/{po_id}/cancel", response_model=PurchaseOrderResponse)
def cancel_po(
    po_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PurchaseOrderResponse:
    return ProcurementActions(db, hospital_id, user).cancel_po(po_id)


@router.post("/purchase-orders/{po_id}/send", response_model=PurchaseOrderResponse)
def mark_po_sent(
    po_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PurchaseOrderResponse:
    return ProcurementActions(db, hospital_id, user).mark_po_sent(po_id)


# ── Feature 51: Goods Received Note ──────────────────────────────────────────

@router.post("/grns", response_model=GrnResponse, status_code=status.HTTP_201_CREATED)
def record_grn(
    payload: GrnCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> GrnResponse:
    return ProcurementActions(db, hospital_id, user).record_grn(payload)


@router.get("/purchase-orders/{po_id}/grns", response_model=list[GrnResponse])
def list_grns_for_po(
    po_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[GrnResponse]:
    return ProcurementActions(db, hospital_id, user).list_grns_for_po(po_id)


# ── Feature 54: Supplier Performance ─────────────────────────────────────────

@router.get("/suppliers/{supplier_id}/performance", response_model=SupplierPerformanceResponse)
def get_supplier_performance(
    supplier_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SupplierPerformanceResponse:
    return ProcurementActions(db, hospital_id, user).get_supplier_performance(supplier_id)


# ── Feature 55: Purchase & Consumption Analytics ─────────────────────────────

@router.get("/analytics", response_model=ProcurementAnalyticsResponse)
def get_analytics(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> ProcurementAnalyticsResponse:
    return ProcurementActions(db, hospital_id, user).get_analytics()
