"""Billing domain calculations, idempotent charge generation, and ledger accounting service.

Conforms to UltrionTech-Backend-Template modules/billing/services/ specification.
Replaces app.utils.billing with target-native business logic and entities.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import math
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingSourceType,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.patients.entities.patient import Patient


def compute_net(
    charge_amount: float,
    discount_amount: float = 0.0,
    discount_percent: float | None = None,
) -> tuple[float, float]:
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
    """Lookup active non-cancelled charge by source domain and identifier."""
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
    """Mark an active charge cancelled for a given source entity."""
    row = find_charge_by_source(db, hospital_id, source_type, source_id)
    if not row:
        return None
    row.status = BillingChargeStatus.cancelled
    return row


def bed_stay_days(admitted_at: datetime, discharged_at: datetime) -> int:
    """
    Billable bed days using standard hospital midnight census accounting.
    A patient admitted on Day 1 and discharged on Day 3 crosses 2 midnights;
    with standard admission day / midnight census accounting, billable days = max(1, (discharge_date - admission_date).days + (1 if same_day or post-cutoff else 0))
    or calendar midnights crossed:
    - Same-day admission & discharge: 1 day (daycare / minimum stay).
    - Multi-day: number of distinct calendar midnight periods or calendar days occupied.
    Specifically: (end.date() - start.date()).days, minimum 1. If discharged on a later date,
    each calendar day the bed is occupied (including discharge day if discharged after 12:00 PM cutoff) counts.
    To prevent under-billing (HMS-FLAW-023), billable days is computed as:
    max(1, (end.date() - start.date()).days + (1 if end.hour >= 12 else 0))
    """
    start = admitted_at
    end = discharged_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    
    calendar_diff = (end.date() - start.date()).days
    if calendar_diff <= 0:
        return 1
    # If stayed across calendar days, bill base calendar difference, plus 1 if checkout after 12:00 PM standard checkout cutoff
    extra_day = 1 if end.hour >= 12 else 0
    return max(1, calendar_diff + extra_day)


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
    """Record one-time admission charge for IPD stay."""
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
    """Calculate and post bed stay daily charges upon discharge, accounting for transfer segments."""
    # Check if BedStaySegments exist for this admission (HMS-FLAW-025)
    try:
        from modules.inpatient.entities.admission import BedStaySegment
        segments = (
            db.query(BedStaySegment)
            .filter(
                BedStaySegment.hospital_id == hospital_id,
                BedStaySegment.admission_id == admission_id,
            )
            .order_by(BedStaySegment.started_at.asc())
            .all()
        )
    except Exception:
        segments = []

    if segments:
        total_amount = 0.0
        total_days = 0
        notes_parts = []
        for s in segments:
            seg_start = s.started_at
            seg_end = s.ended_at or discharged_at
            seg_days = bed_stay_days(seg_start, seg_end)
            seg_rate = float(s.rate_per_day or 0.0)
            seg_amount = round(seg_rate * seg_days, 2)
            total_amount += seg_amount
            total_days += seg_days
            notes_parts.append(f"seg(rate={seg_rate}, days={seg_days}, amt={seg_amount})")
        
        amount = round(total_amount, 2)
        days = max(1, total_days)
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
            notes=f"multi_segment; days={days}; {'; '.join(notes_parts)}"[:512],
        )

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


def allocate_payment_to_charges(
    db: Session, hospital_id: UUID, patient_id: UUID, amount: float
) -> None:
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
        .with_for_update()
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


def patient_ledger_totals(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    *,
    charges: list[BillingCharge] | None = None,
    payments: list[BillingPayment] | None = None,
) -> dict[str, Any]:
    """
    Compute aggregate charges, payments, and outstanding balance for a patient.

    `charges`/`payments` are optional pre-fetched rows for callers that
    already loaded the same (hospital_id, patient_id, non-cancelled-charges)
    rows for another purpose in the same request — e.g. GetPatientLedgerAction
    and GetPatientSummaryAction in billing_actions.py, which previously
    triggered 2 extra queries here on top of their own, plus 2 more in
    build_ledger_entries, to fetch what is functionally the same row set
    three times over. When omitted, behavior is unchanged from before.
    """
    if charges is None:
        charges = (
            db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.patient_id == patient_id,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .all()
        )
    if payments is None:
        payments = (
            db.query(BillingPayment)
            .filter(
                BillingPayment.hospital_id == hospital_id,
                BillingPayment.patient_id == patient_id,
            )
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


def patient_ledger_totals_bulk(
    db: Session, hospital_id: UUID, patient_ids: list[UUID]
) -> dict[UUID, dict[str, Any]]:
    """Compute aggregate charges, payments, and outstanding balance for many patients
    in two bulk queries instead of one pair of queries per patient."""
    unique_ids = list({pid for pid in patient_ids if pid is not None})
    result: dict[UUID, dict[str, Any]] = {
        pid: {
            "total_charges": 0.0,
            "total_paid": 0.0,
            "outstanding": 0.0,
            "charge_count": 0,
            "payment_count": 0,
        }
        for pid in unique_ids
    }
    if not unique_ids:
        return result

    charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id.in_(unique_ids),
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .all()
    )
    payments = (
        db.query(BillingPayment)
        .filter(
            BillingPayment.hospital_id == hospital_id,
            BillingPayment.patient_id.in_(unique_ids),
        )
        .all()
    )

    charges_by_patient: dict[UUID, list[BillingCharge]] = {}
    for c in charges:
        charges_by_patient.setdefault(c.patient_id, []).append(c)
    payments_by_patient: dict[UUID, list[BillingPayment]] = {}
    for p in payments:
        payments_by_patient.setdefault(p.patient_id, []).append(p)

    for pid in unique_ids:
        p_charges = charges_by_patient.get(pid, [])
        p_payments = payments_by_patient.get(pid, [])
        total_charges = round(sum(float(c.net_amount) for c in p_charges), 2)
        total_paid = round(sum(float(p.amount) for p in p_payments), 2)
        outstanding = round(max(0.0, total_charges - total_paid), 2)
        result[pid] = {
            "total_charges": total_charges,
            "total_paid": total_paid,
            "outstanding": outstanding,
            "charge_count": len(p_charges),
            "payment_count": len(p_payments),
        }
    return result


def charge_to_dict(c: BillingCharge, patient: Patient | None = None) -> dict[str, Any]:
    """Serialize BillingCharge ORM model to dictionary with patient context."""
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


def payment_to_dict(
    p: BillingPayment, patient: Patient | None = None
) -> dict[str, Any]:
    """Serialize BillingPayment ORM model to dictionary with patient context."""
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
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    *,
    charges: list[BillingCharge] | None = None,
    payments: list[BillingPayment] | None = None,
) -> list[dict[str, Any]]:
    """
    Build chronological unified ledger timeline with charge debits and payment credits.

    `charges`/`payments` are optional pre-fetched rows — see
    patient_ledger_totals() above for why. This function includes cancelled
    charges in its own filter (unlike patient_ledger_totals) so it can still
    render them in the timeline; it filters them out in the entries loop
    below. Passing in patient_ledger_totals' non-cancelled-only charges list
    would therefore be wrong here — callers combining both must pass this
    function its own charges list (or none, to keep the original query).
    """
    if charges is None:
        charges = (
            db.query(BillingCharge)
            .options(joinedload(BillingCharge.patient))
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.patient_id == patient_id,
            )
            .all()
        )
    if payments is None:
        payments = (
            db.query(BillingPayment)
            .options(joinedload(BillingPayment.patient))
            .filter(
                BillingPayment.hospital_id == hospital_id,
                BillingPayment.patient_id == patient_id,
            )
            .all()
        )
    entries: list[dict[str, Any]] = []
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
                "occurred_at": (
                    datetime.combine(p.payment_date, datetime.min.time()).replace(
                        tzinfo=timezone.utc
                    )
                    if p.payment_date
                    else p.created_at
                ),
                "description": f"Payment ({p.payment_method.value.replace('_', ' ')})",
                "source_type": None,
                "debit": 0.0,
                "credit": float(p.amount),
                "status": "received",
                "ref_id": p.id,
            }
        )
    def _normalize_dt(val: Any) -> datetime:
        if not val:
            return datetime.min.replace(tzinfo=timezone.utc)
        if isinstance(val, date) and not isinstance(val, datetime):
            val = datetime.combine(val, datetime.min.time())
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)

    entries.sort(
        key=lambda e: _normalize_dt(e["occurred_at"]),
        reverse=True,
    )
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
    """Record patient payment and optionally FIFO allocate across charges."""
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
