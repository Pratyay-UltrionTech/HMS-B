"""
Central + departmental non-drug inventory API endpoints (Features 44-48).

Prefix: /inventory
Tags: ["inventory"]
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.inventory.actions.inventory_actions import InventoryActions
from hms_migration.modules.inventory.contracts.inventory_contracts import (
    CentralInventoryStockResponse,
    ConsumableBatchResponse,
    ConsumableItemCreate,
    ConsumableItemResponse,
    ConsumableItemUpdate,
    ConsumeDepartmentalStockRequest,
    DepartmentalStockResponse,
    InventoryTransferApprove,
    InventoryTransferCreate,
    InventoryStockTransactionResponse,
    InventoryTransferReject,
    InventoryTransferResponse,
    ReceiveStockRequest,
)
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/inventory", tags=["inventory"])


# ── Feature 45: Non-Drug Consumable Master ──────────────────────────────────

@router.get("/items", response_model=list[ConsumableItemResponse])
def list_items(
    active_only: bool | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[ConsumableItemResponse]:
    return InventoryActions(db, hospital_id, user).list_items(active_only=active_only)


@router.post("/items", response_model=ConsumableItemResponse, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: ConsumableItemCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> ConsumableItemResponse:
    return InventoryActions(db, hospital_id, user).create_item(payload)


@router.get("/items/{item_id}", response_model=ConsumableItemResponse)
def get_item(
    item_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> ConsumableItemResponse:
    return InventoryActions(db, hospital_id, user).get_item(item_id)


@router.put("/items/{item_id}", response_model=ConsumableItemResponse)
def update_item(
    item_id: UUID,
    payload: ConsumableItemUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> ConsumableItemResponse:
    return InventoryActions(db, hospital_id, user).update_item(item_id, payload)


# ── Feature 46: Batch/Lot Tracking (read) ───────────────────────────────────

@router.get("/items/{item_id}/batches", response_model=list[ConsumableBatchResponse])
def list_batches(
    item_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[ConsumableBatchResponse]:
    return InventoryActions(db, hospital_id, user).list_batches(item_id)


# ── Ledger (read) ────────────────────────────────────────────────────────────

@router.get("/transactions", response_model=list[InventoryStockTransactionResponse])
def list_transactions(
    item_id: UUID | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[InventoryStockTransactionResponse]:
    return InventoryActions(db, hospital_id, user).list_transactions(item_id=item_id)


# ── Feature 44: Central Inventory ───────────────────────────────────────────

@router.get("/central-stock", response_model=list[CentralInventoryStockResponse])
def list_central_stock(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[CentralInventoryStockResponse]:
    return InventoryActions(db, hospital_id, user).list_central_stock()


@router.post("/central-stock/receive", response_model=CentralInventoryStockResponse, status_code=status.HTTP_201_CREATED)
def receive_stock(
    payload: ReceiveStockRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> CentralInventoryStockResponse:
    return InventoryActions(db, hospital_id, user).receive_stock(payload)


# ── Feature 47: Departmental Stock ──────────────────────────────────────────

@router.get("/departmental-stock", response_model=list[DepartmentalStockResponse])
def list_departmental_stock(
    department: str | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[DepartmentalStockResponse]:
    return InventoryActions(db, hospital_id, user).list_departmental_stock(department=department)


@router.post("/departmental-stock/consume", response_model=DepartmentalStockResponse)
def consume_departmental_stock(
    payload: ConsumeDepartmentalStockRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> DepartmentalStockResponse:
    return InventoryActions(db, hospital_id, user).consume_departmental_stock(payload)


# ── Feature 48: Inter-Department Transfer ───────────────────────────────────

@router.get("/transfers", response_model=list[InventoryTransferResponse])
def list_transfers(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[InventoryTransferResponse]:
    return InventoryActions(db, hospital_id, user).list_transfers(status_filter=status_filter)


@router.get("/transfers/{transfer_id}", response_model=InventoryTransferResponse)
def get_transfer(
    transfer_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InventoryTransferResponse:
    return InventoryActions(db, hospital_id, user).get_transfer(transfer_id)


@router.post("/transfers", response_model=InventoryTransferResponse, status_code=status.HTTP_201_CREATED)
def request_transfer(
    payload: InventoryTransferCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InventoryTransferResponse:
    return InventoryActions(db, hospital_id, user).request_transfer(payload)


@router.post("/transfers/{transfer_id}/approve", response_model=InventoryTransferResponse)
def approve_transfer(
    transfer_id: UUID,
    payload: InventoryTransferApprove,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InventoryTransferResponse:
    return InventoryActions(db, hospital_id, user).approve_transfer(transfer_id, payload)


@router.post("/transfers/{transfer_id}/dispatch", response_model=InventoryTransferResponse)
def dispatch_transfer(
    transfer_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InventoryTransferResponse:
    return InventoryActions(db, hospital_id, user).dispatch_transfer(transfer_id)


@router.post("/transfers/{transfer_id}/complete", response_model=InventoryTransferResponse)
def complete_transfer(
    transfer_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InventoryTransferResponse:
    return InventoryActions(db, hospital_id, user).complete_transfer(transfer_id)


@router.post("/transfers/{transfer_id}/reject", response_model=InventoryTransferResponse)
def reject_transfer(
    transfer_id: UUID,
    payload: InventoryTransferReject,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InventoryTransferResponse:
    return InventoryActions(db, hospital_id, user).reject_transfer(transfer_id, payload)
