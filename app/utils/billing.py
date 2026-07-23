"""Billing foundation helpers: idempotent charges, ledger totals, payment allocation."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from app.models import (
    BillingCharge,
    BillingChargeStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingSourceType,
    Patient,
)


def compute_net(charge_amount: float, discount_amount: float = 0.0, discount_percent: float | None = None) -> tuple[float, float]:
    """Return (discount_amount, net_amount). Percent applied to charge_amount if provided."""
    charge = max(0.0, float(charge_amount or 0))
    disc = max(0.0, float(discount_amount or 0))
    if discount_percent is not None and float(discount_percent) > 0:
        disc = max(disc, round(charge * float(discount_percent) / 100.0, 2))
    disc = min(disc, charge)
    return disc, round(charge - disc, 2)


def find_charge_by_source(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
) -> BillingCharge | None:
    return (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.source_type == source_type,
            BillingCharge.source_id == source_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .first()
    )


def ensure_charge(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID | None,
    description: str,
    charge_amount: float,
    discount_amount: float = 0.0,
    discount_percent: float | None = None,
    notes: str | None = None,
    created_by_name: str = "",
) -> BillingCharge:
    """Create charge if not already present for this source. Does not commit."""
    if source_id is not None:
        existing = find_charge_by_source(db, hospital_id, source_type, source_id)
        if existing:
            return existing

    disc, net = compute_net(charge_amount, discount_amount, discount_percent)
    status = BillingChargeStatus.paid if net <= 0 else BillingChargeStatus.pending
    row = BillingCharge(
        hospital_id=hospital_id,
        patient_id=patient_id,
        source_type=source_type,
        source_id=source_id,
        description=description.strip()[:512],
        charge_amount=round(float(charge_amount or 0), 2),
        discount_amount=disc,
        discount_percent=discount_percent,
        net_amount=net,
        amount_paid=net if status == BillingChargeStatus.paid else 0.0,
        status=status,
        notes=notes,
        created_by_name=created_by_name or "System",
    )
    db.add(row)
    db.flush()
    return row


def cancel_charge_for_source(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
) -> BillingCharge | None:
    row = find_charge_by_source(db, hospital_id, source_type, source_id)
    if not row:
        return None
    row.status = BillingChargeStatus.cancelled
    return row


def bed_stay_days(admitted_at: datetime, discharged_at: datetime) -> int:
    """Billable bed days: ceil(hours/24), minimum 1 day."""
    start = admitted_at
    end = discharged_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    hours = max(0.0, (end - start).total_seconds() / 3600.0)
    if hours <= 0:
        return 1
    return max(1, int(math.ceil(hours / 24.0)))


def ensure_admission_charge(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    admission_id: UUID,
    ward_name: str | None,
    admission_fee: float,
    created_by_name: str = "",
) -> BillingCharge:
    return ensure_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient_id,
        source_type=BillingSourceType.admission,
        source_id=admission_id,
        description=f"Admission Charge — {ward_name or 'Ward'}"[:512],
        charge_amount=float(admission_fee or 0),
        created_by_name=created_by_name or "System",
    )


def ensure_bed_charge_for_admission(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    admission_id: UUID,
    admitted_at: datetime,
    discharged_at: datetime,
    ward_name: str | None,
    room_code: str | None,
    bed_code: str | None,
    bed_charge_per_day: float,
    created_by_name: str = "",
) -> BillingCharge:
    days = bed_stay_days(admitted_at, discharged_at)
    amount = round(float(bed_charge_per_day or 0) * days, 2)
    day_label = "Day" if days == 1 else "Days"
    place = " / ".join(
        p for p in [ward_name or None, room_code or None, bed_code or None] if p
    ) or "Bed"
    return ensure_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient_id,
        source_type=BillingSourceType.bed,
        source_id=admission_id,
        description=f"Bed Charges ({days} {day_label}) — {place}"[:512],
        charge_amount=amount,
        created_by_name=created_by_name or "System",
        notes=f"days={days}; rate_per_day={float(bed_charge_per_day or 0)}",
    )


def _refresh_charge_status(charge: BillingCharge) -> None:
    if charge.status == BillingChargeStatus.cancelled:
        return
    paid = float(charge.amount_paid or 0)
    net = float(charge.net_amount or 0)
    if net <= 0 or paid >= net:
        charge.status = BillingChargeStatus.paid
        charge.amount_paid = net
    elif paid > 0:
        charge.status = BillingChargeStatus.partially_paid
    else:
        charge.status = BillingChargeStatus.pending


def allocate_payment_to_charges(db: Session, hospital_id: UUID, patient_id: UUID, amount: float) -> None:
    """FIFO allocate payment across pending / partially paid charges."""
    remaining = round(float(amount), 2)
    if remaining <= 0:
        return
    charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id == patient_id,
            BillingCharge.status.in_(
                [BillingChargeStatus.pending, BillingChargeStatus.partially_paid]
            ),
        )
        .order_by(BillingCharge.created_at.asc())
        .all()
    )
    for charge in charges:
        if remaining <= 0:
            break
        due = round(float(charge.net_amount) - float(charge.amount_paid or 0), 2)
        if due <= 0:
            _refresh_charge_status(charge)
            continue
        apply = min(due, remaining)
        charge.amount_paid = round(float(charge.amount_paid or 0) + apply, 2)
        remaining = round(remaining - apply, 2)
        _refresh_charge_status(charge)


def patient_ledger_totals(db: Session, hospital_id: UUID, patient_id: UUID) -> dict:
    charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id == patient_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .all()
    )
    payments = (
        db.query(BillingPayment)
        .filter(BillingPayment.hospital_id == hospital_id, BillingPayment.patient_id == patient_id)
        .all()
    )
    total_charges = round(sum(float(c.net_amount) for c in charges), 2)
    total_paid = round(sum(float(p.amount) for p in payments), 2)
    outstanding = round(max(0.0, total_charges - total_paid), 2)
    return {
        "total_charges": total_charges,
        "total_paid": total_paid,
        "outstanding": outstanding,
        "charge_count": len(charges),
        "payment_count": len(payments),
    }


def charge_to_dict(c: BillingCharge, patient: Patient | None = None) -> dict:
    p = patient or c.patient
    return {
        "id": c.id,
        "hospital_id": c.hospital_id,
        "patient_id": c.patient_id,
        "source_type": c.source_type,
        "source_id": c.source_id,
        "description": c.description,
        "charge_amount": c.charge_amount,
        "discount_amount": c.discount_amount,
        "discount_percent": c.discount_percent,
        "net_amount": c.net_amount,
        "amount_paid": c.amount_paid,
        "status": c.status,
        "notes": c.notes,
        "created_by_name": c.created_by_name,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "patient_name": p.name if p else None,
        "patient_uhid": p.uhid if p else None,
    }


def payment_to_dict(p: BillingPayment, patient: Patient | None = None) -> dict:
    pat = patient or p.patient
    return {
        "id": p.id,
        "hospital_id": p.hospital_id,
        "patient_id": p.patient_id,
        "amount": p.amount,
        "payment_date": p.payment_date,
        "payment_method": p.payment_method,
        "notes": p.notes,
        "received_by_name": p.received_by_name,
        "created_at": p.created_at,
        "patient_name": pat.name if pat else None,
        "patient_uhid": pat.uhid if pat else None,
    }


def build_ledger_entries(
    db: Session, hospital_id: UUID, patient_id: UUID
) -> list[dict]:
    charges = (
        db.query(BillingCharge)
        .options(joinedload(BillingCharge.patient))
        .filter(BillingCharge.hospital_id == hospital_id, BillingCharge.patient_id == patient_id)
        .all()
    )
    payments = (
        db.query(BillingPayment)
        .options(joinedload(BillingPayment.patient))
        .filter(BillingPayment.hospital_id == hospital_id, BillingPayment.patient_id == patient_id)
        .all()
    )
    entries: list[dict] = []
    for c in charges:
        if c.status == BillingChargeStatus.cancelled:
            continue
        entries.append(
            {
                "id": str(c.id),
                "entry_type": "charge",
                "occurred_at": c.created_at,
                "description": c.description,
                "source_type": c.source_type.value if c.source_type else None,
                "debit": float(c.net_amount),
                "credit": 0.0,
                "status": c.status.value,
                "ref_id": c.id,
            }
        )
    for p in payments:
        entries.append(
            {
                "id": str(p.id),
                "entry_type": "payment",
                "occurred_at": datetime.combine(p.payment_date, datetime.min.time()).replace(
                    tzinfo=timezone.utc
                )
                if p.payment_date
                else p.created_at,
                "description": f"Payment ({p.payment_method.value.replace('_', ' ')})",
                "source_type": None,
                "debit": 0.0,
                "credit": float(p.amount),
                "status": "received",
                "ref_id": p.id,
            }
        )
    entries.sort(key=lambda e: e["occurred_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return entries


def create_payment(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    amount: float,
    payment_date: date,
    payment_method: BillingPaymentMethod,
    notes: str | None,
    received_by_name: str,
    allocate: bool = True,
) -> BillingPayment:
    pay = BillingPayment(
        hospital_id=hospital_id,
        patient_id=patient_id,
        amount=round(float(amount), 2),
        payment_date=payment_date,
        payment_method=payment_method,
        notes=notes,
        received_by_name=received_by_name or "Staff",
    )
    db.add(pay)
    db.flush()
    if allocate:
        allocate_payment_to_charges(db, hospital_id, patient_id, pay.amount)
    return pay
