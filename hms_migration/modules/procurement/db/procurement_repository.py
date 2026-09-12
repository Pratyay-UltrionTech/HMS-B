"""
Repository handling database lookups for the Procurement Lifecycle domain.
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.inventory.entities.inventory_entities import ConsumableItem
from hms_migration.modules.masters.entities.organization_entities import Supplier
from hms_migration.modules.procurement.entities.procurement_entities import (
    ApprovalAction,
    ApprovalEntityType,
    ApprovalThreshold,
    GoodsReceivedNote,
    GoodsReceivedNoteItem,
    MaterialRequisition,
    MaterialRequisitionStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
)


class ProcurementRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Consumable / Supplier lookups (shared masters) ───────────────────────

    def get_consumable_item(self, item_id: UUID, hospital_id: UUID) -> ConsumableItem | None:
        return (
            self.db.query(ConsumableItem)
            .filter(ConsumableItem.id == item_id, ConsumableItem.hospital_id == hospital_id)
            .first()
        )

    def get_supplier(self, supplier_id: UUID, hospital_id: UUID) -> Supplier | None:
        return (
            self.db.query(Supplier)
            .filter(Supplier.id == supplier_id, Supplier.hospital_id == hospital_id)
            .first()
        )

    # ── Feature 49: Material Requisitions ────────────────────────────────────

    def next_requisition_no(self, hospital_id: UUID) -> str:
        count = self.db.query(MaterialRequisition).filter(MaterialRequisition.hospital_id == hospital_id).count()
        return f"MR{count + 1:06d}"

    def get_requisition_by_id(self, requisition_id: UUID, hospital_id: UUID) -> MaterialRequisition | None:
        return (
            self.db.query(MaterialRequisition)
            .options(joinedload(MaterialRequisition.item))
            .filter(MaterialRequisition.id == requisition_id, MaterialRequisition.hospital_id == hospital_id)
            .first()
        )

    def list_requisitions(
        self, hospital_id: UUID, status_filter: MaterialRequisitionStatus | None = None
    ) -> Sequence[MaterialRequisition]:
        q = (
            self.db.query(MaterialRequisition)
            .options(joinedload(MaterialRequisition.item))
            .filter(MaterialRequisition.hospital_id == hospital_id)
        )
        if status_filter:
            q = q.filter(MaterialRequisition.status == status_filter)
        return q.order_by(MaterialRequisition.created_at.desc()).limit(1000).all()

    # ── Feature 52: Approval Workflow ────────────────────────────────────────

    def list_active_thresholds(self, hospital_id: UUID, applies_to: ApprovalEntityType) -> Sequence[ApprovalThreshold]:
        return (
            self.db.query(ApprovalThreshold)
            .filter(
                ApprovalThreshold.hospital_id == hospital_id,
                ApprovalThreshold.applies_to == applies_to,
                ApprovalThreshold.is_active.is_(True),
            )
            .order_by(ApprovalThreshold.sequence_order.asc())
            .all()
        )

    def list_approval_actions(self, hospital_id: UUID, entity_type: ApprovalEntityType, entity_id: UUID) -> Sequence[ApprovalAction]:
        return (
            self.db.query(ApprovalAction)
            .filter(
                ApprovalAction.hospital_id == hospital_id,
                ApprovalAction.entity_type == entity_type,
                ApprovalAction.entity_id == entity_id,
            )
            .order_by(ApprovalAction.sequence_order.asc())
            .all()
        )

    # ── Feature 50: Purchase Orders ──────────────────────────────────────────

    def next_po_number(self, hospital_id: UUID) -> str:
        count = self.db.query(PurchaseOrder).filter(PurchaseOrder.hospital_id == hospital_id).count()
        return f"PO{count + 1:06d}"

    def get_po_by_id(self, po_id: UUID, hospital_id: UUID) -> PurchaseOrder | None:
        return (
            self.db.query(PurchaseOrder)
            .options(joinedload(PurchaseOrder.items).joinedload(PurchaseOrderItem.item), joinedload(PurchaseOrder.supplier))
            .filter(PurchaseOrder.id == po_id, PurchaseOrder.hospital_id == hospital_id)
            .first()
        )

    def list_pos(self, hospital_id: UUID, status_filter: PurchaseOrderStatus | None = None) -> Sequence[PurchaseOrder]:
        q = (
            self.db.query(PurchaseOrder)
            .options(joinedload(PurchaseOrder.items).joinedload(PurchaseOrderItem.item), joinedload(PurchaseOrder.supplier))
            .filter(PurchaseOrder.hospital_id == hospital_id)
        )
        if status_filter:
            q = q.filter(PurchaseOrder.status == status_filter)
        return q.order_by(PurchaseOrder.created_at.desc()).limit(1000).all()

    def get_po_item_by_id(self, po_item_id: UUID, hospital_id: UUID) -> PurchaseOrderItem | None:
        return (
            self.db.query(PurchaseOrderItem)
            .filter(PurchaseOrderItem.id == po_item_id, PurchaseOrderItem.hospital_id == hospital_id)
            .with_for_update()
            .first()
        )

    # ── Feature 51: Goods Received Notes ─────────────────────────────────────

    def next_grn_number(self, hospital_id: UUID) -> str:
        count = self.db.query(GoodsReceivedNote).filter(GoodsReceivedNote.hospital_id == hospital_id).count()
        return f"GRN{count + 1:06d}"

    def get_grn_by_id(self, grn_id: UUID, hospital_id: UUID) -> GoodsReceivedNote | None:
        return (
            self.db.query(GoodsReceivedNote)
            .options(joinedload(GoodsReceivedNote.items))
            .filter(GoodsReceivedNote.id == grn_id, GoodsReceivedNote.hospital_id == hospital_id)
            .first()
        )

    def list_grns_for_po(self, po_id: UUID, hospital_id: UUID) -> Sequence[GoodsReceivedNote]:
        return (
            self.db.query(GoodsReceivedNote)
            .filter(GoodsReceivedNote.purchase_order_id == po_id, GoodsReceivedNote.hospital_id == hospital_id)
            .order_by(GoodsReceivedNote.received_date.asc())
            .all()
        )

    # ── Feature 54/55: Analytics lookups ─────────────────────────────────────

    def list_pos_for_supplier(self, supplier_id: UUID, hospital_id: UUID) -> Sequence[PurchaseOrder]:
        return (
            self.db.query(PurchaseOrder)
            .options(joinedload(PurchaseOrder.items))
            .filter(PurchaseOrder.supplier_id == supplier_id, PurchaseOrder.hospital_id == hospital_id)
            .all()
        )

    def list_grns_for_supplier(self, supplier_id: UUID, hospital_id: UUID) -> Sequence[GoodsReceivedNote]:
        return (
            self.db.query(GoodsReceivedNote)
            .join(PurchaseOrder, GoodsReceivedNote.purchase_order_id == PurchaseOrder.id)
            .options(joinedload(GoodsReceivedNote.items))
            .filter(PurchaseOrder.supplier_id == supplier_id, GoodsReceivedNote.hospital_id == hospital_id)
            .all()
        )

    def list_all_pos(self, hospital_id: UUID) -> Sequence[PurchaseOrder]:
        return (
            self.db.query(PurchaseOrder)
            .options(
                joinedload(PurchaseOrder.items).joinedload(PurchaseOrderItem.item),
                joinedload(PurchaseOrder.supplier),
            )
            .filter(PurchaseOrder.hospital_id == hospital_id)
            .all()
        )
