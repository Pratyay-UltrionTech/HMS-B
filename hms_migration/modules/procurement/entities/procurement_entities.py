"""
Target-native SQLAlchemy entities for the Procurement Lifecycle domain
(Features 49-55: Material Requisition, Purchase Order Processing, Goods
Received Note, Approval Workflow, Vendor Master, Supplier Performance,
Purchase & Consumption Analytics).

Tables:
- procurement_material_requisitions
- procurement_approval_thresholds
- procurement_approval_actions
- procurement_purchase_orders
- procurement_purchase_order_items
- procurement_grns
- procurement_grn_items

── Feature 49 (Material Requisition) MERGE decision ─────────────────────────
`modules/equipment` already ships a working, tested, migrated `EquipmentRequest`
entity/table/routes with its own pending -> approved/rejected/assigned
workflow, scoped specifically to equipment. Renaming or folding that entity
into a new generic requisition table would risk changing an existing API
contract and existing equipment behavior, which is explicitly out of bounds.

So this module does NOT touch `EquipmentRequest` at all. Instead,
`MaterialRequisition` below is a new, generalized requisition entity scoped to
consumable/material requests (FK to `inventory.ConsumableItem`), following the
SAME structural workflow shape as `EquipmentRequest` (requester/department
fields, a pending/approved/rejected status enum, admin/approval notes) so the
two entities are conceptually consistent even though they are not literally
the same table. `EquipmentRequest` is the equipment-specific instance of this
pattern and is intentionally left untouched. A true full merge (making
`EquipmentRequest` a special case of `MaterialRequisition`, e.g. via a
polymorphic `requisition_type` discriminator) is a larger migration not
undertaken in this iteration — noted as a known/deferred item.

── Feature 52 (Approval Workflow) design notes ──────────────────────────────
`ApprovalAction` is intentionally polymorphic-ish: `entity_id` has no hard FK
because it can reference either a `MaterialRequisition` or a `PurchaseOrder`
row (`entity_type` disambiguates). This avoids two near-identical approval-log
tables. Simplification: if a hospital has configured no `ApprovalThreshold`
rows for a given `applies_to`, a SINGLE approval step is sufficient to
approve/reject that entity (this is the common/default case, implemented
robustly). If thresholds ARE configured, sequential multi-tier approval by
`sequence_order` is applied as a best-effort layer on top of the same
single-step primitives — this module does not implement a full generic
workflow engine beyond what MaterialRequisition/PurchaseOrder need.

── Feature 53 (Vendor Master) design notes ──────────────────────────────────
No new vendor/supplier table is created here. `PurchaseOrder` and
`GoodsReceivedNote` reference `modules.masters.entities.organization_entities.Supplier`
(table `suppliers`) via FK — the enterprise-canonical vendor master. See that
entity's docstring for the additive nullable columns added to support
procurement (payment_terms, tax_id, contract_start/end, supplied_categories).
`modules/pharmacy`'s `PharmacySupplier` is untouched and remains pharmacy-scoped.
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


# ── Feature 49: Material Requisition ─────────────────────────────────────────


class MaterialRequisitionStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    fulfilled = "fulfilled"


class MaterialRequisition(Base):
    """Consumable/material requisition (generalized workflow analog of
    `modules.equipment.entities.equipment_entities.EquipmentRequest` — see
    module docstring for the MERGE decision)."""

    __tablename__ = "procurement_material_requisitions"
    __table_args__ = (
        UniqueConstraint("hospital_id", "requisition_no", name="uq_procurement_requisition_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requisition_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    requested_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department: Mapped[str] = mapped_column(String(128), nullable=False)
    consumable_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_consumable_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity_requested: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[MaterialRequisitionStatus] = mapped_column(
        Enum(MaterialRequisitionStatus, name="procurement_material_requisition_status"),
        nullable=False,
        default=MaterialRequisitionStatus.pending,
        index=True,
    )
    approved_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    item: Mapped["object"] = relationship("ConsumableItem")


# ── Feature 52: Approval Workflow ────────────────────────────────────────────


class ApprovalEntityType(str, enum.Enum):
    material_requisition = "material_requisition"
    purchase_order = "purchase_order"


class ApprovalDecision(str, enum.Enum):
    approved = "approved"
    rejected = "rejected"


class ApprovalThreshold(Base):
    """Configurable, optional approval tier for a given entity type. If no
    rows exist for a hospital + applies_to, a single approval step suffices
    (see module docstring)."""

    __tablename__ = "procurement_approval_thresholds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    applies_to: Mapped[ApprovalEntityType] = mapped_column(
        Enum(ApprovalEntityType, name="procurement_approval_applies_to"), nullable=False, index=True
    )
    min_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    approver_role: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ApprovalAction(Base):
    """Generic approval decision log. `entity_id` intentionally has no hard FK
    since it is polymorphic across MaterialRequisition/PurchaseOrder — see
    module docstring for the design rationale."""

    __tablename__ = "procurement_approval_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[ApprovalEntityType] = mapped_column(
        Enum(ApprovalEntityType, name="procurement_approval_entity_type"), nullable=False, index=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    approver_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="procurement_approval_decision"), nullable=False
    )
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sequence_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ── Feature 50: Purchase Order Processing ────────────────────────────────────


class PurchaseOrderStatus(str, enum.Enum):
    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    sent = "sent"
    partially_received = "partially_received"
    completed = "completed"
    cancelled = "cancelled"


class PurchaseOrder(Base):
    __tablename__ = "procurement_purchase_orders"
    __table_args__ = (
        UniqueConstraint("hospital_id", "po_number", name="uq_procurement_po_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    po_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    requisition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("procurement_material_requisitions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        Enum(PurchaseOrderStatus, name="procurement_purchase_order_status"),
        nullable=False,
        default=PurchaseOrderStatus.draft,
        index=True,
    )
    expected_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    supplier: Mapped["object"] = relationship("Supplier")
    items: Mapped[list["PurchaseOrderItem"]] = relationship(back_populates="purchase_order", cascade="all, delete-orphan")
    grns: Mapped[list["GoodsReceivedNote"]] = relationship(back_populates="purchase_order")


class PurchaseOrderItem(Base):
    __tablename__ = "procurement_purchase_order_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("procurement_purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    consumable_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_consumable_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity_ordered: Mapped[float] = mapped_column(Float, nullable=False)
    negotiated_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quantity_received: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="items")
    item: Mapped["object"] = relationship("ConsumableItem")


# ── Feature 51: Goods Received Note ──────────────────────────────────────────


class GoodsReceivedNote(Base):
    __tablename__ = "procurement_grns"
    __table_args__ = (
        UniqueConstraint("hospital_id", "grn_number", name="uq_procurement_grn_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    grn_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("procurement_purchase_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    supplier_invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="grns")
    items: Mapped[list["GoodsReceivedNoteItem"]] = relationship(back_populates="grn", cascade="all, delete-orphan")


class GoodsReceivedNoteItem(Base):
    __tablename__ = "procurement_grn_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    grn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("procurement_grns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_order_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("procurement_purchase_order_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    accepted_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rejected_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch_number: Mapped[str] = mapped_column(String(64), nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    grn: Mapped["GoodsReceivedNote"] = relationship(back_populates="items")
    purchase_order_item: Mapped["PurchaseOrderItem"] = relationship()
