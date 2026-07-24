"""Pharmacy stock service — sole path for inventory mutations."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    Medicine,
    MedicineBatch,
    MedicineInventory,
    StockTransaction,
    StockTransactionType,
)


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
    # silence unused
    _ = today
    return inv


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
) -> StockTransaction:
    """
    Apply signed quantity change to a batch and write StockTransaction.
    quantity_delta > 0 increases stock; < 0 decreases.
    """
    batch = (
        db.query(MedicineBatch)
        .filter(
            MedicineBatch.id == batch_id,
            MedicineBatch.hospital_id == hospital_id,
            MedicineBatch.medicine_id == medicine_id,
        )
        .with_for_update()
        .first()
    )
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine batch not found")

    if quantity_delta < 0 and not allow_expired and batch.expiry_date < date.today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot sell/issue expired batch {batch.batch_number} (expired {batch.expiry_date})",
        )

    before = float(batch.available_quantity or 0)
    after = round(before + float(quantity_delta), 2)
    if after < -0.0001:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient stock on batch {batch.batch_number} (available {before})",
        )
    after = max(0.0, after)
    batch.available_quantity = after

    txn = StockTransaction(
        hospital_id=hospital_id,
        medicine_id=medicine_id,
        batch_id=batch_id,
        transaction_type=transaction_type,
        quantity=round(float(quantity_delta), 2),
        quantity_before=before,
        quantity_after=after,
        reference_type=reference_type,
        reference_id=reference_id,
        notes=notes,
        created_by_name=created_by_name or "System",
    )
    db.add(txn)
    db.flush()
    refresh_medicine_inventory(db, hospital_id, medicine_id)
    return txn


def allocate_fifo(
    db: Session,
    *,
    hospital_id: UUID,
    medicine_id: UUID,
    quantity: float,
) -> list[tuple[MedicineBatch, float]]:
    """Return list of (batch, qty) using earliest expiry first. Raises if insufficient non-expired stock."""
    if quantity <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Quantity must be positive")

    today = date.today()
    batches = (
        db.query(MedicineBatch)
        .filter(
            MedicineBatch.hospital_id == hospital_id,
            MedicineBatch.medicine_id == medicine_id,
            MedicineBatch.is_active.is_(True),
            MedicineBatch.available_quantity > 0,
            MedicineBatch.expiry_date >= today,
        )
        .order_by(MedicineBatch.expiry_date.asc(), MedicineBatch.created_at.asc())
        .with_for_update()
        .all()
    )
    remaining = float(quantity)
    allocations: list[tuple[MedicineBatch, float]] = []
    for batch in batches:
        if remaining <= 0:
            break
        take = min(float(batch.available_quantity), remaining)
        if take > 0:
            allocations.append((batch, take))
            remaining = round(remaining - take, 2)
    if remaining > 0.0001:
        med = db.query(Medicine).filter(Medicine.id == medicine_id).first()
        name = med.medicine_name if med else str(medicine_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient non-expired stock for {name} (short by {remaining})",
        )
    return allocations


def inventory_status(inv: MedicineInventory | None, medicine: Medicine, today: date | None = None) -> str:
    today = today or date.today()
    current = float(inv.current_stock if inv else 0)
    if current <= 0:
        return "out_of_stock"
    if inv and inv.nearest_expiry and inv.nearest_expiry < today:
        return "expired"
    if inv and inv.nearest_expiry and (inv.nearest_expiry - today).days <= 30:
        # still may also be low
        if current <= float(medicine.minimum_stock or 0):
            return "low_stock"
        return "expiring_soon"
    if current <= float(medicine.minimum_stock or 0):
        return "low_stock"
    return "ok"


def next_doc_number(db: Session, hospital_id: UUID, model, field_name: str, prefix: str) -> str:
    y = date.today().year
    full_prefix = f"{prefix}-{y}-"
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
    return f"{full_prefix}{max_seq + 1:05d}"


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
