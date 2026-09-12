"""
Target-native SQLAlchemy entities for the central + departmental non-drug
inventory domain (consumables, PPE, ward/lab/housekeeping/stationery supplies).

Features covered:
- Feature 44: Central Inventory (CentralInventoryStock, InventoryStockTransaction)
- Feature 45: Non-Drug Consumable Master (ConsumableItem)
- Feature 46: Batch/Lot Tracking (ConsumableBatch)
- Feature 47: Departmental Stock (DepartmentalStock)
- Feature 48: Inter-Department Transfer (InventoryTransfer)

Tables:
- inventory_consumable_items
- inventory_consumable_batches
- inventory_central_stock
- inventory_stock_transactions
- inventory_departmental_stock
- inventory_transfers

Redundancy-audit note: `ConsumableItem` / `ConsumableBatch` are the single canonical
consumable + batch/lot reference for this whole non-drug inventory domain (and for
requisition/procurement work in later features 49-51). They deliberately do NOT reuse
or duplicate `modules/pharmacy`'s `Medicine` / `MedicineBatch` — those are for
prescribable drugs (dosage, schedule-drug flags, Rx linkage) — but they DO mirror the
same field shapes and stock-transaction discipline (received/available quantity on the
batch, a single append-only ledger of signed transactions) so there is no incompatible
parallel definition of "batch", "lot", "expiry" or "stock quantity" in the codebase.

Department fields (`DepartmentalStock.department`, `InventoryTransfer.to_department`)
are plain strings, not a FK to `modules.masters.entities.organization_entities.Department`.
A real `departments` table does exist in masters, but per investigation only
`modules/ot` uses it as a hard FK; every other clinical/operational module (equipment,
OT staff assignment, cssd issue/return) stores department as a free-text string for
consistency and because departments are frequently entered as free text at these call
sites today. This module follows that same majority convention. If department master
data is enforced later, this column can be migrated to a FK without changing the ledger
semantics.
"""

from __future__ import annotations

from datetime import date, datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base


class ConsumableCategory(str, enum.Enum):
    surgical_disposable = "surgical_disposable"
    ward_supply = "ward_supply"
    lab_consumable = "lab_consumable"
    ppe = "ppe"
    housekeeping = "housekeeping"
    stationery = "stationery"
    other = "other"


class InventoryStockTransactionType(str, enum.Enum):
    receipt = "receipt"
    issue_to_department = "issue_to_department"
    consumption = "consumption"
    transfer_out = "transfer_out"
    transfer_in = "transfer_in"
    adjustment = "adjustment"


class InventoryTransferStatus(str, enum.Enum):
    requested = "requested"
    approved = "approved"
    dispatched = "dispatched"
    completed = "completed"
    rejected = "rejected"


class ConsumableItem(Base):
    """Canonical non-drug consumable master item (Feature 45)."""

    __tablename__ = "inventory_consumable_items"
    __table_args__ = (
        UniqueConstraint("hospital_id", "item_code", name="uq_inventory_item_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    category: Mapped[ConsumableCategory] = mapped_column(
        Enum(ConsumableCategory, name="inventory_consumable_category"), nullable=False
    )
    unit_of_issue: Mapped[str] = mapped_column(String(32), nullable=False)
    reorder_level: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    specification: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    batches: Mapped[list["ConsumableBatch"]] = relationship(back_populates="item")


class ConsumableBatch(Base):
    """Batch/lot tracking for a consumable item (Feature 46), mirroring the
    pharmacy MedicineBatch pattern (received/available quantity, optional expiry)."""

    __tablename__ = "inventory_consumable_batches"
    __table_args__ = (
        UniqueConstraint("hospital_id", "item_id", "batch_number", name="uq_inventory_batch_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_consumable_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    received_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    issued_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    remaining_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    unit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    item: Mapped["ConsumableItem"] = relationship(back_populates="batches")


class CentralInventoryStock(Base):
    """Aggregate central-store stock per (hospital, item) (Feature 44)."""

    __tablename__ = "inventory_central_stock"
    __table_args__ = (
        UniqueConstraint("hospital_id", "item_id", name="uq_inventory_central_stock_item"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_consumable_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    total_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    location: Mapped[str] = mapped_column(String(128), nullable=False, default="Main Store")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    item: Mapped["ConsumableItem"] = relationship()


class InventoryStockTransaction(Base):
    """Append-only ledger of all stock movement across the inventory module
    (central receipt, departmental issue/consumption, transfer, adjustment)."""

    __tablename__ = "inventory_stock_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_consumable_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_consumable_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    transaction_type: Mapped[InventoryStockTransactionType] = mapped_column(
        Enum(InventoryStockTransactionType, name="inventory_stock_transaction_type"), nullable=False, index=True
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)  # signed: +in / -out
    from_location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    to_location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reference_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    performed_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transaction_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class DepartmentalStock(Base):
    """Per-department stock balance for a consumable item (Feature 47)."""

    __tablename__ = "inventory_departmental_stock"
    __table_args__ = (
        UniqueConstraint("hospital_id", "item_id", "department", name="uq_inventory_dept_stock_item"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_consumable_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    department: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    item: Mapped["ConsumableItem"] = relationship()


class InventoryTransfer(Base):
    """Inter-department (or central-to-department) stock transfer with an
    enforced status lifecycle (Feature 48)."""

    __tablename__ = "inventory_transfers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_consumable_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    from_location: Mapped[str] = mapped_column(String(128), nullable=False)
    to_department: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[InventoryTransferStatus] = mapped_column(
        Enum(InventoryTransferStatus, name="inventory_transfer_status"),
        nullable=False,
        default=InventoryTransferStatus.requested,
        index=True,
    )
    requested_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    item: Mapped["ConsumableItem"] = relationship()
