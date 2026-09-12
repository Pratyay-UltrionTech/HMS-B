"""
Procurement Lifecycle domain business action handlers (Features 49-55).

See `modules.procurement.entities.procurement_entities` module docstring for
the Feature 49 MERGE decision and Feature 52/53 design notes.

Feature 51 (GRN) deliberately calls `InventoryActions.receive_stock` (imported
from `modules.inventory.actions.inventory_actions`) to post accepted goods
into inventory rather than re-implementing stock-receipt logic — this is the
single source of truth for "goods entering the building."
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.inventory.actions.inventory_actions import InventoryActions
from hms_migration.modules.inventory.contracts.inventory_contracts import ReceiveStockRequest
from hms_migration.modules.procurement.contracts.procurement_contracts import (
    ApprovalActionResponse,
    ApprovalDecisionRequest,
    ApprovalThresholdCreate,
    ApprovalThresholdResponse,
    ConsumptionTrendPoint,
    GrnCreate,
    GrnItemResponse,
    GrnResponse,
    MaterialRequisitionAction,
    MaterialRequisitionCreate,
    MaterialRequisitionResponse,
    ProcurementAnalyticsResponse,
    PurchaseOrderCreate,
    PurchaseOrderItemResponse,
    PurchaseOrderResponse,
    ReorderAlert,
    SupplierPerformanceResponse,
    SupplierSpend,
)
from hms_migration.modules.procurement.db.procurement_repository import ProcurementRepository
from hms_migration.modules.procurement.entities.procurement_entities import (
    ApprovalAction,
    ApprovalDecision,
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
from hms_migration.shared.audit import write_audit_log

# Forward status transitions permitted for a PurchaseOrder.
_PO_TRANSITIONS: dict[PurchaseOrderStatus, set[PurchaseOrderStatus]] = {
    PurchaseOrderStatus.draft: {PurchaseOrderStatus.pending_approval, PurchaseOrderStatus.cancelled},
    PurchaseOrderStatus.pending_approval: {PurchaseOrderStatus.approved, PurchaseOrderStatus.cancelled},
    PurchaseOrderStatus.approved: {PurchaseOrderStatus.sent, PurchaseOrderStatus.cancelled},
    PurchaseOrderStatus.sent: {PurchaseOrderStatus.partially_received, PurchaseOrderStatus.completed},
    PurchaseOrderStatus.partially_received: {PurchaseOrderStatus.completed},
    PurchaseOrderStatus.completed: set(),
    PurchaseOrderStatus.cancelled: set(),
}

# PO statuses a GRN may legitimately be recorded against.
_GRN_ELIGIBLE_PO_STATUSES = {
    PurchaseOrderStatus.approved,
    PurchaseOrderStatus.sent,
    PurchaseOrderStatus.partially_received,
}


def _requisition_to_response(row: MaterialRequisition) -> MaterialRequisitionResponse:
    return MaterialRequisitionResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        requisition_no=row.requisition_no,
        requested_by_staff_id=row.requested_by_staff_id,
        department=row.department,
        consumable_item_id=row.consumable_item_id,
        item_name=row.item.item_name if row.item else None,
        quantity_requested=row.quantity_requested,
        status=row.status,
        approved_by_staff_id=row.approved_by_staff_id,
        approval_notes=row.approval_notes,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        fulfilled_at=row.fulfilled_at,
    )


def _po_item_to_response(row: PurchaseOrderItem) -> PurchaseOrderItemResponse:
    return PurchaseOrderItemResponse(
        id=row.id,
        consumable_item_id=row.consumable_item_id,
        item_name=row.item.item_name if row.item else None,
        quantity_ordered=row.quantity_ordered,
        negotiated_rate=row.negotiated_rate,
        quantity_received=row.quantity_received,
    )


def _po_to_response(row: PurchaseOrder) -> PurchaseOrderResponse:
    return PurchaseOrderResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        po_number=row.po_number,
        supplier_id=row.supplier_id,
        supplier_name=row.supplier.name if row.supplier else None,
        requisition_id=row.requisition_id,
        status=row.status,
        expected_delivery_date=row.expected_delivery_date,
        terms=row.terms,
        created_by_staff_id=row.created_by_staff_id,
        created_at=row.created_at,
        approved_at=row.approved_at,
        sent_at=row.sent_at,
        completed_at=row.completed_at,
        items=[_po_item_to_response(i) for i in row.items],
    )


def _grn_item_to_response(row: GoodsReceivedNoteItem) -> GrnItemResponse:
    return GrnItemResponse.model_validate(row)


def _grn_to_response(row: GoodsReceivedNote) -> GrnResponse:
    return GrnResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        grn_number=row.grn_number,
        purchase_order_id=row.purchase_order_id,
        received_date=row.received_date,
        supplier_invoice_number=row.supplier_invoice_number,
        received_by_staff_id=row.received_by_staff_id,
        created_at=row.created_at,
        items=[_grn_item_to_response(i) for i in row.items],
    )


class ProcurementActions:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = ProcurementRepository(db)

    # ── Feature 49: Material Requisition ─────────────────────────────────────

    def list_requisitions(self, status_filter: str | None = None) -> list[MaterialRequisitionResponse]:
        status_enum = None
        if status_filter:
            try:
                status_enum = MaterialRequisitionStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
        rows = self.repo.list_requisitions(self.hospital_id, status_filter=status_enum)
        return [_requisition_to_response(r) for r in rows]

    def create_requisition(self, payload: MaterialRequisitionCreate) -> MaterialRequisitionResponse:
        item = self.repo.get_consumable_item(payload.consumable_item_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumable item not found")

        row = MaterialRequisition(
            hospital_id=self.hospital_id,
            requisition_no=self.repo.next_requisition_no(self.hospital_id),
            requested_by_staff_id=payload.requested_by_staff_id,
            department=payload.department.strip(),
            consumable_item_id=item.id,
            quantity_requested=payload.quantity_requested,
            status=MaterialRequisitionStatus.pending,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="procurement_material_requisition",
            entity_id=row.id,
            summary=f"Material requisition {row.requisition_no}: {item.item_code} x{row.quantity_requested}",
        )
        self.db.commit()
        row = self.repo.get_requisition_by_id(row.id, self.hospital_id)
        assert row is not None
        return _requisition_to_response(row)

    def approve_requisition(self, requisition_id: UUID, payload: MaterialRequisitionAction | None = None) -> MaterialRequisitionResponse:
        row = self.repo.get_requisition_by_id(requisition_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")
        if row.status != MaterialRequisitionStatus.pending:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending requisitions can be approved")

        row.status = MaterialRequisitionStatus.approved
        row.resolved_at = datetime.now(timezone.utc)
        if payload:
            row.approved_by_staff_id = payload.approved_by_staff_id
            if payload.approval_notes:
                row.approval_notes = payload.approval_notes.strip()

        self._log_approval(
            ApprovalEntityType.material_requisition,
            row.id,
            ApprovalDecision.approved,
            approver_staff_id=payload.approved_by_staff_id if payload else None,
            decision_notes=payload.approval_notes if payload else None,
        )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="procurement_material_requisition",
            entity_id=row.id,
            summary=f"Approved requisition {row.requisition_no}",
        )
        self.db.commit()
        row = self.repo.get_requisition_by_id(requisition_id, self.hospital_id)
        assert row is not None
        return _requisition_to_response(row)

    def reject_requisition(self, requisition_id: UUID, payload: MaterialRequisitionAction | None = None) -> MaterialRequisitionResponse:
        row = self.repo.get_requisition_by_id(requisition_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")
        if row.status != MaterialRequisitionStatus.pending:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending requisitions can be rejected")

        row.status = MaterialRequisitionStatus.rejected
        row.resolved_at = datetime.now(timezone.utc)
        if payload:
            row.approved_by_staff_id = payload.approved_by_staff_id
            if payload.approval_notes:
                row.approval_notes = payload.approval_notes.strip()

        self._log_approval(
            ApprovalEntityType.material_requisition,
            row.id,
            ApprovalDecision.rejected,
            approver_staff_id=payload.approved_by_staff_id if payload else None,
            decision_notes=payload.approval_notes if payload else None,
        )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="procurement_material_requisition",
            entity_id=row.id,
            summary=f"Rejected requisition {row.requisition_no}",
        )
        self.db.commit()
        row = self.repo.get_requisition_by_id(requisition_id, self.hospital_id)
        assert row is not None
        return _requisition_to_response(row)

    def fulfill_requisition(self, requisition_id: UUID) -> MaterialRequisitionResponse:
        """Marks an approved requisition fulfilled once goods are actually
        issued to the department. Deliberately action-layer only — it does not
        force-couple to Feature 48 transfer; callers that also want stock to
        physically move should separately drive an InventoryTransfer or
        InventoryActions.consume_departmental_stock."""
        row = self.repo.get_requisition_by_id(requisition_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")
        if row.status != MaterialRequisitionStatus.approved:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only approved requisitions can be fulfilled")

        row.status = MaterialRequisitionStatus.fulfilled
        row.fulfilled_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="procurement_material_requisition",
            entity_id=row.id,
            summary=f"Fulfilled requisition {row.requisition_no}",
        )
        self.db.commit()
        row = self.repo.get_requisition_by_id(requisition_id, self.hospital_id)
        assert row is not None
        return _requisition_to_response(row)

    # ── Feature 52: Approval Workflow ────────────────────────────────────────

    def _log_approval(
        self,
        entity_type: ApprovalEntityType,
        entity_id: UUID,
        decision: ApprovalDecision,
        approver_staff_id: str | None,
        decision_notes: str | None,
        sequence_order: int = 1,
    ) -> None:
        row = ApprovalAction(
            hospital_id=self.hospital_id,
            entity_type=entity_type,
            entity_id=entity_id,
            approver_staff_id=approver_staff_id,
            decision=decision,
            decision_notes=decision_notes.strip() if decision_notes else None,
            sequence_order=sequence_order,
        )
        self.db.add(row)
        self.db.flush()

    def create_threshold(self, payload: ApprovalThresholdCreate) -> ApprovalThresholdResponse:
        row = ApprovalThreshold(
            hospital_id=self.hospital_id,
            applies_to=payload.applies_to,
            min_amount=payload.min_amount,
            approver_role=payload.approver_role.strip(),
            sequence_order=payload.sequence_order,
            is_active=True,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return ApprovalThresholdResponse.model_validate(row)

    def list_thresholds(self, applies_to: str | None = None) -> list[ApprovalThresholdResponse]:
        q = self.db.query(ApprovalThreshold).filter(ApprovalThreshold.hospital_id == self.hospital_id)
        if applies_to:
            try:
                q = q.filter(ApprovalThreshold.applies_to == ApprovalEntityType(applies_to))
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid applies_to")
        rows = q.order_by(ApprovalThreshold.sequence_order.asc()).all()
        return [ApprovalThresholdResponse.model_validate(r) for r in rows]

    def list_approval_history(self, entity_type: str, entity_id: UUID) -> list[ApprovalActionResponse]:
        try:
            entity_type_enum = ApprovalEntityType(entity_type)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid entity_type")
        rows = self.repo.list_approval_actions(self.hospital_id, entity_type_enum, entity_id)
        return [ApprovalActionResponse.model_validate(r) for r in rows]

    # ── Feature 50: Purchase Order Processing ────────────────────────────────

    def list_pos(self, status_filter: str | None = None) -> list[PurchaseOrderResponse]:
        status_enum = None
        if status_filter:
            try:
                status_enum = PurchaseOrderStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
        rows = self.repo.list_pos(self.hospital_id, status_filter=status_enum)
        return [_po_to_response(r) for r in rows]

    def get_po(self, po_id: UUID) -> PurchaseOrderResponse:
        row = self.repo.get_po_by_id(po_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase order not found")
        return _po_to_response(row)

    def create_po(self, payload: PurchaseOrderCreate) -> PurchaseOrderResponse:
        supplier = self.repo.get_supplier(payload.supplier_id, self.hospital_id)
        if not supplier:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")
        if payload.requisition_id:
            req = self.repo.get_requisition_by_id(payload.requisition_id, self.hospital_id)
            if not req:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")

        row = PurchaseOrder(
            hospital_id=self.hospital_id,
            po_number=self.repo.next_po_number(self.hospital_id),
            supplier_id=supplier.id,
            requisition_id=payload.requisition_id,
            status=PurchaseOrderStatus.draft,
            expected_delivery_date=payload.expected_delivery_date,
            terms=payload.terms,
            created_by_staff_id=payload.created_by_staff_id,
        )
        self.db.add(row)
        self.db.flush()

        for item_payload in payload.items:
            item = self.repo.get_consumable_item(item_payload.consumable_item_id, self.hospital_id)
            if not item:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumable item not found")
            self.db.add(
                PurchaseOrderItem(
                    hospital_id=self.hospital_id,
                    purchase_order_id=row.id,
                    consumable_item_id=item.id,
                    quantity_ordered=item_payload.quantity_ordered,
                    negotiated_rate=item_payload.negotiated_rate,
                    quantity_received=0.0,
                )
            )
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="procurement_purchase_order",
            entity_id=row.id,
            summary=f"Created draft PO {row.po_number} for supplier {supplier.name}",
        )
        self.db.commit()
        row = self.repo.get_po_by_id(row.id, self.hospital_id)
        assert row is not None
        return _po_to_response(row)

    def _transition_po(self, po_id: UUID, target: PurchaseOrderStatus) -> PurchaseOrder:
        row = self.repo.get_po_by_id(po_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase order not found")
        allowed = _PO_TRANSITIONS.get(row.status, set())
        if target not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot transition PO from {row.status.value} to {target.value}",
            )
        return row

    def submit_po_for_approval(self, po_id: UUID) -> PurchaseOrderResponse:
        row = self._transition_po(po_id, PurchaseOrderStatus.pending_approval)
        row.status = PurchaseOrderStatus.pending_approval
        write_audit_log(
            self.db, hospital_id=self.hospital_id, actor=self.user, action="update",
            entity_type="procurement_purchase_order", entity_id=row.id,
            summary=f"Submitted PO {row.po_number} for approval",
        )
        self.db.commit()
        return _po_to_response(self.repo.get_po_by_id(po_id, self.hospital_id))

    def approve_po(self, po_id: UUID, payload: ApprovalDecisionRequest | None = None) -> PurchaseOrderResponse:
        row = self._transition_po(po_id, PurchaseOrderStatus.approved)
        row.status = PurchaseOrderStatus.approved
        row.approved_at = datetime.now(timezone.utc)
        self._log_approval(
            ApprovalEntityType.purchase_order,
            row.id,
            ApprovalDecision.approved,
            approver_staff_id=payload.approver_staff_id if payload else None,
            decision_notes=payload.decision_notes if payload else None,
        )
        write_audit_log(
            self.db, hospital_id=self.hospital_id, actor=self.user, action="update",
            entity_type="procurement_purchase_order", entity_id=row.id,
            summary=f"Approved PO {row.po_number}",
        )
        self.db.commit()
        return _po_to_response(self.repo.get_po_by_id(po_id, self.hospital_id))

    def cancel_po(self, po_id: UUID) -> PurchaseOrderResponse:
        row = self._transition_po(po_id, PurchaseOrderStatus.cancelled)
        row.status = PurchaseOrderStatus.cancelled
        write_audit_log(
            self.db, hospital_id=self.hospital_id, actor=self.user, action="update",
            entity_type="procurement_purchase_order", entity_id=row.id,
            summary=f"Cancelled PO {row.po_number}",
        )
        self.db.commit()
        return _po_to_response(self.repo.get_po_by_id(po_id, self.hospital_id))

    def mark_po_sent(self, po_id: UUID) -> PurchaseOrderResponse:
        row = self._transition_po(po_id, PurchaseOrderStatus.sent)
        row.status = PurchaseOrderStatus.sent
        row.sent_at = datetime.now(timezone.utc)
        write_audit_log(
            self.db, hospital_id=self.hospital_id, actor=self.user, action="update",
            entity_type="procurement_purchase_order", entity_id=row.id,
            summary=f"Marked PO {row.po_number} as sent to supplier",
        )
        self.db.commit()
        return _po_to_response(self.repo.get_po_by_id(po_id, self.hospital_id))

    # ── Feature 51: Goods Received Note ──────────────────────────────────────

    def record_grn(self, payload: GrnCreate) -> GrnResponse:
        po = self.repo.get_po_by_id(payload.purchase_order_id, self.hospital_id)
        if not po:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase order not found")
        if po.status not in _GRN_ELIGIBLE_PO_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot record goods against a PO in status {po.status.value}",
            )

        po_items_by_id = {i.id: i for i in po.items}

        grn = GoodsReceivedNote(
            hospital_id=self.hospital_id,
            grn_number=self.repo.next_grn_number(self.hospital_id),
            purchase_order_id=po.id,
            received_date=payload.received_date or date.today(),
            supplier_invoice_number=payload.supplier_invoice_number,
            received_by_staff_id=payload.received_by_staff_id,
        )
        self.db.add(grn)
        self.db.flush()

        inventory_actions = InventoryActions(self.db, self.hospital_id, self.user)

        for item_payload in payload.items:
            po_item = po_items_by_id.get(item_payload.purchase_order_item_id)
            if not po_item:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="purchase_order_item_id does not belong to this purchase order",
                )
            remaining = po_item.quantity_ordered - po_item.quantity_received
            claimed = item_payload.accepted_quantity + item_payload.rejected_quantity
            if claimed > remaining + 1e-9:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Over-receipt blocked for item {po_item.consumable_item_id}: "
                        f"remaining {remaining}, attempted {claimed}"
                    ),
                )

            self.db.add(
                GoodsReceivedNoteItem(
                    hospital_id=self.hospital_id,
                    grn_id=grn.id,
                    purchase_order_item_id=po_item.id,
                    accepted_quantity=item_payload.accepted_quantity,
                    rejected_quantity=item_payload.rejected_quantity,
                    rejection_reason=item_payload.rejection_reason,
                    batch_number=item_payload.batch_number.strip(),
                    expiry_date=item_payload.expiry_date,
                )
            )
            po_item.quantity_received = round(po_item.quantity_received + item_payload.accepted_quantity, 4)

            if item_payload.accepted_quantity > 0:
                inventory_actions.receive_stock(
                    ReceiveStockRequest(
                        item_id=po_item.consumable_item_id,
                        batch_number=item_payload.batch_number.strip(),
                        quantity=item_payload.accepted_quantity,
                        expiry_date=item_payload.expiry_date,
                        received_date=grn.received_date,
                        unit_cost=item_payload.unit_cost if item_payload.unit_cost is not None else po_item.negotiated_rate,
                        location="Main Store",
                        performed_by_staff_id=payload.received_by_staff_id,
                        notes=f"GRN {grn.grn_number} against PO {po.po_number}",
                    )
                )

        self.db.flush()

        # An item is "fully closed" once accepted + rejected quantities across
        # ALL its GRN lines reach quantity_ordered — a rejected item can never
        # be re-shipped against this PO in this simplified model, so rejected
        # quantity also counts toward closing the line (not just accepted).
        all_lines_closed = True
        for po_item in po.items:
            accounted = (
                self.db.query(GoodsReceivedNoteItem)
                .filter(GoodsReceivedNoteItem.purchase_order_item_id == po_item.id)
                .all()
            )
            total_accounted = sum(float(gi.accepted_quantity or 0) + float(gi.rejected_quantity or 0) for gi in accounted)
            if total_accounted < po_item.quantity_ordered - 1e-9:
                all_lines_closed = False
                break
        po.status = PurchaseOrderStatus.completed if all_lines_closed else PurchaseOrderStatus.partially_received
        if all_lines_closed:
            po.completed_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="procurement_grn",
            entity_id=grn.id,
            summary=f"Recorded GRN {grn.grn_number} against PO {po.po_number}",
        )
        self.db.commit()
        grn = self.repo.get_grn_by_id(grn.id, self.hospital_id)
        assert grn is not None
        return _grn_to_response(grn)

    def list_grns_for_po(self, po_id: UUID) -> list[GrnResponse]:
        rows = self.repo.list_grns_for_po(po_id, self.hospital_id)
        return [_grn_to_response(r) for r in rows]

    # ── Feature 54: Supplier Performance ─────────────────────────────────────

    def get_supplier_performance(self, supplier_id: UUID) -> SupplierPerformanceResponse:
        supplier = self.repo.get_supplier(supplier_id, self.hospital_id)
        if not supplier:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

        pos = list(self.repo.list_pos_for_supplier(supplier_id, self.hospital_id))
        grns = list(self.repo.list_grns_for_supplier(supplier_id, self.hospital_id))

        total_accepted = 0.0
        total_rejected = 0.0
        for grn in grns:
            for gi in grn.items:
                total_accepted += float(gi.accepted_quantity or 0)
                total_rejected += float(gi.rejected_quantity or 0)

        total_received_qty = total_accepted + total_rejected
        rejection_rate = (total_rejected / total_received_qty) if total_received_qty > 0 else None

        # On-time delivery: compare each PO's expected_delivery_date to its
        # first GRN's received_date. Only computed over POs that have both.
        grns_by_po: dict = {}
        for grn in grns:
            grns_by_po.setdefault(grn.purchase_order_id, []).append(grn)

        on_time_count = 0
        delivered_with_expectation = 0
        lead_times: list[float] = []
        for po in pos:
            po_grns = sorted(grns_by_po.get(po.id, []), key=lambda g: g.received_date)
            if not po_grns:
                continue
            first_grn = po_grns[0]
            if po.expected_delivery_date is not None:
                delivered_with_expectation += 1
                if first_grn.received_date <= po.expected_delivery_date:
                    on_time_count += 1
            if po.sent_at is not None:
                sent_at = po.sent_at if po.sent_at.tzinfo is not None else po.sent_at.replace(tzinfo=timezone.utc)
                delta_days = (
                    datetime.combine(first_grn.received_date, datetime.min.time()).replace(tzinfo=timezone.utc)
                    - sent_at
                ).total_seconds() / 86400.0
                if delta_days >= 0:
                    lead_times.append(delta_days)

        on_time_rate = (on_time_count / delivered_with_expectation) if delivered_with_expectation > 0 else None
        avg_lead_time = (sum(lead_times) / len(lead_times)) if lead_times else None

        return SupplierPerformanceResponse(
            supplier_id=supplier.id,
            supplier_name=supplier.name,
            total_purchase_orders=len(pos),
            total_grns=len(grns),
            on_time_delivery_rate=on_time_rate,
            total_accepted_quantity=round(total_accepted, 4),
            total_rejected_quantity=round(total_rejected, 4),
            rejection_rate=rejection_rate,
            avg_days_po_sent_to_first_grn=avg_lead_time,
        )

    # ── Feature 55: Purchase & Consumption Analytics ─────────────────────────

    def get_analytics(self) -> ProcurementAnalyticsResponse:
        pos = list(self.repo.list_all_pos(self.hospital_id))

        spend_by_supplier: dict = {}
        for po in pos:
            spend = sum(float(i.quantity_ordered) * float(i.negotiated_rate) for i in po.items)
            key = po.supplier_id
            if key not in spend_by_supplier:
                spend_by_supplier[key] = {
                    "supplier_id": po.supplier_id,
                    "supplier_name": po.supplier.name if po.supplier else None,
                    "total_spend": 0.0,
                    "po_count": 0,
                }
            spend_by_supplier[key]["total_spend"] += spend
            spend_by_supplier[key]["po_count"] += 1

        spend_list = [
            SupplierSpend(
                supplier_id=v["supplier_id"],
                supplier_name=v["supplier_name"],
                total_spend=round(v["total_spend"], 2),
                po_count=v["po_count"],
            )
            for v in spend_by_supplier.values()
        ]

        pending_statuses = {
            PurchaseOrderStatus.draft,
            PurchaseOrderStatus.pending_approval,
            PurchaseOrderStatus.approved,
            PurchaseOrderStatus.sent,
            PurchaseOrderStatus.partially_received,
        }
        pending_count = sum(1 for po in pos if po.status in pending_statuses)

        # Consumption trends: real data from inventory's stock transaction ledger.
        from hms_migration.modules.inventory.entities.inventory_entities import (
            ConsumableItem,
            InventoryStockTransaction,
            InventoryStockTransactionType,
        )

        consumption_rows = (
            self.db.query(InventoryStockTransaction)
            .filter(
                InventoryStockTransaction.hospital_id == self.hospital_id,
                InventoryStockTransaction.transaction_type == InventoryStockTransactionType.consumption,
            )
            .all()
        )
        consumption_by_item: dict = {}
        for r in consumption_rows:
            consumption_by_item[r.item_id] = consumption_by_item.get(r.item_id, 0.0) + abs(float(r.quantity))

        item_ids = list(consumption_by_item.keys())
        items_map = {}
        if item_ids:
            items_map = {
                i.id: i
                for i in self.db.query(ConsumableItem)
                .filter(ConsumableItem.id.in_(item_ids), ConsumableItem.hospital_id == self.hospital_id)
                .all()
            }
        consumption_trends = [
            ConsumptionTrendPoint(
                item_id=item_id,
                item_name=items_map[item_id].item_name if item_id in items_map else None,
                total_consumed=round(qty, 4),
            )
            for item_id, qty in consumption_by_item.items()
        ]

        # Reorder alerts: real comparison of reorder_level vs CentralInventoryStock.
        from hms_migration.modules.inventory.entities.inventory_entities import CentralInventoryStock

        alerts: list[ReorderAlert] = []
        central_rows = (
            self.db.query(CentralInventoryStock)
            .filter(CentralInventoryStock.hospital_id == self.hospital_id)
            .all()
        )
        for c in central_rows:
            item = c.item
            if item and float(c.total_quantity or 0) <= float(item.reorder_level or 0):
                alerts.append(
                    ReorderAlert(
                        item_id=item.id,
                        item_name=item.item_name,
                        reorder_level=item.reorder_level,
                        current_quantity=c.total_quantity,
                    )
                )

        return ProcurementAnalyticsResponse(
            spend_by_supplier=spend_list,
            consumption_trends=consumption_trends,
            pending_orders_count=pending_count,
            reorder_alerts=alerts,
        )
