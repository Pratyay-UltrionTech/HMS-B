"""Billing domain calculations, financial accounts, deposits, refunds, selective payment allocations,
and running bank-statement ledger accounting service.

Conforms to UltrionTech-Backend-Template modules/billing/services/ specification.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import math
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingPayment,
    BillingPaymentAllocation,
    BillingPaymentMethod,
    BillingRefund,
    BillingSourceType,
    DepositStatus,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    RefundStatus,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.patients.entities.patient import Patient


# ── Mathematical Helpers ─────────────────────────────────────────────────────

def compute_net(
    charge_amount: float,
    discount_amount: float = 0.0,
    discount_percent: float | None = None,
    tax_amount: float = 0.0,
) -> tuple[float, float]:
    """Return (discount_amount, net_amount). Percent applied to charge_amount if provided. Tax added."""
    charge = max(0.0, float(charge_amount or 0))
    disc = max(0.0, float(discount_amount or 0))
    if discount_percent is not None and float(discount_percent) > 0:
        disc = max(disc, round(charge * float(discount_percent) / 100.0, 2))
    disc = min(disc, charge)
    tax = max(0.0, float(tax_amount or 0))
    net = round((charge - disc) + tax, 2)
    return disc, net


# ── Financial Account (Episode) Operations ───────────────────────────────────

def next_financial_account_number(db: Session, hospital_id: UUID) -> str:
    """Generate sequential account number: ACC-YYYY-NNNNN (atomic)."""
    year = date.today().year
    counter_name = f"account_{year}"
    from shared.database.sequences import (
        counter_exists as _exists,
        ensure_counter_at_least as _ensure_ctr,
        next_sequence_value as _next_seq,
    )
    if not _exists(db, hospital_id, counter_name):
        prefix = f"ACC-{year}-"
        rows = (
            db.query(FinancialAccount.account_number)
            .filter(
                FinancialAccount.hospital_id == hospital_id,
                FinancialAccount.account_number.like(f"{prefix}%"),
            )
            .all()
        )
        max_seq = 0
        for (num,) in rows:
            try:
                max_seq = max(max_seq, int(str(num).split("-")[-1]))
            except (ValueError, IndexError):
                continue
        _ensure_ctr(db, hospital_id, counter_name, max_seq)
    seq = _next_seq(db, hospital_id, counter_name)
    return f"ACC-{year}-{seq:05d}"


def get_or_create_financial_account(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    account_type: FinancialAccountType = FinancialAccountType.general,
    admission_id: UUID | None = None,
    appointment_id: UUID | None = None,
    created_by_name: str = "System",
) -> FinancialAccount:
    """
    Get existing open financial account matching episode (admission/appointment),
    or create a new episode financial account with collision-safe numbering.
    """
    query = db.query(FinancialAccount).filter(
        FinancialAccount.hospital_id == hospital_id,
        FinancialAccount.patient_id == patient_id,
        FinancialAccount.status == FinancialAccountStatus.open,
    )
    if admission_id:
        existing = query.filter(FinancialAccount.admission_id == admission_id).first()
        if existing:
            return existing
        # Serialize first-account creation for this admission. A partial unique
        # index below remains the database-level guard for callers racing here.
        from modules.inpatient.entities.admission import Admission

        db.query(Admission.id).filter(
            Admission.id == admission_id,
            Admission.hospital_id == hospital_id,
            Admission.patient_id == patient_id,
        ).with_for_update().first()
        existing = query.filter(FinancialAccount.admission_id == admission_id).first()
        if existing:
            return existing
    elif appointment_id:
        existing = query.filter(FinancialAccount.appointment_id == appointment_id).first()
        if existing:
            return existing
    else:
        # Check for open general account
        existing = query.filter(
            FinancialAccount.account_type == account_type,
            FinancialAccount.admission_id.is_(None),
            FinancialAccount.appointment_id.is_(None),
        ).first()
        if existing:
            return existing

    account_number = next_financial_account_number(db, hospital_id)

    account = FinancialAccount(
        hospital_id=hospital_id,
        patient_id=patient_id,
        account_number=account_number,
        account_type=account_type,
        status=FinancialAccountStatus.open,
        admission_id=admission_id,
        appointment_id=appointment_id,
        created_by_name=created_by_name,
    )
    try:
        with db.begin_nested():
            db.add(account)
            db.flush()
    except Exception:
        # Concurrent insert might have completed for this admission
        if admission_id:
            existing = query.filter(FinancialAccount.admission_id == admission_id).first()
            if existing:
                return existing
        raise
    return account


def close_financial_account(
    db: Session,
    hospital_id: UUID,
    account_id: UUID,
) -> FinancialAccount | None:
    """Close an episode financial account upon settlement."""
    account = (
        db.query(FinancialAccount)
        .filter(
            FinancialAccount.id == account_id,
            FinancialAccount.hospital_id == hospital_id,
        )
        .first()
    )
    if not account:
        return None
    account.status = FinancialAccountStatus.closed
    account.closed_at = datetime.now(timezone.utc)
    return account


# ── Charge Operations ────────────────────────────────────────────────────────

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
    discount_reason: str | None = None,
    quantity: float = 1.0,
    unit_price: float = 0.0,
    tax_amount: float = 0.0,
    gst_rate: float | None = 0.0,
    hsn_sac_code: str | None = None,
    account_id: UUID | None = None,
    notes: str | None = None,
    created_by_name: str = "",
) -> BillingCharge:
    """Create charge if not already present for this source. Idempotent. Does not commit."""
    if source_id is not None:
        existing = find_charge_by_source(db, hospital_id, source_type, source_id)
        if existing:
            return existing

    disc, net = compute_net(charge_amount, discount_amount, discount_percent, tax_amount)
    status = BillingChargeStatus.paid if net <= 0 else BillingChargeStatus.pending
    actual_unit_price = unit_price if unit_price > 0 else (charge_amount / max(1.0, quantity))

    # Auto-link to open episode account if none provided
    if account_id is None:
        if source_type in (BillingSourceType.bed, BillingSourceType.admission) and source_id:
            acc = get_or_create_financial_account(
                db,
                hospital_id=hospital_id,
                patient_id=patient_id,
                account_type=FinancialAccountType.ipd,
                admission_id=source_id,
                created_by_name=created_by_name,
            )
            account_id = acc.id
        elif source_type == BillingSourceType.consultation and source_id:
            acc = get_or_create_financial_account(
                db,
                hospital_id=hospital_id,
                patient_id=patient_id,
                account_type=FinancialAccountType.opd,
                appointment_id=source_id,
                created_by_name=created_by_name,
            )
            account_id = acc.id

    row = BillingCharge(
        hospital_id=hospital_id,
        patient_id=patient_id,
        account_id=account_id,
        source_type=source_type,
        source_id=source_id,
        description=description.strip()[:512],
        quantity=float(quantity or 1.0),
        unit_price=round(float(actual_unit_price), 2),
        charge_amount=round(float(charge_amount or 0), 2),
        discount_amount=disc,
        discount_percent=discount_percent,
        discount_reason=discount_reason,
        tax_amount=round(float(tax_amount or 0), 2),
        gst_rate=gst_rate,
        hsn_sac_code=hsn_sac_code,
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
    *,
    reason: str = "Source service cancelled",
    actor: dict[str, Any] | None = None,
    performed: bool = False,
) -> BillingCharge | None:
    """Compatibility wrapper around the canonical charge reversal service."""
    from modules.billing.services.charge_reversal_service import cancel_charges_for_source

    results = cancel_charges_for_source(
        db,
        hospital_id,
        source_type,
        source_id,
        reason=reason,
        actor=actor,
        performed=performed,
    )
    return results[0].charge if results else None


def bed_stay_days(admitted_at: datetime, discharged_at: datetime) -> int:
    """
    Billable bed days using standard hospital midnight census accounting.
    Same-day stay bills minimum 1 day. Multi-day bills calendar days crossing midnights.
    """
    start = admitted_at if admitted_at.tzinfo else admitted_at.replace(tzinfo=timezone.utc)
    end = discharged_at if discharged_at.tzinfo else discharged_at.replace(tzinfo=timezone.utc)
    calendar_diff = (end.date() - start.date()).days
    if calendar_diff <= 0:
        return 1
    return max(1, calendar_diff)


def ensure_admission_charge(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    admission_id: UUID,
    ward_name: str | None,
    admission_fee: float,
    created_by_name: str = "",
) -> BillingCharge | None:
    """Record one-time admission charge for IPD stay."""
    fee = float(admission_fee or 0)
    if fee <= 0:
        return None
    return ensure_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient_id,
        source_type=BillingSourceType.admission,
        source_id=admission_id,
        description=f"Admission Charge — {ward_name or 'Ward'}"[:512],
        charge_amount=fee,
        created_by_name=created_by_name or "System",
    )


def ensure_bed_charge_for_admission(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    admission_id: UUID,
    admitted_at: datetime,
    discharged_at: datetime | None = None,
    as_of: datetime | None = None,
    ward_name: str | None = None,
    room_code: str | None = None,
    bed_code: str | None = None,
    bed_charge_per_day: float = 0.0,
    created_by_name: str = "",
) -> BillingCharge | None:
    """Calculate and post bed stay daily charges up to discharge or as_of for active stays, accounting for transfer segments."""
    effective_end = discharged_at or as_of or datetime.now(timezone.utc)
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

    charge: BillingCharge | None = None

    if segments:
        total_amount = 0.0
        total_days = 0
        notes_parts = []
        for s in segments:
            seg_start = s.started_at
            seg_end = s.ended_at or effective_end
            seg_days = bed_stay_days(seg_start, seg_end)
            seg_rate = float(s.rate_per_day or 0.0)
            seg_amount = round(seg_rate * seg_days, 2)
            total_amount += seg_amount
            total_days += seg_days
            notes_parts.append(f"seg(rate={seg_rate}, days={seg_days}, amt={seg_amount})")
        
        amount = round(total_amount, 2)
        if amount <= 0:
            return None
        days = max(1, total_days)
        day_label = "Day" if days == 1 else "Days"
        place = " / ".join(
            p for p in [ward_name or None, room_code or None, bed_code or None] if p
        ) or "Bed"
        existing = find_charge_by_source(db, hospital_id, BillingSourceType.bed, admission_id)
        if existing:
            from modules.billing.services.service_charge_resolver import sync_service_charge_amount
            charge = sync_service_charge_amount(
                db,
                hospital_id=hospital_id,
                patient_id=patient_id,
                source_type=BillingSourceType.bed,
                source_id=admission_id,
                charge_amount=amount,
                description=f"Bed Charges ({days} {day_label}) — {place}"[:512],
                reason=f"Bed stay updated ({days} {day_label})",
                created_by_name=created_by_name or "System",
            )
            if charge:
                charge.quantity = float(days)
                charge.unit_price = round(amount / days, 2) if days > 0 else 0.0
                charge.notes = f"multi_segment; days={days}; {'; '.join(notes_parts)}"[:512]
        else:
            charge = ensure_charge(
                db,
                hospital_id=hospital_id,
                patient_id=patient_id,
                source_type=BillingSourceType.bed,
                source_id=admission_id,
                description=f"Bed Charges ({days} {day_label}) — {place}"[:512],
                quantity=float(days),
                unit_price=round(amount / days, 2) if days > 0 else 0.0,
                charge_amount=amount,
                created_by_name=created_by_name or "System",
                notes=f"multi_segment; days={days}; {'; '.join(notes_parts)}"[:512],
            )
    else:
        days = bed_stay_days(admitted_at, effective_end)
        amount = round(float(bed_charge_per_day or 0) * days, 2)
        if amount <= 0:
            return None
        day_label = "Day" if days == 1 else "Days"
        place = " / ".join(
            p for p in [ward_name or None, room_code or None, bed_code or None] if p
        ) or "Bed"
        existing = find_charge_by_source(db, hospital_id, BillingSourceType.bed, admission_id)
        if existing:
            from modules.billing.services.service_charge_resolver import sync_service_charge_amount
            charge = sync_service_charge_amount(
                db,
                hospital_id=hospital_id,
                patient_id=patient_id,
                source_type=BillingSourceType.bed,
                source_id=admission_id,
                charge_amount=amount,
                description=f"Bed Charges ({days} {day_label}) — {place}"[:512],
                reason=f"Bed stay updated ({days} {day_label})",
                created_by_name=created_by_name or "System",
            )
            if charge:
                charge.quantity = float(days)
                charge.unit_price = float(bed_charge_per_day or 0)
                charge.notes = f"days={days}; rate_per_day={float(bed_charge_per_day or 0)}"
        else:
            charge = ensure_charge(
                db,
                hospital_id=hospital_id,
                patient_id=patient_id,
                source_type=BillingSourceType.bed,
                source_id=admission_id,
                description=f"Bed Charges ({days} {day_label}) — {place}"[:512],
                quantity=float(days),
                unit_price=float(bed_charge_per_day or 0),
                charge_amount=amount,
                created_by_name=created_by_name or "System",
                notes=f"days={days}; rate_per_day={float(bed_charge_per_day or 0)}",
            )

    if charge and charge.account_id:
        draw_down_account_deposits_for_charge(
            db,
            hospital_id=hospital_id,
            charge=charge,
            created_by_name=created_by_name or "System",
        )

    return charge


MONEY_EPS = 0.005


def charge_allocated_totals_bulk(db: Session, charge_ids: list[UUID]) -> dict[UUID, float]:
    """Net money applied to multiple charges in a single grouped query."""
    if not charge_ids:
        return {}
    db.flush()
    rows = (
        db.query(
            BillingPaymentAllocation.charge_id,
            func.coalesce(func.sum(BillingPaymentAllocation.allocated_amount), 0),
        )
        .filter(BillingPaymentAllocation.charge_id.in_(charge_ids))
        .group_by(BillingPaymentAllocation.charge_id)
        .all()
    )
    res = {cid: 0.0 for cid in charge_ids}
    for cid, total in rows:
        if cid:
            res[cid] = round(float(total or 0), 2)
    return res


def charge_allocated_total(db: Session, charge_id: UUID) -> float:
    """Net money applied to a charge: SUM of all allocation rows (reversals are negative)."""
    db.flush()
    total = (
        db.query(func.coalesce(func.sum(BillingPaymentAllocation.allocated_amount), 0))
        .filter(BillingPaymentAllocation.charge_id == charge_id)
        .scalar()
    )
    return round(float(total or 0), 2)


def recompute_charge_financials(
    db: Session, charge: BillingCharge, allocated_total: float | None = None
) -> BillingCharge:
    """Single source of truth for ``amount_paid`` / settlement ``status`` of a charge.

    ``amount_paid`` is the sum of the charge's allocation rows (payment + deposit
    allocations, net of reversals). ``status`` is derived from it and can never
    be set independently: paid when fully covered (or net <= 0), partially_paid
    when some money is applied, pending otherwise. Cancelled charges keep their
    status; their allocations must have been reversed by the reversal service.
    If ``allocated_total`` is provided, it is used directly without issuing a query.
    """
    if allocated_total is not None:
        allocated = max(0.0, round(float(allocated_total), 2))
    else:
        allocated = max(0.0, charge_allocated_total(db, charge.id))
    net = round(float(charge.net_amount or 0), 2)
    if charge.status == BillingChargeStatus.cancelled:
        charge.amount_paid = allocated
        return charge
    if net <= 0:
        # Zero/negative nets (full waivers, pharmacy returns) are settled by definition.
        charge.status = BillingChargeStatus.paid
        charge.amount_paid = net if net < 0 else allocated
    elif allocated >= net - MONEY_EPS:
        charge.status = BillingChargeStatus.paid
        charge.amount_paid = allocated
    elif allocated > 0:
        charge.status = BillingChargeStatus.partially_paid
        charge.amount_paid = allocated
    else:
        charge.status = BillingChargeStatus.pending
        charge.amount_paid = 0.0
    return charge


def allocation_remaining(db: Session, allocation: BillingPaymentAllocation) -> float:
    """Unreversed portion of an original (positive) allocation row."""
    db.flush()
    reversed_sum = (
        db.query(func.coalesce(func.sum(BillingPaymentAllocation.allocated_amount), 0))
        .filter(BillingPaymentAllocation.reversal_of_id == allocation.id)
        .scalar()
    )
    return round(float(allocation.allocated_amount or 0) + float(reversed_sum or 0), 2)


def allocations_remaining_bulk(
    db: Session, allocations: list[BillingPaymentAllocation]
) -> dict[UUID, float]:
    """Bulk computation of remaining unreversed portions for a list of original allocations."""
    if not allocations:
        return {}
    db.flush()
    alloc_ids = [a.id for a in allocations]
    reversals = (
        db.query(
            BillingPaymentAllocation.reversal_of_id,
            func.coalesce(func.sum(BillingPaymentAllocation.allocated_amount), 0),
        )
        .filter(BillingPaymentAllocation.reversal_of_id.in_(alloc_ids))
        .group_by(BillingPaymentAllocation.reversal_of_id)
        .all()
    )
    rev_by_id = {row[0]: float(row[1]) for row in reversals}
    return {
        a.id: round(float(a.allocated_amount or 0) + rev_by_id.get(a.id, 0.0), 2)
        for a in allocations
    }


def active_allocations_for_charge(
    db: Session, charge_id: UUID
) -> list[tuple[BillingPaymentAllocation, float]]:
    """Original allocations on a charge that still hold money, newest first.

    Returns ``(allocation, remaining_amount)`` pairs. Deterministic order: newest
    ``created_at`` first; ties broken by larger amount first, then id.
    """
    rows = (
        db.query(BillingPaymentAllocation)
        .filter(
            BillingPaymentAllocation.charge_id == charge_id,
            BillingPaymentAllocation.reversal_of_id.is_(None),
            BillingPaymentAllocation.allocated_amount > 0,
        )
        .all()
    )
    rem_map = allocations_remaining_bulk(db, rows)
    out = [(a, rem_map.get(a.id, float(a.allocated_amount or 0))) for a in rows]
    out = [(a, rem) for a, rem in out if rem > MONEY_EPS]
    out.sort(key=lambda t: (t[0].created_at, float(t[0].allocated_amount), str(t[0].id)), reverse=True)
    return out


def reverse_allocation(
    db: Session,
    allocation: BillingPaymentAllocation,
    amount: float,
    *,
    reason: str,
    created_by_name: str,
    refund_id: UUID | None = None,
    credit_deposit_id: UUID | None = None,
    precomputed_remaining: float | None = None,
    flush: bool = True,
) -> BillingPaymentAllocation:
    """Append a negative allocation row reversing ``amount`` of ``allocation``.

    History is never mutated. Deposit-funded money is returned to the source
    deposit's ``available_amount``. Payment-funded money must be attributed to
    either a refund (``refund_id``) or a cancellation credit deposit
    (``credit_deposit_id``) so the payment's balances stay reconcilable.
    Does not recompute the charge; callers do that once per affected charge.
    """
    amt = round(float(amount), 2)
    if amt <= 0:
        raise ValueError("Reversal amount must be greater than zero")
    if allocation.reversal_of_id is not None or float(allocation.allocated_amount or 0) <= 0:
        raise ValueError("Only original allocations can be reversed")
    if precomputed_remaining is not None:
        remaining = round(float(precomputed_remaining), 2)
    else:
        remaining = allocation_remaining(db, allocation)
    if amt > remaining + MONEY_EPS:
        raise ValueError(
            f"Reversal ₹{amt:,.2f} exceeds the unreversed allocation balance ₹{remaining:,.2f}"
        )
    if allocation.payment_id is not None and refund_id is None and credit_deposit_id is None:
        raise ValueError("Payment allocation reversal requires a refund or credit destination")

    row = BillingPaymentAllocation(
        hospital_id=allocation.hospital_id,
        payment_id=allocation.payment_id,
        deposit_id=allocation.deposit_id,
        charge_id=allocation.charge_id,
        allocated_amount=-amt,
        reversal_of_id=allocation.id,
        refund_id=refund_id,
        credit_deposit_id=credit_deposit_id,
        reversal_reason=(reason or "").strip()[:512] or None,
        created_by_name=created_by_name or "System",
    )
    db.add(row)

    if allocation.deposit_id is not None and allocation.payment_id is None:
        deposit = (
            db.query(BillingDeposit)
            .filter(BillingDeposit.id == allocation.deposit_id)
            .with_for_update()
            .first()
        )
        if deposit is not None:
            deposit.available_amount = round(float(deposit.available_amount or 0) + amt, 2)
            refresh_deposit_status(db, deposit)
    if flush:
        db.flush()
    return row


# ── Payment & Selective Allocation ───────────────────────────────────────────

def allocate_payment_to_charges(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    amount: float,
    *,
    payment_id: UUID | None = None,
    deposit_id: UUID | None = None,
    created_by_name: str = "System",
    account_id: UUID | None = None,
) -> float:
    """
    FIFO allocate payment/deposit across pending or partially paid charges.
    Creates explicit BillingPaymentAllocation audit records for every settled dollar.
    Returns amount allocated.
    """
    remaining = round(float(amount), 2)
    if remaining <= 0:
        return 0.0

    query = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id == patient_id,
            BillingCharge.status.in_(
                [BillingChargeStatus.pending, BillingChargeStatus.partially_paid]
            ),
        )
    )
    if account_id is not None:
        query = query.filter(BillingCharge.account_id == account_id)
    else:
        query = query.filter(BillingCharge.account_id.is_(None))
    
    charges = query.order_by(BillingCharge.created_at.asc()).with_for_update().all()
    if not charges:
        return 0.0

    # Bulk load current allocation totals for all locked candidate charges in 1 query
    allocated_totals = charge_allocated_totals_bulk(db, [c.id for c in charges])
    total_allocated = 0.0

    for charge in charges:
        if remaining <= 0:
            break
        current_alloc = allocated_totals.get(charge.id, 0.0)
        recompute_charge_financials(db, charge, allocated_total=current_alloc)
        due = round(float(charge.net_amount) - float(charge.amount_paid or 0), 2)
        if due <= 0:
            continue
        apply = min(due, remaining)
        remaining = round(remaining - apply, 2)
        total_allocated = round(total_allocated + apply, 2)

        db.add(
            BillingPaymentAllocation(
                hospital_id=hospital_id,
                payment_id=payment_id,
                deposit_id=deposit_id,
                charge_id=charge.id,
                allocated_amount=apply,
                created_by_name=created_by_name,
            )
        )
        current_alloc = round(current_alloc + apply, 2)
        allocated_totals[charge.id] = current_alloc
        recompute_charge_financials(db, charge, allocated_total=current_alloc)

    if total_allocated > 0:
        db.flush()
        from modules.billing.services.invoice_service import refresh_invoice_paid_status
        refresh_invoice_paid_status(db, hospital_id, patient_id)

    return total_allocated


def allocate_specific_charges(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    allocations_plan: list[dict[str, Any]],  # list of {"charge_id": UUID, "amount": float, "discount_amount": float?, "discount_percent": float?, "discount_reason": str?}
    *,
    payment_id: UUID | None = None,
    deposit_id: UUID | None = None,
    created_by_name: str = "Cashier",
    account_id: UUID | None = None,
) -> float:
    """
    Selectively allocate money strictly to specific charges chosen by cashier.
    Applies any per-item discount requested at settlement before recording allocations.
    Enforces that charges belong to the scoped account_id when account-bound.
    """
    is_deposit_allocation = deposit_id is not None
    if is_deposit_allocation and account_id is None:
        dep_row = db.query(BillingDeposit.account_id).filter(BillingDeposit.id == deposit_id).first()
        if dep_row:
            account_id = dep_row[0]

    plan_cids = [item["charge_id"] for item in allocations_plan if item.get("charge_id")]
    if not plan_cids:
        return 0.0

    # Bulk lock charges targeted in plan
    locked_charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.id.in_(plan_cids),
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id == patient_id,
            BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
        )
        .with_for_update()
        .all()
    )
    charge_by_id = {c.id: c for c in locked_charges}
    allocated_totals = charge_allocated_totals_bulk(db, list(charge_by_id.keys()))

    total_allocated = 0.0
    discounted_cids = [
        item["charge_id"]
        for item in allocations_plan
        if item.get("charge_id") and (item.get("discount_amount") is not None or item.get("discount_percent") is not None)
    ]
    already_invoiced_set: set[UUID] = set()
    if discounted_cids:
        from modules.billing.services.invoice_service import charges_already_invoiced
        already_invoiced_set = set(charges_already_invoiced(db, hospital_id, discounted_cids))

    for item in allocations_plan:
        cid = item["charge_id"]
        alloc_amt = round(float(item["amount"]), 2)
        if alloc_amt <= 0 and not (item.get("discount_amount") or item.get("discount_percent")):
            continue

        charge = charge_by_id.get(cid)
        if not charge:
            continue

        if is_deposit_allocation:
            if charge.account_id != account_id:
                raise ValueError(
                    f"Cross-account allocation prohibited: Deposit belongs to account '{account_id}', "
                    f"which does not match charge account '{charge.account_id}'."
                )
        elif account_id is not None:
            if charge.account_id != account_id:
                raise ValueError(
                    f"Cross-account allocation prohibited: Charge {charge.id} belongs to account {charge.account_id}, "
                    f"which does not match the required account {account_id}."
                )

        # If discount is specified at payment settlement, apply and recalculate net.
        disc_amt = item.get("discount_amount")
        disc_pct = item.get("discount_percent")
        disc_reason = item.get("discount_reason")
        if disc_amt is not None or disc_pct is not None:
            if cid in already_invoiced_set:
                raise ValueError(
                    "Discount cannot be applied at settlement: charge "
                    f"{charge.description} is on an active invoice. "
                    "Issue an adjustment or credit note instead."
                )
            if disc_amt is not None:
                charge.discount_amount = round(float(disc_amt), 2)
            if disc_pct is not None:
                charge.discount_percent = round(float(disc_pct), 2)
            if disc_reason:
                charge.discount_reason = disc_reason

            # Recalculate net_amount (compute_net returns (disc, net) tuple)
            disc, net = compute_net(
                float(charge.charge_amount or 0),
                float(charge.discount_amount or 0),
                charge.discount_percent,
                float(charge.tax_amount or 0),
            )
            charge.discount_amount = disc
            charge.net_amount = net

        current_alloc = allocated_totals.get(charge.id, 0.0)
        recompute_charge_financials(db, charge, allocated_total=current_alloc)
        due = round(float(charge.net_amount) - float(charge.amount_paid or 0), 2)
        apply = min(due, alloc_amt)
        if apply > 0:
            total_allocated = round(total_allocated + apply, 2)
            db.add(
                BillingPaymentAllocation(
                    hospital_id=hospital_id,
                    payment_id=payment_id,
                    deposit_id=deposit_id,
                    charge_id=charge.id,
                    allocated_amount=apply,
                    created_by_name=created_by_name,
                )
            )
            current_alloc = round(current_alloc + apply, 2)
            allocated_totals[charge.id] = current_alloc
        recompute_charge_financials(db, charge, allocated_total=current_alloc)

    return total_allocated


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
    account_id: UUID | None = None,
    reference_number: str | None = None,
    idempotency_key: str | None = None,
    allocations_plan: list[dict[str, Any]] | None = None,
    allocate: bool = True,
) -> BillingPayment:
    """
    Record patient payment tender.
    If allocations_plan is supplied, selectively allocates to those exact charges.
    Otherwise, if allocate=True, applies FIFO across open charges.
    Idempotent on (hospital_id, idempotency_key).
    """
    if idempotency_key:
        existing = (
            db.query(BillingPayment)
            .filter(
                BillingPayment.hospital_id == hospital_id,
                BillingPayment.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing:
            return existing

    pay = BillingPayment(
        hospital_id=hospital_id,
        patient_id=patient_id,
        account_id=account_id,
        amount=round(float(amount), 2),
        payment_date=payment_date,
        payment_method=payment_method,
        reference_number=reference_number,
        idempotency_key=idempotency_key,
        notes=notes,
        received_by_name=received_by_name or "Staff",
    )
    db.add(pay)
    db.flush()

    if allocations_plan:
        allocate_specific_charges(
            db,
            hospital_id,
            patient_id,
            allocations_plan,
            payment_id=pay.id,
            created_by_name=received_by_name,
            account_id=account_id,
        )
    elif allocate:
        allocate_payment_to_charges(
            db,
            hospital_id,
            patient_id,
            pay.amount,
            payment_id=pay.id,
            created_by_name=received_by_name,
            account_id=account_id,
        )
    return pay


# ── Advance / Deposit System ────────────────────────────────────────────────

def _max_existing_seq_for(db: Session, column, hospital_id: UUID, prefix: str) -> int:
    """Highest numeric suffix already stored for ``prefix`` (counter resync)."""
    max_seq = 0
    for (num,) in (
        db.query(column)
        .filter(
            column.like(f"{prefix}%"),
        )
        .filter(
            BillingDeposit.hospital_id == hospital_id
            if column is BillingDeposit.deposit_number
            else BillingRefund.hospital_id == hospital_id,
        )
        .all()
    ):
        try:
            max_seq = max(max_seq, int(str(num).split("-")[-1]))
        except (ValueError, IndexError):
            continue
    return max_seq


def next_deposit_number(db: Session, hospital_id: UUID, year: int | None = None) -> str:
    """Generate sequential deposit voucher number: DEP-YYYY-NNNNN (atomic)."""
    from shared.database.sequences import (
        counter_exists as _exists,
        ensure_counter_at_least as _ensure,
        next_deposit_number as _atomic_next_dep,
    )
    y = year or date.today().year
    counter_name = f"deposit_{y}"
    if not _exists(db, hospital_id, counter_name):
        _ensure(db, hospital_id, counter_name, _max_existing_seq_for(
            db, BillingDeposit.deposit_number, hospital_id, f"DEP-{y}-"))
    return _atomic_next_dep(db, hospital_id, year)


def create_deposit(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    amount: float,
    deposit_date: date,
    deposit_type: str = "admission",
    payment_method: BillingPaymentMethod = BillingPaymentMethod.cash,
    account_id: UUID | None = None,
    admission_id: UUID | None = None,
    reference_number: str | None = None,
    idempotency_key: str | None = None,
    notes: str | None = None,
    received_by_name: str = "Cashier",
) -> BillingDeposit:
    """Record advance deposit held as unearned liability. Validates admission and account integrity."""
    if idempotency_key:
        existing = (
            db.query(BillingDeposit)
            .filter(
                BillingDeposit.hospital_id == hospital_id,
                BillingDeposit.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing:
            return existing

    # Scope and deposit type integrity
    if deposit_type == "general":
        if admission_id is not None or account_id is not None:
            raise ValueError(
                "General account deposits must be unscoped: admission_id and account_id must be None"
            )
    elif deposit_type == "ipd_advance":
        if admission_id is None and account_id is None:
            raise ValueError(
                "Inpatient advance deposit (ipd_advance) requires a valid admission_id or account_id"
            )

    # Integrity verification
    if admission_id is not None:
        from modules.inpatient.entities.admission import Admission
        adm = (
            db.query(Admission)
            .filter(Admission.id == admission_id, Admission.hospital_id == hospital_id)
            .first()
        )
        if not adm:
            raise ValueError(f"Admission {admission_id} not found in hospital")
        if adm.patient_id != patient_id:
            raise ValueError(f"Admission {admission_id} belongs to patient {adm.patient_id}, not {patient_id}")

        if account_id is not None:
            acc = (
                db.query(FinancialAccount)
                .filter(FinancialAccount.id == account_id, FinancialAccount.hospital_id == hospital_id)
                .first()
            )
            if not acc:
                raise ValueError(f"FinancialAccount {account_id} not found")
            if acc.patient_id != patient_id:
                raise ValueError(f"FinancialAccount {account_id} belongs to a different patient")
            if acc.admission_id != admission_id:
                raise ValueError(
                    f"FinancialAccount {account_id} is associated with admission {acc.admission_id}, not {admission_id}"
                )
        else:
            acc = get_or_create_financial_account(
                db,
                hospital_id=hospital_id,
                patient_id=patient_id,
                account_type=FinancialAccountType.ipd,
                admission_id=admission_id,
                created_by_name=received_by_name,
            )
            account_id = acc.id
    elif account_id is not None:
        acc = (
            db.query(FinancialAccount)
            .filter(FinancialAccount.id == account_id, FinancialAccount.hospital_id == hospital_id)
            .first()
        )
        if not acc:
            raise ValueError(f"FinancialAccount {account_id} not found")
        if acc.patient_id != patient_id:
            raise ValueError(f"FinancialAccount {account_id} belongs to a different patient")
        if acc.admission_id:
            admission_id = acc.admission_id

    dep_amt = round(float(amount), 2)
    deposit_num = next_deposit_number(db, hospital_id)
    deposit = BillingDeposit(
        hospital_id=hospital_id,
        patient_id=patient_id,
        account_id=account_id,
        admission_id=admission_id,
        deposit_number=deposit_num,
        deposit_date=deposit_date,
        deposit_type=deposit_type,
        payment_method=payment_method,
        original_amount=dep_amt,
        available_amount=dep_amt,
        status=DepositStatus.available,
        reference_number=reference_number,
        idempotency_key=idempotency_key,
        notes=notes,
        received_by_name=received_by_name,
    )
    db.add(deposit)
    db.flush()
    return deposit


def draw_down_deposit_for_charges(
    db: Session,
    *,
    hospital_id: UUID,
    deposit_id: UUID,
    allocations_plan: list[dict[str, Any]] | None = None,
    created_by_name: str = "Staff",
    max_amount: float | None = None,
) -> float:
    """
    Draw down available advance deposit funds against open charges (selective or FIFO).
    Transactionally locks deposit and affected charges. Allocates only what is needed.
    """
    deposit = (
        db.query(BillingDeposit)
        .filter(
            BillingDeposit.id == deposit_id,
            BillingDeposit.hospital_id == hospital_id,
            BillingDeposit.available_amount > 0,
        )
        .with_for_update()
        .first()
    )
    if not deposit:
        return 0.0

    avail = float(deposit.available_amount)
    if max_amount is not None:
        avail = min(avail, float(max_amount))
    if avail <= 0:
        return 0.0

    if allocations_plan:
        # Never allocate more than the deposit actually holds.
        budget = round(avail, 2)
        capped_plan: list[dict[str, Any]] = []
        for item in allocations_plan:
            amt = round(min(float(item.get("amount") or 0), budget), 2)
            has_discount = bool(item.get("discount_amount") or item.get("discount_percent"))
            if amt <= 0 and not has_discount:
                continue
            capped_plan.append({**item, "amount": max(0.0, amt)})
            budget = round(budget - max(0.0, amt), 2)
        allocated = allocate_specific_charges(
            db,
            hospital_id,
            deposit.patient_id,
            capped_plan,
            deposit_id=deposit.id,
            created_by_name=created_by_name,
            account_id=deposit.account_id,
        )
    else:
        allocated = allocate_payment_to_charges(
            db,
            hospital_id,
            deposit.patient_id,
            avail,
            deposit_id=deposit.id,
            created_by_name=created_by_name,
            account_id=deposit.account_id,
        )

    new_avail = round(float(deposit.available_amount) - allocated, 2)
    deposit.available_amount = max(0.0, new_avail)
    refresh_deposit_status(db, deposit)
    db.flush()
    return allocated


def draw_down_deposits_bulk_for_account(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    account_id: UUID | None,
    created_by_name: str = "Discharge Settlement",
) -> float:
    """Bulk draw down available advance deposit funds across open charges.

    Transactionally locks eligible deposits and pending charges once with
    with_for_update(). Precomputes charge allocation totals in bulk and
    performs drawdown allocations in memory without per-deposit query loops.
    """
    deposit_query = (
        db.query(BillingDeposit)
        .filter(
            BillingDeposit.hospital_id == hospital_id,
            BillingDeposit.patient_id == patient_id,
            BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
            BillingDeposit.available_amount > 0,
        )
    )
    if account_id is not None:
        deposit_query = deposit_query.filter(BillingDeposit.account_id == account_id)
    else:
        deposit_query = deposit_query.filter(BillingDeposit.account_id.is_(None))

    deposits = (
        deposit_query
        .order_by(BillingDeposit.created_at.asc())
        .with_for_update()
        .all()
    )
    if not deposits:
        return 0.0

    charge_query = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id == patient_id,
            BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
        )
    )
    if account_id is not None:
        charge_query = charge_query.filter(BillingCharge.account_id == account_id)
    else:
        charge_query = charge_query.filter(BillingCharge.account_id.is_(None))

    charges = charge_query.order_by(BillingCharge.created_at.asc()).with_for_update().all()
    if not charges:
        return 0.0

    # Bulk precompute already allocated amounts per charge
    charge_ids = [c.id for c in charges]
    alloc_map = charge_allocated_totals_bulk(db, charge_ids)
    due_by_charge: dict[UUID, float] = {}
    for c in charges:
        paid = alloc_map.get(c.id, float(c.amount_paid or 0))
        net = float(c.net_amount or 0)
        due_by_charge[c.id] = max(0.0, round(net - paid, 2))

    total_allocated = 0.0
    touched_charges: set[UUID] = set()
    charge_idx = 0
    num_charges = len(charges)

    for dep in deposits:
        dep_avail = float(dep.available_amount or 0)
        dep_used = 0.0
        if dep_avail <= MONEY_EPS:
            continue

        while charge_idx < num_charges and dep_avail > MONEY_EPS:
            c = charges[charge_idx]
            c_due = due_by_charge[c.id]
            if c_due <= MONEY_EPS:
                charge_idx += 1
                continue

            take = round(min(dep_avail, c_due), 2)
            if take <= 0:
                charge_idx += 1
                continue

            row = BillingPaymentAllocation(
                hospital_id=hospital_id,
                payment_id=None,
                deposit_id=dep.id,
                charge_id=c.id,
                allocated_amount=take,
                created_by_name=created_by_name,
            )
            db.add(row)
            touched_charges.add(c.id)

            dep_avail = round(dep_avail - take, 2)
            rem_due = round(c_due - take, 2)
            due_by_charge[c.id] = rem_due
            dep_used = round(dep_used + take, 2)
            total_allocated = round(total_allocated + take, 2)

            if rem_due <= MONEY_EPS:
                charge_idx += 1

        if dep_used > 0:
            dep.available_amount = max(0.0, round(float(dep.available_amount) - dep_used, 2))
            refresh_deposit_status(db, dep, is_applied=True)

    db.flush()

    if touched_charges:
        alloc_totals = charge_allocated_totals_bulk(db, list(touched_charges))
        for c in charges:
            if c.id in touched_charges:
                recompute_charge_financials(db, c, allocated_total=alloc_totals.get(c.id, 0.0))

        from modules.billing.services.invoice_service import refresh_invoice_paid_status
        refresh_invoice_paid_status(db, hospital_id, patient_id)

    return total_allocated


def refresh_deposit_status(
    db: Session,
    deposit: BillingDeposit,
    *,
    is_applied: bool | None = None,
) -> BillingDeposit:
    """Derive deposit status from its balance.

    available: untouched; partially_allocated: partly consumed (allocations and/or
    refunds); exhausted: nothing left and some of it is applied to charges;
    refunded: nothing left and none of it is applied to charges.
    """
    avail = round(float(deposit.available_amount or 0), 2)
    original = round(float(deposit.original_amount or 0), 2)
    if avail >= original - MONEY_EPS:
        deposit.status = DepositStatus.available
    elif avail > MONEY_EPS:
        deposit.status = DepositStatus.partially_allocated
    elif is_applied is not None:
        deposit.status = DepositStatus.exhausted if is_applied else DepositStatus.refunded
    else:
        db.flush()
        applied = (
            db.query(func.coalesce(func.sum(BillingPaymentAllocation.allocated_amount), 0))
            .filter(
                BillingPaymentAllocation.deposit_id == deposit.id,
                BillingPaymentAllocation.payment_id.is_(None),
            )
            .scalar()
        )
        deposit.status = (
            DepositStatus.exhausted if float(applied or 0) > MONEY_EPS else DepositStatus.refunded
        )
    return deposit


def eligible_account_deposits(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    account_id: UUID | None,
    for_update: bool = False,
) -> list[BillingDeposit]:
    """Deposits with balance that may fund charges of ``account_id``, oldest first.

    Never crosses accounts: an account-scoped charge only draws deposits of that
    same account; an unscoped charge only draws unscoped (patient-level) deposits.
    """
    q = db.query(BillingDeposit).filter(
        BillingDeposit.hospital_id == hospital_id,
        BillingDeposit.patient_id == patient_id,
        BillingDeposit.available_amount > 0,
        BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
    )
    if account_id is not None:
        q = q.filter(BillingDeposit.account_id == account_id)
    else:
        q = q.filter(BillingDeposit.account_id.is_(None))
    q = q.order_by(BillingDeposit.created_at.asc(), BillingDeposit.deposit_number.asc())
    if for_update:
        q = q.with_for_update()
    return q.all()


def draw_down_account_deposits_for_charge(
    db: Session,
    *,
    hospital_id: UUID,
    charge: BillingCharge,
    created_by_name: str = "Staff",
    max_amount: float | None = None,
) -> float:
    """Cover a charge's outstanding balance from eligible deposits of its own account.

    Draws from multiple deposits, oldest first, with explicit allocation rows.
    Returns the total amount allocated.
    """
    if charge.status in (BillingChargeStatus.cancelled, BillingChargeStatus.paid):
        return 0.0
    if charge.amount_paid is not None and float(charge.amount_paid) >= float(charge.net_amount or 0) - MONEY_EPS:
        return 0.0
    recompute_charge_financials(db, charge)
    outstanding = round(float(charge.net_amount or 0) - float(charge.amount_paid or 0), 2)
    if max_amount is not None:
        outstanding = min(outstanding, round(float(max_amount), 2))
    if outstanding <= MONEY_EPS:
        return 0.0

    total = 0.0
    for dep in eligible_account_deposits(
        db,
        hospital_id=hospital_id,
        patient_id=charge.patient_id,
        account_id=charge.account_id,
        for_update=True,
    ):
        if outstanding <= MONEY_EPS:
            break
        take = min(outstanding, round(float(dep.available_amount or 0), 2))
        if take <= 0:
            continue
        got = draw_down_deposit_for_charges(
            db,
            hospital_id=hospital_id,
            deposit_id=dep.id,
            allocations_plan=[{"charge_id": charge.id, "amount": take}],
            created_by_name=created_by_name,
        )
        total = round(total + got, 2)
        outstanding = round(outstanding - got, 2)
    return total


def auto_apply_eligible_deposits_to_charge(
    db: Session,
    *,
    hospital_id: UUID,
    charge: BillingCharge,
    created_by_name: str = "Auto Deposit Settlement",
    max_amount: float | None = None,
) -> float:
    """Centralized service operation to auto-settle a charge against eligible account deposits."""
    return draw_down_account_deposits_for_charge(
        db,
        hospital_id=hospital_id,
        charge=charge,
        created_by_name=created_by_name,
        max_amount=max_amount,
    )


# ── Payment balances ────────────────────────────────────────────────────────

def payment_balances(db: Session, payment: BillingPayment) -> dict[str, float]:
    """Decompose a payment: amount = allocated + refunded + transferred_to_credit + unallocated.

    - allocated: net money currently applied to charges (reversals subtracted)
    - refunded: processed refund vouchers against this payment
    - transferred_to_credit: money released from cancelled/re-priced charges into
      a cancellation_credit deposit (that deposit is now the refundable balance)
    """
    db.flush()
    allocated = (
        db.query(func.coalesce(func.sum(BillingPaymentAllocation.allocated_amount), 0))
        .filter(BillingPaymentAllocation.payment_id == payment.id)
        .scalar()
    )
    transferred = (
        db.query(func.coalesce(func.sum(BillingPaymentAllocation.allocated_amount), 0))
        .filter(
            BillingPaymentAllocation.payment_id == payment.id,
            BillingPaymentAllocation.credit_deposit_id.isnot(None),
        )
        .scalar()
    )
    refunded = (
        db.query(func.coalesce(func.sum(BillingRefund.amount), 0))
        .filter(
            BillingRefund.payment_id == payment.id,
            BillingRefund.status == RefundStatus.processed,
        )
        .scalar()
    )
    amount = round(float(payment.amount or 0), 2)
    allocated_f = round(float(allocated or 0), 2)
    transferred_f = round(-float(transferred or 0), 2)
    refunded_f = round(float(refunded or 0), 2)
    unallocated = round(amount - allocated_f - refunded_f - transferred_f, 2)
    return {
        "amount": amount,
        "allocated": allocated_f,
        "refunded": refunded_f,
        "transferred_to_credit": transferred_f,
        "unallocated": max(0.0, unallocated),
        "refundable": max(0.0, round(amount - refunded_f - transferred_f, 2)),
    }


def payment_unallocated_amount(db: Session, payment: BillingPayment) -> float:
    """Tender on this payment not applied, refunded, or moved to credit."""
    return payment_balances(db, payment)["unallocated"]


# ── Refund System ────────────────────────────────────────────────────────────

def next_refund_number(db: Session, hospital_id: UUID, year: int | None = None) -> str:
    """Generate sequential refund voucher number: REF-YYYY-NNNNN (atomic)."""
    from shared.database.sequences import (
        counter_exists as _exists,
        ensure_counter_at_least as _ensure,
        next_refund_number as _atomic_next_ref,
    )
    y = year or date.today().year
    counter_name = f"refund_{y}"
    if not _exists(db, hospital_id, counter_name):
        _ensure(db, hospital_id, counter_name, _max_existing_seq_for(
            db, BillingRefund.refund_number, hospital_id, f"REF-{y}-"))
    return _atomic_next_ref(db, hospital_id, year)


def process_refund(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    amount: float,
    reason: str,
    refund_date: date,
    refund_method: BillingPaymentMethod = BillingPaymentMethod.cash,
    payment_id: UUID | None = None,
    deposit_id: UUID | None = None,
    account_id: UUID | None = None,
    approved_by_name: str = "Admin",
    processed_by_name: str = "Cashier",
    charge_ids: list[UUID] | None = None,
) -> BillingRefund:
    """Process a sourced refund voucher and reconcile the ledger.

    Exactly one source is required:
    - ``deposit_id``: refunds only the deposit's unallocated ``available_amount``.
    - ``payment_id``: refunds up to the payment's refundable balance (amount minus
      earlier refunds minus money already moved to a cancellation credit). The
      refund is taken first from the payment's unallocated remainder, then by
      reversing its allocations — restricted to ``charge_ids`` when given,
      otherwise newest allocation first. Each reversal is a negative allocation
      row linked to this refund; affected charges and invoices are re-derived,
      so a refunded service is no longer financially cleared.
    """
    ref_amt = round(float(amount), 2)
    if ref_amt <= 0:
        raise ValueError("Refund amount must be greater than zero")
    if not payment_id and not deposit_id:
        raise ValueError("A refund must reference the source payment_id or deposit_id")
    if payment_id and deposit_id:
        raise ValueError("A refund must reference exactly one source (payment_id or deposit_id), not both")

    pay: BillingPayment | None = None
    to_reverse = 0.0
    if deposit_id:
        deposit = (
            db.query(BillingDeposit)
            .filter(
                BillingDeposit.id == deposit_id,
                BillingDeposit.hospital_id == hospital_id,
                BillingDeposit.patient_id == patient_id,
            )
            .with_for_update()
            .first()
        )
        if not deposit:
            raise ValueError("Target deposit not found")
        if ref_amt > round(float(deposit.available_amount), 2) + MONEY_EPS:
            raise ValueError(
                f"Refund amount exceeds available deposit balance (₹{float(deposit.available_amount):,.2f})"
            )
        deposit.available_amount = round(float(deposit.available_amount) - ref_amt, 2)
        refresh_deposit_status(db, deposit)
        if account_id is None:
            account_id = deposit.account_id
    else:
        pay = (
            db.query(BillingPayment)
            .filter(
                BillingPayment.id == payment_id,
                BillingPayment.hospital_id == hospital_id,
                BillingPayment.patient_id == patient_id,
            )
            .with_for_update()
            .first()
        )
        if not pay:
            raise ValueError("Target payment not found")
        balances = payment_balances(db, pay)
        if ref_amt > balances["refundable"] + MONEY_EPS:
            raise ValueError(
                "Refund amount exceeds the payment's refundable balance "
                f"(₹{balances['refundable']:,.2f}). Money already moved to a cancellation "
                "credit must be refunded from that credit deposit."
            )
        to_reverse = round(max(0.0, ref_amt - balances["unallocated"]), 2)
        if account_id is None:
            account_id = pay.account_id

    refund_num = next_refund_number(db, hospital_id)
    refund = BillingRefund(
        hospital_id=hospital_id,
        patient_id=patient_id,
        account_id=account_id,
        payment_id=payment_id,
        deposit_id=deposit_id,
        refund_number=refund_num,
        refund_date=refund_date,
        refund_method=refund_method,
        amount=amount if isinstance(amount, Decimal) else Decimal(str(ref_amt)),
        reason=reason.strip()[:512],
        status=RefundStatus.processed,
        approved_by_name=approved_by_name,
        processed_by_name=processed_by_name,
    )
    db.add(refund)
    db.flush()

    if pay is not None and to_reverse > MONEY_EPS:
        _reverse_payment_allocations_for_refund(
            db,
            pay,
            to_reverse,
            refund=refund,
            charge_ids=charge_ids,
            created_by_name=processed_by_name,
        )
    return refund


def _reverse_payment_allocations_for_refund(
    db: Session,
    pay: BillingPayment,
    amount: float,
    *,
    refund: BillingRefund,
    charge_ids: list[UUID] | None,
    created_by_name: str,
) -> list[BillingCharge]:
    originals = (
        db.query(BillingPaymentAllocation)
        .filter(
            BillingPaymentAllocation.payment_id == pay.id,
            BillingPaymentAllocation.reversal_of_id.is_(None),
            BillingPaymentAllocation.allocated_amount > 0,
        )
        .all()
    )
    if charge_ids:
        wanted = {UUID(str(c)) for c in charge_ids}
        originals = [a for a in originals if a.charge_id in wanted]
    rem_map = allocations_remaining_bulk(db, originals)
    candidates = [(a, rem_map.get(a.id, float(a.allocated_amount or 0))) for a in originals]
    candidates = [(a, rem) for a, rem in candidates if rem > MONEY_EPS]
    candidates.sort(key=lambda t: (t[0].created_at, float(t[0].allocated_amount), str(t[0].id)), reverse=True)

    available = round(sum(rem for _, rem in candidates), 2)
    if amount > available + MONEY_EPS:
        raise ValueError(
            f"Refund needs ₹{amount:,.2f} reversed from charge allocations but only "
            f"₹{available:,.2f} is allocated{' to the selected charges' if charge_ids else ''}."
        )

    needed_charge_ids = list({a.charge_id for a, _ in candidates if a.charge_id is not None})
    charges_by_id: dict[UUID, BillingCharge] = {}
    if needed_charge_ids:
        c_rows = (
            db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == pay.hospital_id,
                BillingCharge.id.in_(needed_charge_ids),
            )
            .with_for_update()
            .all()
        )
        charges_by_id = {c.id: c for c in c_rows}

    remaining = round(amount, 2)
    touched: dict[UUID, BillingCharge] = {}
    for alloc, rem in candidates:
        if remaining <= MONEY_EPS:
            break
        take = round(min(rem, remaining), 2)
        reverse_allocation(
            db,
            alloc,
            take,
            reason=f"Refund {refund.refund_number}: {refund.reason}",
            created_by_name=created_by_name,
            refund_id=refund.id,
            precomputed_remaining=rem,
            flush=False,
        )
        remaining = round(remaining - take, 2)
        charge = charges_by_id.get(alloc.charge_id)
        if charge is not None:
            touched[charge.id] = charge

    db.flush()

    if touched:
        alloc_totals = charge_allocated_totals_bulk(db, list(touched.keys()))
        for charge in touched.values():
            recompute_charge_financials(db, charge, allocated_total=alloc_totals.get(charge.id, 0.0))

        from modules.billing.services.invoice_service import refresh_invoice_paid_status

        refresh_invoice_paid_status(db, pay.hospital_id, pay.patient_id)
    return list(touched.values())


# ── Patient Ledger Totals & Bank Statement Timeline ─────────────────────────

def patient_ledger_totals(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    *,
    charges: list[BillingCharge] | None = None,
    payments: list[BillingPayment] | None = None,
    deposits: list[BillingDeposit] | None = None,
    account_id: UUID | None = None,
) -> dict[str, Any]:
    """
    Compute aggregate charges, payments, active deposits, outstanding balance,
    and net patient balance (outstanding less available deposits).
    """
    db.flush()
    if charges is None and payments is None and deposits is None:
        from sqlalchemy import case
        chg_row = (
            db.query(
                func.coalesce(func.sum(BillingCharge.net_amount), 0).label("tot_net"),
                func.coalesce(func.sum(BillingCharge.amount_paid), 0).label("tot_paid_alloc"),
                func.coalesce(
                    func.sum(
                        case(
                            (BillingCharge.net_amount > BillingCharge.amount_paid, BillingCharge.net_amount - BillingCharge.amount_paid),
                            else_=0.0,
                        )
                    ),
                    0,
                ).label("tot_out"),
                func.count(BillingCharge.id).label("cnt"),
            )
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.patient_id == patient_id,
                BillingCharge.status != BillingChargeStatus.cancelled,
                *( [BillingCharge.account_id == account_id] if account_id else [] ),
            )
            .first()
        )
        pay_row = (
            db.query(
                func.coalesce(func.sum(BillingPayment.amount), 0).label("tot_tender"),
                func.count(BillingPayment.id).label("cnt"),
            )
            .filter(
                BillingPayment.hospital_id == hospital_id,
                BillingPayment.patient_id == patient_id,
                *( [BillingPayment.account_id == account_id] if account_id else [] ),
            )
            .first()
        )
        dep_row = (
            db.query(
                func.coalesce(func.sum(BillingDeposit.available_amount), 0).label("tot_dep"),
            )
            .filter(
                BillingDeposit.hospital_id == hospital_id,
                BillingDeposit.patient_id == patient_id,
                BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
                *( [BillingDeposit.account_id == account_id] if account_id else [] ),
            )
            .first()
        )
        total_charges = round(float(chg_row.tot_net or 0), 2) if chg_row else 0.0
        total_paid_allocations = round(float(chg_row.tot_paid_alloc or 0), 2) if chg_row else 0.0
        outstanding = round(float(chg_row.tot_out or 0), 2) if chg_row else 0.0
        charge_count = int(chg_row.cnt or 0) if chg_row else 0

        total_paid_tender = round(float(pay_row.tot_tender or 0), 2) if pay_row else 0.0
        payment_count = int(pay_row.cnt or 0) if pay_row else 0

        total_deposits_avail = round(float(dep_row.tot_dep or 0), 2) if dep_row else 0.0

        total_paid = total_paid_allocations if account_id else total_paid_tender
        net_patient_balance = round(outstanding - total_deposits_avail, 2)

        return {
            "total_charges": total_charges,
            "total_paid": total_paid,
            "total_deposits_available": total_deposits_avail,
            "outstanding": outstanding,
            "net_patient_balance": net_patient_balance,
            "charge_count": charge_count,
            "payment_count": payment_count,
        }

    if charges is None:
        q = db.query(BillingCharge).filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id == patient_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        if account_id:
            q = q.filter(BillingCharge.account_id == account_id)
        charges = q.all()

    if payments is None:
        q = db.query(BillingPayment).filter(
            BillingPayment.hospital_id == hospital_id,
            BillingPayment.patient_id == patient_id,
        )
        if account_id:
            q = q.filter(BillingPayment.account_id == account_id)
        payments = q.all()

    if deposits is None:
        q = db.query(BillingDeposit).filter(
            BillingDeposit.hospital_id == hospital_id,
            BillingDeposit.patient_id == patient_id,
            BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
        )
        if account_id:
            q = q.filter(BillingDeposit.account_id == account_id)
        deposits = q.all()

    active_charges = [c for c in charges if c.status != BillingChargeStatus.cancelled]
    total_charges = round(sum(float(c.net_amount or 0) for c in active_charges), 2)
    total_paid_allocations = round(sum(float(c.amount_paid or 0) for c in active_charges), 2)
    total_paid_tender = round(sum(float(p.amount or 0) for p in payments), 2)
    total_deposits_avail = round(sum(float(d.available_amount or 0) for d in deposits), 2)

    # 1.2 Allocation-based outstanding: Remaining Patient Liability = Net Charges - Allocations Applied
    outstanding = round(
        sum(max(0.0, float(c.net_amount or 0) - float(c.amount_paid or 0)) for c in active_charges),
        2,
    )
    total_paid = total_paid_allocations if account_id else total_paid_tender
    net_patient_balance = round(outstanding - total_deposits_avail, 2)

    return {
        "total_charges": total_charges,
        "total_paid": total_paid,
        "total_deposits_available": total_deposits_avail,
        "outstanding": outstanding,
        "net_patient_balance": net_patient_balance,
        "charge_count": len(active_charges),
        "payment_count": len(payments),
    }


def patient_ledger_totals_bulk(
    db: Session, hospital_id: UUID, patient_ids: list[UUID]
) -> dict[UUID, dict[str, Any]]:
    """Compute aggregate totals for many patients in bulk queries using allocation-based outstanding."""
    unique_ids = list({pid for pid in patient_ids if pid is not None})
    result: dict[UUID, dict[str, Any]] = {
        pid: {
            "total_charges": 0.0,
            "total_paid": 0.0,
            "total_deposits_available": 0.0,
            "outstanding": 0.0,
            "net_patient_balance": 0.0,
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
    deposits = (
        db.query(BillingDeposit)
        .filter(
            BillingDeposit.hospital_id == hospital_id,
            BillingDeposit.patient_id.in_(unique_ids),
            BillingDeposit.status.in_([DepositStatus.available, DepositStatus.partially_allocated]),
        )
        .all()
    )

    charges_by_patient: dict[UUID, list[BillingCharge]] = {}
    for c in charges:
        charges_by_patient.setdefault(c.patient_id, []).append(c)
    payments_by_patient: dict[UUID, list[BillingPayment]] = {}
    for p in payments:
        payments_by_patient.setdefault(p.patient_id, []).append(p)
    deposits_by_patient: dict[UUID, list[BillingDeposit]] = {}
    for d in deposits:
        deposits_by_patient.setdefault(d.patient_id, []).append(d)

    for pid in unique_ids:
        p_charges = [c for c in charges_by_patient.get(pid, []) if c.status != BillingChargeStatus.cancelled]
        p_payments = payments_by_patient.get(pid, [])
        p_deposits = deposits_by_patient.get(pid, [])
        total_charges = round(sum(float(c.net_amount or 0) for c in p_charges), 2)
        total_paid = round(sum(float(p.amount or 0) for p in p_payments), 2)
        total_dep = round(sum(float(d.available_amount or 0) for d in p_deposits), 2)
        outstanding = round(
            sum(max(0.0, float(c.net_amount or 0) - float(c.amount_paid or 0)) for c in p_charges),
            2,
        )
        net_bal = round(outstanding - total_dep, 2)
        result[pid] = {
            "total_charges": total_charges,
            "total_paid": total_paid,
            "total_deposits_available": total_dep,
            "outstanding": outstanding,
            "net_patient_balance": net_bal,
            "charge_count": len(p_charges),
            "payment_count": len(p_payments),
        }
    return result


def build_ledger_entries(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    *,
    charges: list[BillingCharge] | None = None,
    payments: list[BillingPayment] | None = None,
    deposits: list[BillingDeposit] | None = None,
    refunds: list[BillingRefund] | None = None,
    account_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """
    Build chronological bank-statement ledger with exact forward-calculated running balance:
    Date | Description | Debit (Charge/Refund) | Credit (Payment/Deposit) | Running Balance | Reference
    """
    if charges is None:
        q = db.query(BillingCharge).filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id == patient_id,
        )
        if account_id:
            q = q.filter(BillingCharge.account_id == account_id)
        charges = q.all()

    if payments is None:
        q = db.query(BillingPayment).filter(
            BillingPayment.hospital_id == hospital_id,
            BillingPayment.patient_id == patient_id,
        )
        if account_id:
            q = q.filter(BillingPayment.account_id == account_id)
        payments = q.all()

    if deposits is None:
        q = db.query(BillingDeposit).filter(
            BillingDeposit.hospital_id == hospital_id,
            BillingDeposit.patient_id == patient_id,
        )
        if account_id:
            q = q.filter(BillingDeposit.account_id == account_id)
        deposits = q.all()

    if refunds is None:
        q = db.query(BillingRefund).filter(
            BillingRefund.hospital_id == hospital_id,
            BillingRefund.patient_id == patient_id,
        )
        if account_id:
            q = q.filter(BillingRefund.account_id == account_id)
        refunds = q.all()

    raw_items: list[dict[str, Any]] = []

    # 1. Charges (Debits: patient owes hospital)
    for c in charges:
        if c.status == BillingChargeStatus.cancelled:
            continue
        status_label = "settled" if c.status.value in ("settled", "paid") else c.status.value
        raw_items.append({
            "id": str(c.id),
            "entry_type": "charge",
            "occurred_at": c.created_at,
            "description": c.description,
            "source_type": c.source_type.value if c.source_type else None,
            "debit": float(c.net_amount),
            "credit": 0.0,
            "status": status_label,
            "ref_id": c.id,
            "reference_number": None,
            "payment_method": None,
            "business_date": c.created_at.date().isoformat() if c.created_at else None,
            "allocations": [],
        })

    # 2. Payments (Credits: patient paid hospital against bill)
    for p in payments:
        dt = p.created_at or (
            datetime.combine(p.payment_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            if p.payment_date else None
        )
        allocations_list = []
        total_allocated = 0.0
        if hasattr(p, "allocations") and p.allocations:
            for alloc in p.allocations:
                amt = float(alloc.allocated_amount or 0.0)
                if amt > 0:
                    total_allocated += amt
                    charge_desc = alloc.charge.description if (hasattr(alloc, "charge") and alloc.charge) else None
                    allocations_list.append({
                        "allocation_id": str(alloc.id),
                        "charge_id": str(alloc.charge_id),
                        "amount": amt,
                        "charge_description": charge_desc,
                    })
        pay_amount = float(p.amount)
        if total_allocated >= pay_amount and pay_amount > 0:
            payment_status = "allocated"
        elif total_allocated > 0:
            payment_status = "partially_allocated"
        else:
            payment_status = "received"

        method_str = p.payment_method.value if hasattr(p.payment_method, "value") else str(p.payment_method)
        raw_items.append({
            "id": str(p.id),
            "entry_type": "payment",
            "occurred_at": dt,
            "description": f"Payment ({method_str.replace('_', ' ').title()})",
            "source_type": None,
            "debit": 0.0,
            "credit": pay_amount,
            "status": payment_status,
            "ref_id": p.id,
            "reference_number": p.reference_number,
            "payment_method": method_str,
            "business_date": p.payment_date.isoformat() if p.payment_date else None,
            "allocations": allocations_list,
        })

    # 3. Deposits (Credits: patient advance payment). A cancellation credit is money
    # already credited through its source payment, so it is shown without re-crediting.
    for d in deposits:
        dt = d.created_at or (
            datetime.combine(d.deposit_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            if d.deposit_date else None
        )
        is_credit_reclass = d.deposit_type == "cancellation_credit"
        allocations_list = []
        if hasattr(d, "allocations") and d.allocations:
            for alloc in d.allocations:
                amt = float(alloc.allocated_amount or 0.0)
                if amt > 0:
                    charge_desc = alloc.charge.description if (hasattr(alloc, "charge") and alloc.charge) else None
                    allocations_list.append({
                        "allocation_id": str(alloc.id),
                        "charge_id": str(alloc.charge_id),
                        "amount": amt,
                        "charge_description": charge_desc,
                    })
        method_str = (
            d.payment_method.value if hasattr(getattr(d, "payment_method", None), "value")
            else str(getattr(d, "payment_method", "") or "")
        )
        raw_items.append({
            "id": str(d.id),
            "entry_type": "deposit",
            "occurred_at": dt,
            "description": (
                f"Cancellation Credit ({d.deposit_number}) — reclassified from payment"
                if is_credit_reclass
                else f"Advance Deposit ({d.deposit_type.title()} · {d.deposit_number})"
            ),
            "source_type": None,
            "debit": 0.0,
            "credit": 0.0 if is_credit_reclass else float(d.original_amount),
            "status": d.status.value if hasattr(d.status, "value") else str(d.status),
            "ref_id": d.id,
            "reference_number": d.deposit_number,
            "payment_method": method_str or None,
            "business_date": d.deposit_date.isoformat() if d.deposit_date else None,
            "allocations": allocations_list,
        })

    # 4. Refunds (Debits: hospital returns cash/credit to patient)
    for r in refunds:
        dt = r.created_at or (
            datetime.combine(r.refund_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            if r.refund_date else None
        )
        method_str = (
            r.refund_method.value if hasattr(getattr(r, "refund_method", None), "value")
            else str(getattr(r, "refund_method", "") or "")
        )
        raw_items.append({
            "id": str(r.id),
            "entry_type": "refund",
            "occurred_at": dt,
            "description": f"Refund Voucher ({r.refund_number} · {r.reason[:40]})",
            "source_type": None,
            "debit": float(r.amount),
            "credit": 0.0,
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "ref_id": r.id,
            "reference_number": r.refund_number,
            "payment_method": method_str or None,
            "business_date": r.refund_date.isoformat() if r.refund_date else None,
            "allocations": [],
        })

    def _norm(dt: Any) -> datetime:
        if not dt:
            return datetime.min.replace(tzinfo=timezone.utc)
        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime.combine(dt, datetime.min.time())
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # Sort forward in time to compute running passbook balance.
    # Tie-breaking: Debits (charges) before credits (payments/deposits) when timestamps are identical.
    raw_items.sort(key=lambda x: (_norm(x["occurred_at"]), 0 if x["debit"] > 0 else 1))

    running = 0.0
    for item in raw_items:
        # Debits increase patient payable balance; credits reduce it
        running = round(running + item["debit"] - item["credit"], 2)
        item["running_balance"] = running

    # Return reverse chronological (newest first) for UI display with exact running balance attached
    raw_items.sort(key=lambda x: (_norm(x["occurred_at"]), 1 if x["debit"] > 0 else 0), reverse=True)
    return raw_items


# ── Serialization DTO Helpers ───────────────────────────────────────────────

def charge_to_dict(c: BillingCharge, patient: Patient | None = None) -> dict[str, Any]:
    """Serialize BillingCharge ORM model to dictionary with patient & account context."""
    p = patient or c.patient
    return {
        "id": c.id,
        "hospital_id": c.hospital_id,
        "patient_id": c.patient_id,
        "account_id": c.account_id,
        "source_type": c.source_type,
        "source_id": c.source_id,
        "description": c.description,
        "quantity": float(c.quantity or 1.0),
        "unit_price": float(c.unit_price or 0.0),
        "charge_amount": c.charge_amount,
        "discount_amount": c.discount_amount,
        "discount_percent": c.discount_percent,
        "discount_reason": c.discount_reason,
        "tax_amount": float(c.tax_amount or 0.0),
        "gst_rate": c.gst_rate,
        "hsn_sac_code": c.hsn_sac_code,
        "net_amount": c.net_amount,
        "amount_paid": c.amount_paid,
        "status": c.status,
        "notes": c.notes,
        "created_by_name": c.created_by_name,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "patient_name": p.name if p else None,
        "patient_uhid": p.uhid if p else None,
        "account_number": c.account.account_number if getattr(c, "account", None) else None,
    }


def payment_to_dict(p: BillingPayment, patient: Patient | None = None) -> dict[str, Any]:
    """Serialize BillingPayment ORM model to dictionary with patient context."""
    pat = patient or p.patient
    allocations_dtos = [
        {
            "id": a.id,
            "payment_id": a.payment_id,
            "deposit_id": a.deposit_id,
            "charge_id": a.charge_id,
            "allocated_amount": float(a.allocated_amount),
            "reversal_of_id": a.reversal_of_id,
            "refund_id": a.refund_id,
            "credit_deposit_id": a.credit_deposit_id,
            "reversal_reason": a.reversal_reason,
            "created_by_name": a.created_by_name,
            "created_at": a.created_at,
            "charge_description": a.charge.description if a.charge else None,
        }
        for a in (p.allocations or [])
    ]
    return {
        "id": p.id,
        "hospital_id": p.hospital_id,
        "patient_id": p.patient_id,
        "account_id": p.account_id,
        "amount": p.amount,
        "payment_date": p.payment_date,
        "payment_method": p.payment_method,
        "reference_number": p.reference_number,
        "notes": p.notes,
        "received_by_name": p.received_by_name,
        "created_at": p.created_at,
        "patient_name": pat.name if pat else None,
        "patient_uhid": pat.uhid if pat else None,
        "allocations": allocations_dtos,
    }


def deposit_to_dict(d: BillingDeposit, patient: Patient | None = None) -> dict[str, Any]:
    """Serialize BillingDeposit ORM model to dictionary."""
    pat = patient or d.patient
    return {
        "id": d.id,
        "hospital_id": d.hospital_id,
        "patient_id": d.patient_id,
        "account_id": d.account_id,
        "admission_id": d.admission_id,
        "deposit_number": d.deposit_number,
        "deposit_date": d.deposit_date,
        "deposit_type": d.deposit_type,
        "payment_method": d.payment_method,
        "original_amount": d.original_amount,
        "available_amount": d.available_amount,
        "status": d.status,
        "reference_number": d.reference_number,
        "notes": d.notes,
        "received_by_name": d.received_by_name,
        "created_at": d.created_at,
        "patient_name": pat.name if pat else None,
        "patient_uhid": pat.uhid if pat else None,
    }


def refund_to_dict(r: BillingRefund, patient: Patient | None = None) -> dict[str, Any]:
    """Serialize BillingRefund ORM model to dictionary."""
    pat = patient or r.patient
    return {
        "id": r.id,
        "hospital_id": r.hospital_id,
        "patient_id": r.patient_id,
        "account_id": r.account_id,
        "payment_id": r.payment_id,
        "deposit_id": r.deposit_id,
        "refund_number": r.refund_number,
        "refund_date": r.refund_date,
        "refund_method": r.refund_method,
        "amount": r.amount,
        "reason": r.reason,
        "status": r.status,
        "approved_by_name": r.approved_by_name,
        "processed_by_name": r.processed_by_name,
        "created_at": r.created_at,
        "patient_name": pat.name if pat else None,
        "patient_uhid": pat.uhid if pat else None,
    }


def account_to_dict(acc: FinancialAccount, patient: Patient | None = None) -> dict[str, Any]:
    """Serialize FinancialAccount ORM model to dictionary."""
    pat = patient or acc.patient
    return {
        "id": acc.id,
        "hospital_id": acc.hospital_id,
        "patient_id": acc.patient_id,
        "account_number": acc.account_number,
        "account_type": acc.account_type,
        "status": acc.status,
        "admission_id": acc.admission_id,
        "appointment_id": acc.appointment_id,
        "opened_at": acc.opened_at,
        "closed_at": acc.closed_at,
        "notes": acc.notes,
        "created_by_name": acc.created_by_name,
        "patient_name": pat.name if pat else None,
        "patient_uhid": pat.uhid if pat else None,
    }


def prepare_ipd_bill(
    db: Session,
    *,
    hospital_id: UUID,
    account_id: UUID | None = None,
    admission_id: UUID | None = None,
    as_of: datetime | None = None,
    actor_name: str = "Billing Preparation",
) -> dict[str, Any]:
    """Prepare and reconcile an active IPD bill before presenting charges or generating invoice.

    1. Validates hospital tenancy and resolves FinancialAccount & Admission.
    2. Reconciles current BedStaySegments through as_of (default now).
    3. Creates/updates canonical bed charge.
    4. Auto-draws down only deposits belonging strictly to this IPD account.
    5. Recomputes current financial state.
    6. Returns current account charges and financial summary.
    """
    if not account_id and not admission_id:
        raise ValueError("Either account_id or admission_id must be provided to prepare IPD bill")

    acc: FinancialAccount | None = None
    if account_id is not None:
        acc = (
            db.query(FinancialAccount)
            .filter(
                FinancialAccount.id == account_id,
                FinancialAccount.hospital_id == hospital_id,
            )
            .first()
        )
        if not acc:
            raise ValueError(f"FinancialAccount {account_id} not found")
        admission_id = acc.admission_id

    adm = None
    if admission_id is not None:
        from modules.inpatient.entities.admission import Admission
        adm = (
            db.query(Admission)
            .filter(
                Admission.id == admission_id,
                Admission.hospital_id == hospital_id,
            )
            .first()
        )
        if not adm:
            raise ValueError(f"Admission {admission_id} not found")

        if acc is None:
            acc = get_or_create_financial_account(
                db,
                hospital_id=hospital_id,
                patient_id=adm.patient_id,
                account_type=FinancialAccountType.ipd,
                admission_id=adm.id,
                created_by_name=actor_name,
            )

    if acc is None:
        raise ValueError("Could not resolve IPD financial account")

    effective_as_of = as_of or datetime.now(timezone.utc)

    # Reconcile bed stay charges through effective_as_of
    if adm is not None:
        ensure_bed_charge_for_admission(
            db,
            hospital_id=hospital_id,
            patient_id=adm.patient_id,
            admission_id=adm.id,
            admitted_at=adm.admitted_at,
            discharged_at=adm.discharged_at,
            as_of=effective_as_of,
            ward_name=getattr(adm, "ward_name", None),
            room_code=getattr(adm, "room_code", None),
            bed_code=getattr(adm, "bed_code", None),
            bed_charge_per_day=float(getattr(adm, "bed_charge_per_day", 0.0) or 0.0),
            created_by_name=actor_name,
        )

    # Auto-apply deposits belonging strictly to this exact IPD account
    draw_down_deposits_bulk_for_account(
        db,
        hospital_id=hospital_id,
        patient_id=acc.patient_id,
        account_id=acc.id,
        created_by_name=actor_name,
    )

    db.flush()

    charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.account_id == acc.id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .order_by(BillingCharge.created_at.asc())
        .all()
    )

    total_charges = round(sum(float(c.net_amount or 0.0) for c in charges), 2)
    total_paid = round(sum(float(c.amount_paid or 0.0) for c in charges), 2)
    outstanding = round(max(0.0, total_charges - total_paid), 2)

    return {
        "account_id": acc.id,
        "admission_id": acc.admission_id,
        "as_of": effective_as_of.isoformat(),
        "total_charges": total_charges,
        "total_paid": total_paid,
        "outstanding": outstanding,
        "charges": [charge_to_dict(c) for c in charges],
    }

