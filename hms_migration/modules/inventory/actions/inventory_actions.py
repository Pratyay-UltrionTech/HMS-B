"""
Inventory domain business action handlers (Features 44-48).

All stock mutations go through this module — never through raw handler/DB mutation —
mirroring the discipline in `modules/pharmacy/services/pharmacy_stock_service.py`:
every quantity change on a batch, central stock row, or departmental stock row is
paired with an `InventoryStockTransaction` ledger entry in the same DB transaction.

Enforced lifecycle rules (Feature 48):
- InventoryTransfer: requested -> approved -> dispatched -> completed, or
  requested/approved -> rejected. No other transition is permitted.
- Stock only actually moves on the dispatched -> completed transition. This is a
  deliberate choice (see spec) so that a transfer rejected or stalled at
  approved/dispatched never double-counts stock that was never physically moved.
- On completion: source stock (CentralInventoryStock if from_location == "Central",
  else the source DepartmentalStock row) is decremented, the destination
  DepartmentalStock row is incremented (created if missing), and two ledger rows
  are written (transfer_out from source, transfer_in to destination), both carrying
  reference_type="inventory_transfer" / reference_id=transfer.id.

Departmental consumption (`consume_departmental_stock`) uses transaction_type=
`consumption` (distinct from `issue_to_department`, which is reserved for the
transfer-in leg of a central/inter-department transfer landing in a department).
Consumption can never take a department's stock negative.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.inventory.contracts.inventory_contracts import (
    CentralInventoryStockResponse,
    ConsumableBatchResponse,
    ConsumableItemCreate,
    ConsumableItemResponse,
    ConsumableItemUpdate,
    ConsumeDepartmentalStockRequest,
    DepartmentalStockResponse,
    InventoryStockTransactionResponse,
    InventoryTransferApprove,
    InventoryTransferCreate,
    InventoryTransferReject,
    InventoryTransferResponse,
    ReceiveStockRequest,
)
from hms_migration.modules.inventory.db.inventory_repository import InventoryRepository
from hms_migration.modules.inventory.entities.inventory_entities import (
    CentralInventoryStock,
    ConsumableBatch,
    ConsumableItem,
    DepartmentalStock,
    InventoryStockTransaction,
    InventoryStockTransactionType,
    InventoryTransfer,
    InventoryTransferStatus,
)
from hms_migration.shared.audit import write_audit_log

# Valid forward transitions for an inventory transfer's status.
_TRANSFER_TRANSITIONS: dict[InventoryTransferStatus, set[InventoryTransferStatus]] = {
    InventoryTransferStatus.requested: {InventoryTransferStatus.approved, InventoryTransferStatus.rejected},
    InventoryTransferStatus.approved: {InventoryTransferStatus.dispatched, InventoryTransferStatus.rejected},
    InventoryTransferStatus.dispatched: {InventoryTransferStatus.completed},
    InventoryTransferStatus.completed: set(),
    InventoryTransferStatus.rejected: set(),
}


def _item_to_response(row: ConsumableItem) -> ConsumableItemResponse:
    return ConsumableItemResponse.model_validate(row)


def _batch_to_response(row: ConsumableBatch) -> ConsumableBatchResponse:
    return ConsumableBatchResponse.model_validate(row)


def _central_to_response(row: CentralInventoryStock) -> CentralInventoryStockResponse:
    return CentralInventoryStockResponse(
        id=row.id,
        item_id=row.item_id,
        item_name=row.item.item_name if row.item else None,
        total_quantity=row.total_quantity,
        location=row.location,
        updated_at=row.updated_at,
    )


def _dept_to_response(row: DepartmentalStock) -> DepartmentalStockResponse:
    return DepartmentalStockResponse(
        id=row.id,
        item_id=row.item_id,
        item_name=row.item.item_name if row.item else None,
        department=row.department,
        quantity=row.quantity,
        updated_at=row.updated_at,
    )


def _transaction_to_response(row: InventoryStockTransaction) -> InventoryStockTransactionResponse:
    return InventoryStockTransactionResponse.model_validate(row)


def _transfer_to_response(row: InventoryTransfer) -> InventoryTransferResponse:
    return InventoryTransferResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        item_id=row.item_id,
        item_name=row.item.item_name if row.item else None,
        from_location=row.from_location,
        to_department=row.to_department,
        quantity=row.quantity,
        status=row.status,
        requested_by_staff_id=row.requested_by_staff_id,
        approved_by_staff_id=row.approved_by_staff_id,
        requested_at=row.requested_at,
        approved_at=row.approved_at,
        dispatched_at=row.dispatched_at,
        completed_at=row.completed_at,
        rejection_reason=row.rejection_reason,
    )


class InventoryActions:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = InventoryRepository(db)

    # ── Feature 45: Non-Drug Consumable Master ───────────────────────────────

    def list_items(self, active_only: bool | None = None) -> list[ConsumableItemResponse]:
        rows = self.repo.list_items(self.hospital_id, active_only=active_only)
        return [_item_to_response(r) for r in rows]

    def get_item(self, item_id: UUID) -> ConsumableItemResponse:
        row = self.repo.get_item_by_id(item_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumable item not found")
        return _item_to_response(row)

    def create_item(self, payload: ConsumableItemCreate) -> ConsumableItemResponse:
        item_code = (payload.item_code or "").strip().upper()
        if not item_code:
            item_code = self.repo.next_item_code(self.hospital_id)
        if self.repo.get_item_by_code(self.hospital_id, item_code):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Item code already exists")

        row = ConsumableItem(
            hospital_id=self.hospital_id,
            item_code=item_code,
            item_name=payload.item_name.strip(),
            category=payload.category,
            unit_of_issue=payload.unit_of_issue.strip(),
            reorder_level=payload.reorder_level,
            specification=payload.specification,
            is_active=payload.is_active,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="inventory_consumable_item",
            entity_id=row.id,
            summary=f"Created consumable item {row.item_code} — {row.item_name}",
        )
        self.db.commit()
        self.db.refresh(row)
        return _item_to_response(row)

    def update_item(self, item_id: UUID, payload: ConsumableItemUpdate) -> ConsumableItemResponse:
        row = self.repo.get_item_by_id(item_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumable item not found")

        data = payload.model_dump(exclude_unset=True)
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip() or None
            setattr(row, k, v)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="inventory_consumable_item",
            entity_id=row.id,
            summary=f"Updated consumable item {row.item_code}",
        )
        self.db.commit()
        self.db.refresh(row)
        return _item_to_response(row)

    # ── Feature 46: Batches (read) ────────────────────────────────────────────

    def list_batches(self, item_id: UUID) -> list[ConsumableBatchResponse]:
        if not self.repo.get_item_by_id(item_id, self.hospital_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumable item not found")
        rows = self.repo.list_batches_for_item(item_id, self.hospital_id)
        return [_batch_to_response(r) for r in rows]

    # ── Ledger (read) ─────────────────────────────────────────────────────────

    def list_transactions(self, item_id: UUID | None = None) -> list[InventoryStockTransactionResponse]:
        if item_id is not None and not self.repo.get_item_by_id(item_id, self.hospital_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumable item not found")
        rows = self.repo.list_transactions(self.hospital_id, item_id=item_id)
        return [_transaction_to_response(r) for r in rows]

    # ── Feature 44: Central Inventory ────────────────────────────────────────

    def list_central_stock(self) -> list[CentralInventoryStockResponse]:
        rows = self.repo.list_central_stock(self.hospital_id)
        return [_central_to_response(r) for r in rows]

    def receive_stock(self, payload: ReceiveStockRequest) -> CentralInventoryStockResponse:
        """Entry point for ALL new stock into the system (GRN in Feature 51 will call
        this too). Creates or increments a ConsumableBatch by batch_number, increments
        CentralInventoryStock.total_quantity, and writes a `receipt` ledger entry."""
        item = self.repo.get_item_by_id(payload.item_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumable item not found")

        batch_number = payload.batch_number.strip()
        batch = self.repo.get_batch_by_number(self.hospital_id, item.id, batch_number)
        if batch:
            batch.received_quantity = round(float(batch.received_quantity or 0) + payload.quantity, 4)
            batch.remaining_quantity = round(float(batch.remaining_quantity or 0) + payload.quantity, 4)
            if payload.unit_cost is not None:
                batch.unit_cost = payload.unit_cost
            if payload.expiry_date is not None:
                batch.expiry_date = payload.expiry_date
        else:
            batch = ConsumableBatch(
                hospital_id=self.hospital_id,
                item_id=item.id,
                batch_number=batch_number,
                received_quantity=payload.quantity,
                issued_quantity=0.0,
                remaining_quantity=payload.quantity,
                expiry_date=payload.expiry_date,
                received_date=payload.received_date or date.today(),
                unit_cost=payload.unit_cost,
                is_active=True,
            )
            self.db.add(batch)
        self.db.flush()

        central = self.repo.get_central_stock(item.id, self.hospital_id, for_update=True)
        if not central:
            central = CentralInventoryStock(
                hospital_id=self.hospital_id,
                item_id=item.id,
                total_quantity=0.0,
                location=payload.location,
            )
            self.db.add(central)
            self.db.flush()
        central.total_quantity = round(float(central.total_quantity or 0) + payload.quantity, 4)
        central.location = payload.location or central.location

        txn = InventoryStockTransaction(
            hospital_id=self.hospital_id,
            item_id=item.id,
            batch_id=batch.id,
            transaction_type=InventoryStockTransactionType.receipt,
            quantity=round(payload.quantity, 4),
            to_location=payload.location,
            performed_by_staff_id=payload.performed_by_staff_id,
            notes=payload.notes,
        )
        self.db.add(txn)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="inventory_stock_transaction",
            entity_id=txn.id,
            summary=(
                f"Received {payload.quantity} of {item.item_code} "
                f"(batch {batch_number}) into {payload.location}"
            ),
        )
        self.db.commit()
        self.db.refresh(central)
        return _central_to_response(central)

    # ── Feature 47: Departmental Stock ────────────────────────────────────────

    def list_departmental_stock(self, department: str | None = None) -> list[DepartmentalStockResponse]:
        rows = self.repo.list_departmental_stock(self.hospital_id, department=department)
        return [_dept_to_response(r) for r in rows]

    def consume_departmental_stock(self, payload: ConsumeDepartmentalStockRequest) -> DepartmentalStockResponse:
        """Ward usage: decrements departmental stock. Cannot go negative."""
        item = self.repo.get_item_by_id(payload.item_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumable item not found")

        dept_stock = self.repo.get_departmental_stock(
            item.id, payload.department.strip(), self.hospital_id, for_update=True
        )
        available = float(dept_stock.quantity) if dept_stock else 0.0
        if available < payload.quantity - 1e-9:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Insufficient departmental stock for {item.item_code} in "
                    f"{payload.department} (available {available}, requested {payload.quantity})"
                ),
            )

        dept_stock.quantity = round(available - payload.quantity, 4)

        txn = InventoryStockTransaction(
            hospital_id=self.hospital_id,
            item_id=item.id,
            batch_id=None,
            transaction_type=InventoryStockTransactionType.consumption,
            quantity=-round(payload.quantity, 4),
            from_location=payload.department,
            performed_by_staff_id=payload.performed_by_staff_id,
            notes=payload.notes,
        )
        self.db.add(txn)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="inventory_departmental_stock",
            entity_id=dept_stock.id,
            summary=f"Consumed {payload.quantity} of {item.item_code} in {payload.department}",
        )
        self.db.commit()
        self.db.refresh(dept_stock)
        return _dept_to_response(dept_stock)

    # ── Feature 48: Inter-Department Transfer ────────────────────────────────

    def list_transfers(self, status_filter: str | None = None) -> list[InventoryTransferResponse]:
        status_enum = None
        if status_filter:
            try:
                status_enum = InventoryTransferStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
        rows = self.repo.list_transfers(self.hospital_id, status_filter=status_enum)
        return [_transfer_to_response(r) for r in rows]

    def get_transfer(self, transfer_id: UUID) -> InventoryTransferResponse:
        row = self.repo.get_transfer_by_id(transfer_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")
        return _transfer_to_response(row)

    def request_transfer(self, payload: InventoryTransferCreate) -> InventoryTransferResponse:
        item = self.repo.get_item_by_id(payload.item_id, self.hospital_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumable item not found")

        row = InventoryTransfer(
            hospital_id=self.hospital_id,
            item_id=item.id,
            from_location=payload.from_location.strip(),
            to_department=payload.to_department.strip(),
            quantity=payload.quantity,
            status=InventoryTransferStatus.requested,
            requested_by_staff_id=payload.requested_by_staff_id,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="inventory_transfer",
            entity_id=row.id,
            summary=(
                f"Requested transfer of {row.quantity} {item.item_code} "
                f"from {row.from_location} to {row.to_department}"
            ),
        )
        self.db.commit()
        self.db.refresh(row)
        return _transfer_to_response(row)

    def _transition_transfer(
        self, transfer_id: UUID, target: InventoryTransferStatus, note: str
    ) -> InventoryTransfer:
        row = self.repo.get_transfer_by_id(transfer_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")
        allowed = _TRANSFER_TRANSITIONS.get(row.status, set())
        if target not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot transition transfer from {row.status.value} to {target.value}. "
                    "Allowed sequence is requested -> approved -> dispatched -> completed "
                    "(or requested/approved -> rejected)."
                ),
            )
        return row

    def approve_transfer(self, transfer_id: UUID, payload: InventoryTransferApprove) -> InventoryTransferResponse:
        row = self._transition_transfer(transfer_id, InventoryTransferStatus.approved, "approve")
        row.status = InventoryTransferStatus.approved
        row.approved_by_staff_id = payload.approved_by_staff_id
        row.approved_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="inventory_transfer",
            entity_id=row.id,
            summary=f"Approved transfer {row.id}",
        )
        self.db.commit()
        self.db.refresh(row)
        return _transfer_to_response(row)

    def dispatch_transfer(self, transfer_id: UUID) -> InventoryTransferResponse:
        row = self._transition_transfer(transfer_id, InventoryTransferStatus.dispatched, "dispatch")
        row.status = InventoryTransferStatus.dispatched
        row.dispatched_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="inventory_transfer",
            entity_id=row.id,
            summary=f"Dispatched transfer {row.id}",
        )
        self.db.commit()
        self.db.refresh(row)
        return _transfer_to_response(row)

    def reject_transfer(self, transfer_id: UUID, payload: InventoryTransferReject) -> InventoryTransferResponse:
        """requested or approved -> rejected. No stock has moved yet, so nothing to undo."""
        row = self.repo.get_transfer_by_id(transfer_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")
        if row.status not in (InventoryTransferStatus.requested, InventoryTransferStatus.approved):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only a requested or approved transfer can be rejected",
            )

        row.status = InventoryTransferStatus.rejected
        row.rejection_reason = payload.rejection_reason

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="inventory_transfer",
            entity_id=row.id,
            summary=f"Rejected transfer {row.id}: {payload.rejection_reason or ''}".strip(),
        )
        self.db.commit()
        self.db.refresh(row)
        return _transfer_to_response(row)

    def complete_transfer(self, transfer_id: UUID) -> InventoryTransferResponse:
        """Only transition that actually moves stock. Decrements the source
        (CentralInventoryStock if from_location == 'Central', else the source
        department's DepartmentalStock), increments/creates the destination
        DepartmentalStock, and writes both a transfer_out and a transfer_in ledger
        entry referencing this transfer's id."""
        row = self._transition_transfer(transfer_id, InventoryTransferStatus.completed, "complete")
        item = self.repo.get_item_by_id(row.item_id, self.hospital_id)
        quantity = float(row.quantity)

        if row.from_location.strip().lower() == "central":
            source_central = self.repo.get_central_stock(row.item_id, self.hospital_id, for_update=True)
            available = float(source_central.total_quantity) if source_central else 0.0
            if available < quantity - 1e-9:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Insufficient central stock for {item.item_code} (available {available}, requested {quantity})",
                )
            source_central.total_quantity = round(available - quantity, 4)
        else:
            source_dept = self.repo.get_departmental_stock(
                row.item_id, row.from_location, self.hospital_id, for_update=True
            )
            available = float(source_dept.quantity) if source_dept else 0.0
            if available < quantity - 1e-9:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient stock in {row.from_location} for {item.item_code} "
                        f"(available {available}, requested {quantity})"
                    ),
                )
            source_dept.quantity = round(available - quantity, 4)

        dest_dept = self.repo.get_departmental_stock(row.item_id, row.to_department, self.hospital_id, for_update=True)
        if not dest_dept:
            dest_dept = DepartmentalStock(
                hospital_id=self.hospital_id,
                item_id=row.item_id,
                department=row.to_department,
                quantity=0.0,
            )
            self.db.add(dest_dept)
            self.db.flush()
        dest_dept.quantity = round(float(dest_dept.quantity or 0) + quantity, 4)

        out_txn = InventoryStockTransaction(
            hospital_id=self.hospital_id,
            item_id=row.item_id,
            batch_id=None,
            transaction_type=InventoryStockTransactionType.transfer_out,
            quantity=-round(quantity, 4),
            from_location=row.from_location,
            to_location=row.to_department,
            reference_type="inventory_transfer",
            reference_id=row.id,
        )
        in_txn = InventoryStockTransaction(
            hospital_id=self.hospital_id,
            item_id=row.item_id,
            batch_id=None,
            transaction_type=InventoryStockTransactionType.transfer_in,
            quantity=round(quantity, 4),
            from_location=row.from_location,
            to_location=row.to_department,
            reference_type="inventory_transfer",
            reference_id=row.id,
        )
        self.db.add(out_txn)
        self.db.add(in_txn)

        row.status = InventoryTransferStatus.completed
        row.completed_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="inventory_transfer",
            entity_id=row.id,
            summary=(
                f"Completed transfer {row.id}: moved {quantity} {item.item_code} "
                f"from {row.from_location} to {row.to_department}"
            ),
        )
        self.db.commit()
        self.db.refresh(row)
        return _transfer_to_response(row)
