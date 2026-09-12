"""
Pydantic contracts / DTOs for the Procurement Lifecycle domain
(Features 49-55).
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from hms_migration.modules.procurement.entities.procurement_entities import (
    ApprovalDecision,
    ApprovalEntityType,
    MaterialRequisitionStatus,
    PurchaseOrderStatus,
)

# ── Feature 49: Material Requisition ─────────────────────────────────────────


class MaterialRequisitionCreate(BaseModel):
    department: str = Field(min_length=1, max_length=128)
    consumable_item_id: UUID
    quantity_requested: float = Field(gt=0)
    requested_by_staff_id: str | None = None


class MaterialRequisitionAction(BaseModel):
    approved_by_staff_id: str | None = None
    approval_notes: str | None = None


class MaterialRequisitionResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    requisition_no: str
    requested_by_staff_id: str | None
    department: str
    consumable_item_id: UUID
    item_name: str | None = None
    quantity_requested: float
    status: MaterialRequisitionStatus
    approved_by_staff_id: str | None
    approval_notes: str | None
    created_at: datetime
    resolved_at: datetime | None
    fulfilled_at: datetime | None

    model_config = {"from_attributes": True}


# ── Feature 52: Approval Workflow ────────────────────────────────────────────


class ApprovalThresholdCreate(BaseModel):
    applies_to: ApprovalEntityType
    min_amount: float | None = Field(default=None, ge=0)
    approver_role: str = Field(min_length=1, max_length=128)
    sequence_order: int = Field(default=1, ge=1)


class ApprovalThresholdResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    applies_to: ApprovalEntityType
    min_amount: float | None
    approver_role: str
    sequence_order: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ApprovalActionResponse(BaseModel):
    id: UUID
    entity_type: ApprovalEntityType
    entity_id: UUID
    approver_staff_id: str | None
    decision: ApprovalDecision
    decision_notes: str | None
    sequence_order: int
    decided_at: datetime

    model_config = {"from_attributes": True}


# ── Feature 50: Purchase Order Processing ────────────────────────────────────


class PurchaseOrderItemCreate(BaseModel):
    consumable_item_id: UUID
    quantity_ordered: float = Field(gt=0)
    negotiated_rate: float = Field(ge=0, default=0.0)


class PurchaseOrderCreate(BaseModel):
    supplier_id: UUID
    requisition_id: UUID | None = None
    expected_delivery_date: date | None = None
    terms: str | None = None
    created_by_staff_id: str | None = None
    items: list[PurchaseOrderItemCreate] = Field(min_length=1)


class PurchaseOrderItemResponse(BaseModel):
    id: UUID
    consumable_item_id: UUID
    item_name: str | None = None
    quantity_ordered: float
    negotiated_rate: float
    quantity_received: float

    model_config = {"from_attributes": True}


class PurchaseOrderResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    po_number: str
    supplier_id: UUID
    supplier_name: str | None = None
    requisition_id: UUID | None
    status: PurchaseOrderStatus
    expected_delivery_date: date | None
    terms: str | None
    created_by_staff_id: str | None
    created_at: datetime
    approved_at: datetime | None
    sent_at: datetime | None
    completed_at: datetime | None
    items: list[PurchaseOrderItemResponse] = []

    model_config = {"from_attributes": True}


class ApprovalDecisionRequest(BaseModel):
    approver_staff_id: str | None = None
    decision_notes: str | None = None


# ── Feature 51: Goods Received Note ──────────────────────────────────────────


class GrnItemCreate(BaseModel):
    purchase_order_item_id: UUID
    accepted_quantity: float = Field(ge=0, default=0.0)
    rejected_quantity: float = Field(ge=0, default=0.0)
    rejection_reason: str | None = None
    batch_number: str = Field(min_length=1, max_length=64)
    expiry_date: date | None = None
    unit_cost: float | None = None


class GrnCreate(BaseModel):
    purchase_order_id: UUID
    received_date: date | None = None
    supplier_invoice_number: str | None = None
    received_by_staff_id: str | None = None
    items: list[GrnItemCreate] = Field(min_length=1)


class GrnItemResponse(BaseModel):
    id: UUID
    purchase_order_item_id: UUID
    accepted_quantity: float
    rejected_quantity: float
    rejection_reason: str | None
    batch_number: str
    expiry_date: date | None

    model_config = {"from_attributes": True}


class GrnResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    grn_number: str
    purchase_order_id: UUID
    received_date: date
    supplier_invoice_number: str | None
    received_by_staff_id: str | None
    created_at: datetime
    items: list[GrnItemResponse] = []

    model_config = {"from_attributes": True}


# ── Feature 54: Supplier Performance ─────────────────────────────────────────


class SupplierPerformanceResponse(BaseModel):
    supplier_id: UUID
    supplier_name: str | None = None
    total_purchase_orders: int
    total_grns: int
    on_time_delivery_rate: float | None = None  # null if no delivered POs with expected date yet
    total_accepted_quantity: float
    total_rejected_quantity: float
    rejection_rate: float | None = None  # null if nothing received yet
    avg_days_po_sent_to_first_grn: float | None = None  # null if not computable


# ── Feature 55: Purchase & Consumption Analytics ─────────────────────────────


class SupplierSpend(BaseModel):
    supplier_id: UUID
    supplier_name: str | None = None
    total_spend: float
    po_count: int


class ConsumptionTrendPoint(BaseModel):
    item_id: UUID
    item_name: str | None = None
    total_consumed: float


class ReorderAlert(BaseModel):
    item_id: UUID
    item_name: str | None = None
    reorder_level: float
    current_quantity: float


class ProcurementAnalyticsResponse(BaseModel):
    spend_by_supplier: list[SupplierSpend]
    consumption_trends: list[ConsumptionTrendPoint]
    pending_orders_count: int
    reorder_alerts: list[ReorderAlert]
