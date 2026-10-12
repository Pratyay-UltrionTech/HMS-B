"""
Pharmacy stock management service and helper functions.
Sole path for inventory mutations, FIFO allocations, and document numbering.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
import uuid
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from modules.pharmacy.entities.pharmacy_entities import (
    Medicine,
    MedicineBatch,
    MedicineInventory,
    StockTransaction,
    StockTransactionType,
)


def actor_name(user: dict[str, Any]) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def line_amounts(qty: float, unit_price: float, gst_percent: float, discount: float = 0.0) -> tuple[float, float, float]:
    taxable = max(0.0, round(qty * unit_price - discount, 2))
    gst = round(taxable * float(gst_percent or 0) / 100.0, 2)
    net = round(taxable + gst, 2)
    return taxable, gst, net


def refresh_medicine_inventory(db: Session, hospital_id: UUID, medicine_id: UUID) -> MedicineInventory:
    """Recompute summary from batches. Caller must flush batches first."""
    today = date.today()
    rows = (
        db.query(MedicineBatch)
        .filter(
            MedicineBatch.hospital_id == hospital_id,
            MedicineBatch.medicine_id == medicine_id,
            MedicineBatch.is_active.is_(True),
        )
        .all()
    )
    current = sum(max(0.0, float(b.available_quantity or 0)) for b in rows)
    nearest = None
    active_batches = [b for b in rows if float(b.available_quantity or 0) > 0]
    if active_batches:
        nearest = min((b.expiry_date for b in active_batches), default=None)
    inv = (
        db.query(MedicineInventory)
        .filter(
            MedicineInventory.hospital_id == hospital_id,
            MedicineInventory.medicine_id == medicine_id,
        )
        .first()
    )
    if not inv:
        inv = MedicineInventory(
            hospital_id=hospital_id,
            medicine_id=medicine_id,
            current_stock=0,
            reserved_stock=0,
            available_stock=0,
            batch_count=0,
        )
        db.add(inv)
    inv.current_stock = round(current, 2)
    inv.reserved_stock = round(float(inv.reserved_stock or 0), 2)
    inv.available_stock = round(max(0.0, inv.current_stock - inv.reserved_stock), 2)
    inv.nearest_expiry = nearest
    inv.batch_count = len(active_batches)
    inv.updated_at = datetime.now(timezone.utc)
    _ = today
    return inv


def refresh_medicine_inventories_bulk(
    db: Session, hospital_id: UUID, medicine_ids: list[UUID]
) -> None:
    """Bulk recomputation of inventory summaries from active batches across multiple medicines."""
    if not medicine_ids:
        return
    unique_ids = list(dict.fromkeys(medicine_ids))
    all_batches = (
        db.query(MedicineBatch)
        .filter(
            MedicineBatch.hospital_id == hospital_id,
            MedicineBatch.medicine_id.in_(unique_ids),
            MedicineBatch.is_active.is_(True),
        )
        .all()
    )
    batches_by_med: dict[UUID, list[MedicineBatch]] = {}
    for b in all_batches:
        batches_by_med.setdefault(b.medicine_id, []).append(b)

    existing_invs = (
        db.query(MedicineInventory)
        .filter(
            MedicineInventory.hospital_id == hospital_id,
            MedicineInventory.medicine_id.in_(unique_ids),
        )
        .all()
    )
    inv_by_med = {inv.medicine_id: inv for inv in existing_invs}

    for mid in unique_ids:
        rows = batches_by_med.get(mid, [])
        current = sum(max(0.0, float(b.available_quantity or 0)) for b in rows)
        active_batches = [b for b in rows if float(b.available_quantity or 0) > 0]
        nearest = min((b.expiry_date for b in active_batches), default=None) if active_batches else None

        inv = inv_by_med.get(mid)
        if not inv:
            inv = MedicineInventory(
                hospital_id=hospital_id,
                medicine_id=mid,
                current_stock=0,
                reserved_stock=0,
                available_stock=0,
                batch_count=0,
            )
            db.add(inv)
            inv_by_med[mid] = inv

        inv.current_stock = round(current, 2)
        inv.reserved_stock = round(float(inv.reserved_stock or 0), 2)
        inv.available_stock = round(max(0.0, inv.current_stock - inv.reserved_stock), 2)
        inv.nearest_expiry = nearest
        inv.batch_count = len(active_batches)
        inv.updated_at = datetime.now(timezone.utc)


def apply_stock_changes_bulk(
    db: Session,
    *,
    hospital_id: UUID,
    changes: list[dict[str, Any]],
    preloaded_batches: dict[UUID, MedicineBatch] | None = None,
    auto_refresh: bool = True,
) -> list[StockTransaction]:
    """
    Apply multiple signed quantity changes to batches in bulk with row locking,
    writing StockTransactions in a single pass without per-item flushes.
    """
    if not changes:
        return []

    batch_map: dict[UUID, MedicineBatch] = dict(preloaded_batches) if preloaded_batches else {}
    missing_batch_ids = [
        c["batch_id"] for c in changes
        if c.get("batch_id") and c["batch_id"] not in batch_map
    ]

    if missing_batch_ids:
        batches = (
            db.query(MedicineBatch)
            .filter(
                MedicineBatch.id.in_(list(dict.fromkeys(missing_batch_ids))),
                MedicineBatch.hospital_id == hospital_id,
            )
            .with_for_update()
            .all()
        )
        for b in batches:
            batch_map[b.id] = b

    txns: list[StockTransaction] = []
    med_ids: list[UUID] = []
    today = date.today()

    for item in changes:
        batch_id = item["batch_id"]
        med_id = item["medicine_id"]
        quantity_delta = float(item["quantity_delta"])
        allow_expired = item.get("allow_expired", False)

        batch = batch_map.get(batch_id)
        if not batch or batch.medicine_id != med_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine batch not found")

        if quantity_delta < 0 and not allow_expired and batch.expiry_date < today:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot sell/issue expired batch {batch.batch_number} (expired {batch.expiry_date})",
            )

        before = float(batch.available_quantity or 0)
        after = round(before + quantity_delta, 2)
        if after < -0.0001:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient stock on batch {batch.batch_number} (available {before})",
            )
        after = max(0.0, after)
        batch.available_quantity = after

        txn = StockTransaction(
            id=uuid.uuid4(),
            hospital_id=hospital_id,
            medicine_id=med_id,
            batch_id=batch_id,
            transaction_type=item["transaction_type"],
            quantity=round(quantity_delta, 2),
            quantity_before=before,
            quantity_after=after,
            reference_type=item.get("reference_type"),
            reference_id=item.get("reference_id"),
            notes=item.get("notes"),
            created_by_name=item.get("created_by_name") or "System",
        )
        txns.append(txn)
        med_ids.append(med_id)

    db.add_all(txns)

    if auto_refresh and med_ids:
        refresh_medicine_inventories_bulk(db, hospital_id, list(dict.fromkeys(med_ids)))

    return txns


def apply_stock_change(
    db: Session,
    *,
    hospital_id: UUID,
    medicine_id: UUID,
    batch_id: UUID,
    quantity_delta: float,
    transaction_type: StockTransactionType,
    reference_type: str | None = None,
    reference_id: UUID | None = None,
    notes: str | None = None,
    created_by_name: str = "",
    allow_expired: bool = False,
    auto_refresh: bool = True,
) -> StockTransaction:
    """
    Apply signed quantity change to a single batch and write StockTransaction.
    Preserves immediate flush and return semantics for single-item callers.
    """
    txns = apply_stock_changes_bulk(
        db,
        hospital_id=hospital_id,
        changes=[
            {
                "medicine_id": medicine_id,
                "batch_id": batch_id,
                "quantity_delta": quantity_delta,
                "transaction_type": transaction_type,
                "reference_type": reference_type,
                "reference_id": reference_id,
                "notes": notes,
                "created_by_name": created_by_name,
                "allow_expired": allow_expired,
            }
        ],
        auto_refresh=auto_refresh,
    )
    db.flush()
    return txns[0]


def allocate_fifo_bulk(
    db: Session,
    *,
    hospital_id: UUID,
    requests: list[tuple[UUID, float]],
    medicine_names: dict[UUID, str] | None = None,
) -> list[list[tuple[MedicineBatch, float]]]:
    """
    Allocate batches across multiple medicines or requests using FIFO (earliest expiry first).
    Loads and locks matching active batches in a single bulk query.
    Returns a list of allocations (list of (MedicineBatch, allocated_qty)) corresponding 1:1 with requests.
    """
    if not requests:
        return []
    today = date.today()
    med_ids = list(dict.fromkeys(m for m, _ in requests))

    batches = (
        db.query(MedicineBatch)
        .filter(
            MedicineBatch.hospital_id == hospital_id,
            MedicineBatch.medicine_id.in_(med_ids),
            MedicineBatch.is_active.is_(True),
            MedicineBatch.available_quantity > 0,
            MedicineBatch.expiry_date >= today,
        )
        .order_by(MedicineBatch.expiry_date.asc(), MedicineBatch.created_at.asc())
        .with_for_update()
        .all()
    )

    batches_by_med: dict[UUID, list[MedicineBatch]] = {}
    for b in batches:
        batches_by_med.setdefault(b.medicine_id, []).append(b)

    batch_available: dict[UUID, float] = {b.id: float(b.available_quantity) for b in batches}
    per_request_allocations: list[list[tuple[MedicineBatch, float]]] = []

    for med_id, requested_qty in requests:
        if requested_qty <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Quantity must be positive")

        med_batches = batches_by_med.get(med_id, [])
        remaining = float(requested_qty)
        med_allocs: list[tuple[MedicineBatch, float]] = []

        for b in med_batches:
            if remaining <= 0:
                break
            avail = batch_available.get(b.id, 0.0)
            if avail <= 0:
                continue
            take = min(avail, remaining)
            if take > 0:
                med_allocs.append((b, take))
                batch_available[b.id] = round(avail - take, 2)
                remaining = round(remaining - take, 2)

        if remaining > 0.0001:
            name = (medicine_names or {}).get(med_id)
            if not name:
                med = db.query(Medicine.medicine_name).filter(Medicine.id == med_id).first()
                name = med[0] if med else str(med_id)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient non-expired stock for {name} (short by {remaining})",
            )
        per_request_allocations.append(med_allocs)

    return per_request_allocations


def allocate_fifo(
    db: Session,
    *,
    hospital_id: UUID,
    medicine_id: UUID,
    quantity: float,
) -> list[tuple[MedicineBatch, float]]:
    """Return list of (batch, qty) using earliest expiry first. Raises if insufficient non-expired stock."""
    result = allocate_fifo_bulk(db, hospital_id=hospital_id, requests=[(medicine_id, quantity)])
    return result[0] if result else []


def inventory_status(inv: MedicineInventory | None, medicine: Medicine, today: date | None = None) -> str:
    today = today or date.today()
    current = float(inv.current_stock if inv else 0)
    if current <= 0:
        return "out_of_stock"
    if inv and inv.nearest_expiry and inv.nearest_expiry < today:
        return "expired"
    if inv and inv.nearest_expiry and (inv.nearest_expiry - today).days <= 30:
        if current <= float(medicine.minimum_stock or 0):
            return "low_stock"
        return "expiring_soon"
    if current <= float(medicine.minimum_stock or 0):
        return "low_stock"
    return "ok"


def next_doc_number(db: Session, hospital_id: UUID, model: Any, field_name: str, prefix: str) -> str:
    y = date.today().year
    full_prefix = f"{prefix}-{y}-"
    counter_name = f"{prefix.lower()}_{y}"
    from shared.database.sequences import (
        counter_exists as _exists,
        ensure_counter_at_least as _ensure,
        next_sequence_value as _next_seq,
    )
    if not _exists(db, hospital_id, counter_name):
        col = getattr(model, field_name)
        rows = (
            db.query(col)
            .filter(model.hospital_id == hospital_id, col.like(f"{full_prefix}%"))
            .all()
        )
        max_seq = 0
        for (num,) in rows:
            try:
                max_seq = max(max_seq, int(str(num).split("-")[-1]))
            except (ValueError, IndexError):
                continue
        _ensure(db, hospital_id, counter_name, max_seq)
    seq = _next_seq(db, hospital_id, counter_name)
    return f"{full_prefix}{seq:05d}"


def medicine_stock_total(db: Session, hospital_id: UUID, medicine_id: UUID) -> float:
    total = (
        db.query(func.coalesce(func.sum(MedicineBatch.available_quantity), 0.0))
        .filter(
            MedicineBatch.hospital_id == hospital_id,
            MedicineBatch.medicine_id == medicine_id,
            MedicineBatch.is_active.is_(True),
        )
        .scalar()
    )
    return float(total or 0)
