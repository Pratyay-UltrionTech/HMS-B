"""
Inventory repository handling database lookups for the non-drug consumable domain.
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.inventory.entities.inventory_entities import (
    CentralInventoryStock,
    ConsumableBatch,
    ConsumableItem,
    DepartmentalStock,
    InventoryStockTransaction,
    InventoryTransfer,
)


class InventoryRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Consumable Items (Feature 45) ────────────────────────────────────────

    def get_item_by_id(self, item_id: UUID, hospital_id: UUID) -> ConsumableItem | None:
        return (
            self.db.query(ConsumableItem)
            .filter(ConsumableItem.id == item_id, ConsumableItem.hospital_id == hospital_id)
            .first()
        )

    def get_item_by_code(self, hospital_id: UUID, item_code: str) -> ConsumableItem | None:
        return (
            self.db.query(ConsumableItem)
            .filter(ConsumableItem.hospital_id == hospital_id, ConsumableItem.item_code == item_code)
            .first()
        )

    def list_items(self, hospital_id: UUID, active_only: bool | None = None) -> Sequence[ConsumableItem]:
        q = self.db.query(ConsumableItem).filter(ConsumableItem.hospital_id == hospital_id)
        if active_only:
            q = q.filter(ConsumableItem.is_active.is_(True))
        return q.order_by(ConsumableItem.item_code.asc()).limit(1000).all()

    def next_item_code(self, hospital_id: UUID) -> str:
        count = self.db.query(ConsumableItem).filter(ConsumableItem.hospital_id == hospital_id).count()
        return f"CI{count + 1:05d}"

    # ── Batches (Feature 46) ──────────────────────────────────────────────────

    def get_batch_by_number(self, hospital_id: UUID, item_id: UUID, batch_number: str) -> ConsumableBatch | None:
        return (
            self.db.query(ConsumableBatch)
            .filter(
                ConsumableBatch.hospital_id == hospital_id,
                ConsumableBatch.item_id == item_id,
                ConsumableBatch.batch_number == batch_number,
            )
            .with_for_update()
            .first()
        )

    def get_batch_by_id(self, batch_id: UUID, hospital_id: UUID) -> ConsumableBatch | None:
        return (
            self.db.query(ConsumableBatch)
            .filter(ConsumableBatch.id == batch_id, ConsumableBatch.hospital_id == hospital_id)
            .first()
        )

    def list_batches_for_item(self, item_id: UUID, hospital_id: UUID) -> Sequence[ConsumableBatch]:
        return (
            self.db.query(ConsumableBatch)
            .filter(ConsumableBatch.item_id == item_id, ConsumableBatch.hospital_id == hospital_id)
            .order_by(ConsumableBatch.received_date.asc())
            .all()
        )

    # ── Central Stock (Feature 44) ───────────────────────────────────────────

    def get_central_stock(self, item_id: UUID, hospital_id: UUID, for_update: bool = False) -> CentralInventoryStock | None:
        q = self.db.query(CentralInventoryStock).filter(
            CentralInventoryStock.item_id == item_id, CentralInventoryStock.hospital_id == hospital_id
        )
        if for_update:
            q = q.with_for_update()
        return q.first()

    def list_central_stock(self, hospital_id: UUID) -> Sequence[CentralInventoryStock]:
        return (
            self.db.query(CentralInventoryStock)
            .options(joinedload(CentralInventoryStock.item))
            .filter(CentralInventoryStock.hospital_id == hospital_id)
            .all()
        )

    # ── Departmental Stock (Feature 47) ──────────────────────────────────────

    def get_departmental_stock(
        self, item_id: UUID, department: str, hospital_id: UUID, for_update: bool = False
    ) -> DepartmentalStock | None:
        q = self.db.query(DepartmentalStock).filter(
            DepartmentalStock.item_id == item_id,
            DepartmentalStock.department == department,
            DepartmentalStock.hospital_id == hospital_id,
        )
        if for_update:
            q = q.with_for_update()
        return q.first()

    def list_departmental_stock(self, hospital_id: UUID, department: str | None = None) -> Sequence[DepartmentalStock]:
        q = (
            self.db.query(DepartmentalStock)
            .options(joinedload(DepartmentalStock.item))
            .filter(DepartmentalStock.hospital_id == hospital_id)
        )
        if department:
            q = q.filter(DepartmentalStock.department == department)
        return q.all()

    # ── Transfers (Feature 48) ────────────────────────────────────────────────

    def get_transfer_by_id(self, transfer_id: UUID, hospital_id: UUID) -> InventoryTransfer | None:
        return (
            self.db.query(InventoryTransfer)
            .options(joinedload(InventoryTransfer.item))
            .filter(InventoryTransfer.id == transfer_id, InventoryTransfer.hospital_id == hospital_id)
            .first()
        )

    def list_transfers(self, hospital_id: UUID, status_filter=None) -> Sequence[InventoryTransfer]:
        q = (
            self.db.query(InventoryTransfer)
            .options(joinedload(InventoryTransfer.item))
            .filter(InventoryTransfer.hospital_id == hospital_id)
        )
        if status_filter:
            q = q.filter(InventoryTransfer.status == status_filter)
        return q.order_by(InventoryTransfer.requested_at.desc()).limit(500).all()

    # ── Ledger ────────────────────────────────────────────────────────────────

    def list_transactions_for_item(self, item_id: UUID, hospital_id: UUID) -> Sequence[InventoryStockTransaction]:
        return (
            self.db.query(InventoryStockTransaction)
            .filter(InventoryStockTransaction.item_id == item_id, InventoryStockTransaction.hospital_id == hospital_id)
            .order_by(InventoryStockTransaction.transaction_time.desc())
            .limit(500)
            .all()
        )

    def list_transactions(
        self, hospital_id: UUID, item_id: UUID | None = None, limit: int = 500
    ) -> Sequence[InventoryStockTransaction]:
        q = self.db.query(InventoryStockTransaction).filter(InventoryStockTransaction.hospital_id == hospital_id)
        if item_id is not None:
            q = q.filter(InventoryStockTransaction.item_id == item_id)
        return q.order_by(InventoryStockTransaction.transaction_time.desc()).limit(limit).all()
