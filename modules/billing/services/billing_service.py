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

    # Generate sequential account number: ACC-YYYY-NNNNN
    year = date.today().year
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
    account_number = f"{prefix}{max_seq + 1:05d}"

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
    Same-day stay bills minimum 1 day. Multi-day bills calendar days crossing midnights
    plus 1 if checkout is after standard 12:00 PM cutoff.
    """
    start = admitted_at if admitted_at.tzinfo else admitted_at.replace(tzinfo=timezone.utc)
    end = discharged_at if discharged_at.tzinfo else discharged_at.replace(tzinfo=timezone.utc)
    calendar_diff = (end.date() - start.date()).days
    if calendar_diff <= 0:
        return 1
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
    discharged_at: datetime,
    ward_name: str | None,
    room_code: str | None,
    bed_code: str | None,
    bed_charge_per_day: float,
    created_by_name: str = "",
) -> BillingCharge:
    """Calculate and post bed stay daily charges upon discharge, accounting for transfer segments."""
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
        if amount <= 0:
            return None
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
            quantity=float(days),
            unit_price=round(amount / days, 2) if days > 0 else 0.0,
            charge_amount=amount,
            created_by_name=created_by_name or "System",
            notes=f"multi_segment; days={days}; {'; '.join(notes_parts)}"[:512],
        )

    days = bed_stay_days(admitted_at, discharged_at)
    amount = round(float(bed_charge_per_day or 0) * days, 2)
    if amount <= 0:
        return None
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
        quantity=float(days),
        unit_price=float(bed_charge_per_day or 0),
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
    if account_id:
        query = query.filter(BillingCharge.account_id == account_id)
    
    charges = query.order_by(BillingCharge.created_at.asc()).with_for_update().all()
    total_allocated = 0.0

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
        total_allocated = round(total_allocated + apply, 2)
        _refresh_charge_status(charge)

        # Record explicit allocation line
        allocation = BillingPaymentAllocation(
            hospital_id=hospital_id,
            payment_id=payment_id,
            deposit_id=deposit_id,
            charge_id=charge.id,
            allocated_amount=apply,
            created_by_name=created_by_name,
        )
        db.add(allocation)

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
    if deposit_id and account_id is None:
        dep_row = db.query(BillingDeposit.account_id).filter(BillingDeposit.id == deposit_id).first()
        if dep_row and dep_row[0]:
            account_id = dep_row[0]

    total_allocated = 0.0
    for item in allocations_plan:
        cid = item["charge_id"]
        alloc_amt = round(float(item["amount"]), 2)
        if alloc_amt <= 0 and not (item.get("discount_amount") or item.get("discount_percent")):
            continue

        charge = (
            db.query(BillingCharge)
            .filter(
                BillingCharge.id == cid,
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.patient_id == patient_id,
                BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
            )
            .with_for_update()
            .first()
        )
        if not charge:
            continue

        if account_id and charge.account_id != account_id:
            raise ValueError(
                f"Cross-account allocation prohibited: Charge {charge.id} belongs to account {charge.account_id}, "
                f"which does not match the required account {account_id}."
            )

        # If discount is specified at payment settlement, apply and recalculate net.
        # Convention mirrors UpdateChargeAction: effective discount is
        # max(amount, percent-derived) capped at the charge (see compute_net).
        disc_amt = item.get("discount_amount")
        disc_pct = item.get("discount_percent")
        disc_reason = item.get("discount_reason")
        if disc_amt is not None or disc_pct is not None:
            from modules.billing.services.invoice_service import (
                charges_already_invoiced,
            )
            if charges_already_invoiced(db, hospital_id, [cid]):
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

        due = round(float(charge.net_amount) - float(charge.amount_paid or 0), 2)
        apply = min(due, alloc_amt)
        if apply > 0:
            charge.amount_paid = round(float(charge.amount_paid or 0) + apply, 2)
            total_allocated = round(total_allocated + apply, 2)
        
        _refresh_charge_status(charge)

        if apply > 0:
            allocation = BillingPaymentAllocation(
                hospital_id=hospital_id,
                payment_id=payment_id,
                deposit_id=deposit_id,
                charge_id=charge.id,
                allocated_amount=apply,
                created_by_name=created_by_name,
            )
            db.add(allocation)

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
        ensure_counter_at_least as _ensure,
        next_deposit_number as _atomic_next_dep,
    )
    y = year or date.today().year
    _ensure(db, hospital_id, f"deposit_{y}", _max_existing_seq_for(
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
        allocated = allocate_specific_charges(
            db,
            hospital_id,
            deposit.patient_id,
            allocations_plan,
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
    if deposit.available_amount <= 0:
        deposit.status = DepositStatus.exhausted
    else:
        deposit.status = DepositStatus.partially_allocated
    return allocated


# ── Refund System ────────────────────────────────────────────────────────────

def next_refund_number(db: Session, hospital_id: UUID, year: int | None = None) -> str:
    """Generate sequential refund voucher number: REF-YYYY-NNNNN (atomic)."""
    from shared.database.sequences import (
        ensure_counter_at_least as _ensure,
        next_refund_number as _atomic_next_ref,
    )
    y = year or date.today().year
    _ensure(db, hospital_id, f"refund_{y}", _max_existing_seq_for(
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
) -> BillingRefund:
    """Process validated refund voucher from payment or unused deposit."""
    ref_amt = round(float(amount), 2)
    if ref_amt <= 0:
        raise ValueError("Refund amount must be greater than zero")

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
        if ref_amt > round(float(deposit.available_amount), 2):
            raise ValueError(f"Refund amount exceeds available deposit balance (₹{deposit.available_amount:,.2f})")
        
        deposit.available_amount = round(float(deposit.available_amount) - ref_amt, 2)
        if deposit.available_amount <= 0:
            deposit.status = DepositStatus.refunded
        else:
            deposit.status = DepositStatus.partially_allocated
    elif payment_id:
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
        # Check cumulative refunds against this payment
        past_refunds = (
            db.query(func.coalesce(func.sum(BillingRefund.amount), 0.0))
            .filter(
                BillingRefund.hospital_id == hospital_id,
                BillingRefund.payment_id == payment_id,
                BillingRefund.status == RefundStatus.processed,
            )
            .scalar()
            or 0.0
        )
        if ref_amt + past_refunds > float(pay.amount):
            raise ValueError(f"Total refund cannot exceed payment amount (Remaining: ₹{float(pay.amount) - past_refunds:,.2f})")

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
    return refund


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
        raw_items.append({
            "id": str(c.id),
            "entry_type": "charge",
            "occurred_at": c.created_at,
            "description": c.description,
            "source_type": c.source_type.value if c.source_type else None,
            "debit": float(c.net_amount),
            "credit": 0.0,
            "status": c.status.value,
            "ref_id": c.id,
            "reference_number": None,
        })

    # 2. Payments (Credits: patient paid hospital against bill)
    for p in payments:
        dt = (
            datetime.combine(p.payment_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            if p.payment_date else p.created_at
        )
        raw_items.append({
            "id": str(p.id),
            "entry_type": "payment",
            "occurred_at": dt,
            "description": f"Payment ({p.payment_method.value.replace('_', ' ').title()})",
            "source_type": None,
            "debit": 0.0,
            "credit": float(p.amount),
            "status": "received",
            "ref_id": p.id,
            "reference_number": p.reference_number,
        })

    # 3. Deposits (Credits: patient advance payment)
    for d in deposits:
        dt = (
            datetime.combine(d.deposit_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            if d.deposit_date else d.created_at
        )
        raw_items.append({
            "id": str(d.id),
            "entry_type": "deposit",
            "occurred_at": dt,
            "description": f"Advance Deposit ({d.deposit_type.title()} · {d.deposit_number})",
            "source_type": None,
            "debit": 0.0,
            "credit": float(d.original_amount),
            "status": d.status.value,
            "ref_id": d.id,
            "reference_number": d.deposit_number,
        })

    # 4. Refunds (Debits: hospital returns cash/credit to patient)
    for r in refunds:
        dt = (
            datetime.combine(r.refund_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            if r.refund_date else r.created_at
        )
        raw_items.append({
            "id": str(r.id),
            "entry_type": "refund",
            "occurred_at": dt,
            "description": f"Refund Voucher ({r.refund_number} · {r.reason[:40]})",
            "source_type": None,
            "debit": float(r.amount),
            "credit": 0.0,
            "status": r.status.value,
            "ref_id": r.id,
            "reference_number": r.refund_number,
        })

    def _norm(dt: Any) -> datetime:
        if not dt:
            return datetime.min.replace(tzinfo=timezone.utc)
        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime.combine(dt, datetime.min.time())
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # Sort forward in time to compute running passbook balance
    raw_items.sort(key=lambda x: _norm(x["occurred_at"]))

    running = 0.0
    for item in raw_items:
        # Debits increase patient payable balance; credits reduce it
        running = round(running + item["debit"] - item["credit"], 2)
        item["running_balance"] = running

    # Return reverse chronological (newest first) for UI display with exact running balance attached
    raw_items.sort(key=lambda x: _norm(x["occurred_at"]), reverse=True)
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
