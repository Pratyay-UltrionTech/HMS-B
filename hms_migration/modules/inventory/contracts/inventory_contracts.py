"""
Pydantic contracts / DTOs for the central + departmental non-drug inventory domain.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from hms_migration.modules.inventory.entities.inventory_entities import (
    ConsumableCategory,
    InventoryStockTransactionType,
    InventoryTransferStatus,
)

# ── Feature 45: Non-Drug Consumable Master ──────────────────────────────────


class ConsumableItemCreate(BaseModel):
    item_code: str | None = Field(default=None, max_length=64)
    item_name: str = Field(min_length=1, max_length=255)
    category: ConsumableCategory
    unit_of_issue: str = Field(min_length=1, max_length=32)
    reorder_level: float = Field(ge=0, default=0.0)
    specification: str | None = None
    is_active: bool = True


class ConsumableItemUpdate(BaseModel):
    item_name: str | None = Field(default=None, min_length=1, max_length=255)
    category: ConsumableCategory | None = None
    unit_of_issue: str | None = Field(default=None, min_length=1, max_length=32)
    reorder_level: float | None = Field(default=None, ge=0)
    specification: str | None = None
    is_active: bool | None = None


class ConsumableItemResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    item_code: str
    item_name: str
    category: ConsumableCategory
    unit_of_issue: str
    reorder_level: float
    specification: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Feature 46: Batch/Lot Tracking ──────────────────────────────────────────


class ConsumableBatchResponse(BaseModel):
    id: UUID
    item_id: UUID
    batch_number: str
    received_quantity: float
    issued_quantity: float
    remaining_quantity: float
    expiry_date: date | None
    received_date: date
    unit_cost: float | None
    is_active: bool

    model_config = {"from_attributes": True}


# ── Feature 44: Central Inventory ───────────────────────────────────────────


class ReceiveStockRequest(BaseModel):
    item_id: UUID
    batch_number: str = Field(min_length=1, max_length=64)
    quantity: float = Field(gt=0)
    expiry_date: date | None = None
    received_date: date | None = None
    unit_cost: float | None = None
    location: str = Field(default="Main Store", max_length=128)
    performed_by_staff_id: str | None = None
    notes: str | None = None


class CentralInventoryStockResponse(BaseModel):
    id: UUID
    item_id: UUID
    item_name: str | None = None
    total_quantity: float
    location: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class InventoryStockTransactionResponse(BaseModel):
    id: UUID
    item_id: UUID
    batch_id: UUID | None
    transaction_type: InventoryStockTransactionType
    quantity: float
    from_location: str | None
    to_location: str | None
    reference_type: str | None
    reference_id: UUID | None
    performed_by_staff_id: str | None
    transaction_time: datetime
    notes: str | None

    model_config = {"from_attributes": True}


# ── Feature 47: Departmental Stock ──────────────────────────────────────────


class DepartmentalStockResponse(BaseModel):
    id: UUID
    item_id: UUID
    item_name: str | None = None
    department: str
    quantity: float
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConsumeDepartmentalStockRequest(BaseModel):
    item_id: UUID
    department: str = Field(min_length=1, max_length=128)
    quantity: float = Field(gt=0)
    performed_by_staff_id: str | None = None
    notes: str | None = None


# ── Feature 48: Inter-Department Transfer ───────────────────────────────────


class InventoryTransferCreate(BaseModel):
    item_id: UUID
    from_location: str = Field(min_length=1, max_length=128)
    to_department: str = Field(min_length=1, max_length=128)
    quantity: float = Field(gt=0)
    requested_by_staff_id: str | None = None


class InventoryTransferApprove(BaseModel):
    approved_by_staff_id: str | None = None


class InventoryTransferReject(BaseModel):
    rejection_reason: str | None = None


class InventoryTransferResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    item_id: UUID
    item_name: str | None = None
    from_location: str
    to_department: str
    quantity: float
    status: InventoryTransferStatus
    requested_by_staff_id: str | None
    approved_by_staff_id: str | None
    requested_at: datetime
    approved_at: datetime | None
    dispatched_at: datetime | None
    completed_at: datetime | None
    rejection_reason: str | None

    model_config = {"from_attributes": True}
