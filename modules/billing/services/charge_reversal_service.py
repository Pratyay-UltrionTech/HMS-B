"""Canonical financial cancellation / reversal of billing charges.

Every module that cancels a billable service (Lab, Radiology, Pharmacy, OT,
Appointments, Admission, prescription lifecycle, billing desk) goes through
``cancel_charge`` / ``cancel_charges_for_source``. Rules:

- unpaid & not performed  → charge ``cancelled``.
- paid / partially paid & not performed → every allocation still holding money
  is reversed with an append-only negative allocation row, ``amount_paid`` → 0,
  charge ``cancelled``. The released money becomes refundable credit.
- performed → refused (``ValidationError``) unless the caller explicitly uses the
  authorized adjustment path (``allow_performed_adjustment=True``).

Single canonical representation of refundable credit (no double counting):

- Money that came from a **payment** is moved into ONE ``BillingDeposit`` of
  ``deposit_type="cancellation_credit"`` on the charge's account (admission copied
  from the account) or patient. The reversal rows carry ``credit_deposit_id`` so
  ``payment_balances`` reports the amount as ``transferred_to_credit`` — the
  payment's own unallocated balance is NOT restored. The credit deposit is spent
  or refunded through the normal deposit paths.
- Money that came from a **deposit** goes back to that same deposit
  (``available_amount`` restored); no new deposit is created.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingPaymentMethod,
    BillingSourceType,
    FinancialAccount,
)
from modules.billing.services.billing_service import (
    MONEY_EPS,
    active_allocations_for_charge,
    create_deposit,
    recompute_charge_financials,
    reverse_allocation,
)
from shared.audit.service import write_audit_log
from shared.exceptions.base import ValidationError

CANCELLATION_CREDIT_DEPOSIT_TYPE = "cancellation_credit"

SYSTEM_ACTOR: dict[str, Any] = {"sub": "system", "name": "System", "role": "system"}


@dataclass
class ChargeReversalResult:
    charge: BillingCharge
    amount_reversed: float = 0.0
    amount_to_credit: float = 0.0
    amount_restored_to_deposits: float = 0.0
    credit_deposit_id: UUID | None = None
    already_cancelled: bool = False
    restored_deposit_ids: list[UUID] = field(default_factory=list)


def _actor_name(actor: dict[str, Any] | None) -> str:
    a = actor or SYSTEM_ACTOR
    return str(a.get("name") or a.get("sub") or "System")


def _account_admission_id(db: Session, account_id: UUID | None) -> UUID | None:
    if account_id is None:
        return None
    row = db.query(FinancialAccount.admission_id).filter(FinancialAccount.id == account_id).first()
    return row[0] if row else None


def release_charge_allocations(
    db: Session,
    charge: BillingCharge,
    *,
    amount: float | None,
    reason: str,
    actor: dict[str, Any] | None = None,
) -> ChargeReversalResult:
    """Reverse up to ``amount`` (all when None) of the money applied to ``charge``.

    Newest allocation first. Payment-funded money goes to a single new
    cancellation-credit deposit; deposit-funded money returns to its deposit.
    Recomputes the charge. Does not change a non-cancelled charge's status by
    itself beyond what ``recompute_charge_financials`` derives.
    """
    actor_name = _actor_name(actor)
    result = ChargeReversalResult(charge=charge)
    active = active_allocations_for_charge(db, charge.id)
    budget = None if amount is None else round(float(amount), 2)

    plan: list[tuple[Any, float]] = []
    for alloc, remaining in active:
        if budget is not None and budget <= MONEY_EPS:
            break
        take = remaining if budget is None else round(min(remaining, budget), 2)
        if take <= MONEY_EPS:
            continue
        plan.append((alloc, take))
        if budget is not None:
            budget = round(budget - take, 2)

    def _is_deposit_funded(a) -> bool:
        return a.deposit_id is not None and a.payment_id is None

    to_credit = round(sum(t for a, t in plan if not _is_deposit_funded(a)), 2)
    credit_deposit: BillingDeposit | None = None
    if to_credit > MONEY_EPS:
        credit_deposit = create_deposit(
            db,
            hospital_id=charge.hospital_id,
            patient_id=charge.patient_id,
            amount=to_credit,
            deposit_date=date.today(),
            deposit_type=CANCELLATION_CREDIT_DEPOSIT_TYPE,
            payment_method=BillingPaymentMethod.other,
            account_id=charge.account_id,
            notes=(
                f"Credit released from charge {charge.id} "
                f"({(charge.description or '')[:60]}; {charge.source_type.value}"
                f"{' ' + str(charge.source_id) if charge.source_id else ''}): {reason}"
            )[:1000],
            received_by_name=actor_name,
        )
        result.credit_deposit_id = credit_deposit.id

    for alloc, take in plan:
        if _is_deposit_funded(alloc):
            reverse_allocation(db, alloc, take, reason=reason, created_by_name=actor_name)
            result.amount_restored_to_deposits = round(result.amount_restored_to_deposits + take, 2)
            if alloc.deposit_id not in result.restored_deposit_ids:
                result.restored_deposit_ids.append(alloc.deposit_id)
        else:
            reverse_allocation(
                db,
                alloc,
                take,
                reason=reason,
                created_by_name=actor_name,
                credit_deposit_id=credit_deposit.id if credit_deposit else None,
            )
        result.amount_reversed = round(result.amount_reversed + take, 2)
    result.amount_to_credit = to_credit
    recompute_charge_financials(db, charge)
    return result


def _refresh_invoices_if_invoiced(db: Session, charge: BillingCharge) -> None:
    from modules.billing.services.invoice_service import (
        charges_already_invoiced,
        refresh_invoice_paid_status,
    )

    if charges_already_invoiced(db, charge.hospital_id, [charge.id]):
        refresh_invoice_paid_status(db, charge.hospital_id, charge.patient_id)


def _audit_details(db: Session, charge: BillingCharge, result: ChargeReversalResult, reason: str) -> dict[str, Any]:
    admission_id = _account_admission_id(db, charge.account_id)
    return {
        "charge_id": str(charge.id),
        "patient_id": str(charge.patient_id),
        "account_id": str(charge.account_id) if charge.account_id else None,
        "admission_id": str(admission_id) if admission_id else None,
        "source_type": charge.source_type.value if charge.source_type else None,
        "source_id": str(charge.source_id) if charge.source_id else None,
        "amount_reversed": result.amount_reversed,
        "amount_to_credit": result.amount_to_credit,
        "amount_restored_to_deposits": result.amount_restored_to_deposits,
        "credit_deposit_id": str(result.credit_deposit_id) if result.credit_deposit_id else None,
        "restored_deposit_ids": [str(d) for d in result.restored_deposit_ids],
        "reason": reason,
    }


def cancel_charge(
    db: Session,
    charge: BillingCharge,
    *,
    reason: str,
    actor: dict[str, Any] | None = None,
    performed: bool = False,
    allow_performed_adjustment: bool = False,
) -> ChargeReversalResult:
    """Cancel one charge, reversing any money applied to it (see module docstring).

    Idempotent for already-cancelled charges. Stages an audit row
    (entity ``billing_charge``, action ``cancel``) on the session; the caller commits.
    """
    locked = (
        db.query(BillingCharge).filter(BillingCharge.id == charge.id).with_for_update().first()
    ) or charge
    if locked.status == BillingChargeStatus.cancelled:
        return ChargeReversalResult(charge=locked, already_cancelled=True)

    if performed and not allow_performed_adjustment and float(locked.net_amount or 0) > MONEY_EPS:
        raise ValidationError(
            "This service has already been performed, so its charge cannot simply be cancelled. "
            "Record a refund or an authorized billing adjustment instead.",
            code="CHARGE_SERVICE_PERFORMED",
            details={"charge_id": str(locked.id)},
        )

    result = release_charge_allocations(db, locked, amount=None, reason=reason, actor=actor)
    locked.status = BillingChargeStatus.cancelled
    locked.amount_paid = 0.0
    db.flush()
    _refresh_invoices_if_invoiced(db, locked)

    summary = f"Cancelled charge {locked.id} ({(locked.description or '')[:40]})"
    if result.amount_to_credit > 0:
        summary += f"; ₹{result.amount_to_credit:.2f} moved to cancellation credit"
    if result.amount_restored_to_deposits > 0:
        summary += f"; ₹{result.amount_restored_to_deposits:.2f} returned to deposit"
    write_audit_log(
        db,
        hospital_id=locked.hospital_id,
        actor=actor or SYSTEM_ACTOR,
        action="cancel",
        entity_type="billing_charge",
        entity_id=locked.id,
        summary=summary,
        details=_audit_details(db, locked, result, reason),
    )
    if result.credit_deposit_id:
        write_audit_log(
            db,
            hospital_id=locked.hospital_id,
            actor=actor or SYSTEM_ACTOR,
            action="create",
            entity_type="billing_deposit",
            entity_id=result.credit_deposit_id,
            summary=f"Cancellation credit ₹{result.amount_to_credit:.2f} from charge {locked.id}",
            details=_audit_details(db, locked, result, reason),
        )
    return result


def cancel_charges_for_source(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
    *,
    reason: str,
    actor: dict[str, Any] | None = None,
    performed: bool = False,
    allow_performed_adjustment: bool = False,
) -> list[ChargeReversalResult]:
    """Cancel every active charge linked to a service source (duplicates included)."""
    charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.source_type == source_type,
            BillingCharge.source_id == source_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .order_by(BillingCharge.created_at.asc())
        .all()
    )
    return [
        cancel_charge(
            db,
            c,
            reason=reason,
            actor=actor,
            performed=performed,
            allow_performed_adjustment=allow_performed_adjustment,
        )
        for c in charges
    ]


def cancel_source_charges_or_400(
    db: Session,
    hospital_id: UUID,
    source_type: BillingSourceType,
    source_id: UUID,
    *,
    reason: str,
    actor: dict[str, Any] | None = None,
    performed: bool = False,
) -> list[ChargeReversalResult]:
    """``cancel_charges_for_source`` for module actions: refusal surfaces as HTTP 400."""
    try:
        return cancel_charges_for_source(
            db,
            hospital_id,
            source_type,
            source_id,
            reason=reason,
            actor=actor,
            performed=performed,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc


def reconcile_charge_after_reprice(
    db: Session,
    charge: BillingCharge,
    *,
    reason: str,
    actor: dict[str, Any] | None = None,
) -> ChargeReversalResult | None:
    """After ``net_amount`` changes, release money applied beyond the new net.

    Keeps ``amount_paid <= net_amount``: the excess is reversed (newest first) into
    cancellation credit / back to its deposit, then status is re-derived. Returns
    None when nothing had to be released.
    """
    if charge.status == BillingChargeStatus.cancelled:
        return None
    recompute_charge_financials(db, charge)
    net = round(float(charge.net_amount or 0), 2)
    applied = round(float(charge.amount_paid or 0), 2) if net > 0 else max(
        0.0, round(sum(rem for _, rem in active_allocations_for_charge(db, charge.id)), 2)
    )
    excess = round(applied - max(0.0, net), 2)
    if excess <= MONEY_EPS:
        _refresh_invoices_if_invoiced(db, charge)
        return None
    result = release_charge_allocations(db, charge, amount=excess, reason=reason, actor=actor)
    _refresh_invoices_if_invoiced(db, charge)
    write_audit_log(
        db,
        hospital_id=charge.hospital_id,
        actor=actor or SYSTEM_ACTOR,
        action="reprice_release",
        entity_type="billing_charge",
        entity_id=charge.id,
        summary=f"Released ₹{result.amount_reversed:.2f} over-applied on re-priced charge {charge.id}",
        details=_audit_details(db, charge, result, reason),
    )
    return result


def is_charge_service_performed(db: Session, charge: BillingCharge) -> bool:
    """Best-effort check whether the service behind a charge was already delivered."""
    if charge.source_id is None:
        return False
    st = charge.source_type
    try:
        # A diagnostic charge is keyed on its order (walk-in) or on the prescription
        # request whose orders fulfil it; any fulfilling order already performed counts.
        if st == BillingSourceType.laboratory:
            from modules.laboratory.entities.lab_entities import LabOrder, LabOrderStatus

            return (
                db.query(LabOrder.id)
                .filter(
                    (LabOrder.id == charge.source_id) | (LabOrder.prescription_request_id == charge.source_id),
                    LabOrder.status.in_([LabOrderStatus.in_progress, LabOrderStatus.completed]),
                )
                .first()
                is not None
            )
        if st == BillingSourceType.radiology:
            from modules.radiology.entities.radiology_entities import (
                RadiologyOrder,
                RadiologyOrderStatus,
            )

            return (
                db.query(RadiologyOrder.id)
                .filter(
                    (RadiologyOrder.id == charge.source_id)
                    | (RadiologyOrder.prescription_request_id == charge.source_id),
                    RadiologyOrder.status.in_([RadiologyOrderStatus.in_progress, RadiologyOrderStatus.completed]),
                )
                .first()
                is not None
            )
        if st == BillingSourceType.ot:
            from modules.ot.entities.ot_entities import OtSurgery, OtSurgeryStatus

            surgery = db.query(OtSurgery).filter(OtSurgery.id == charge.source_id).first()
            return bool(
                surgery and surgery.status in (OtSurgeryStatus.in_progress, OtSurgeryStatus.completed)
            )
        if st == BillingSourceType.consultation:
            from modules.appointments.entities.appointment import Appointment
            from modules.appointments.entities.enums import AppointmentStatus

            appt = db.query(Appointment).filter(Appointment.id == charge.source_id).first()
            return bool(appt and appt.status == AppointmentStatus.completed)
        if st == BillingSourceType.pharmacy:
            from modules.pharmacy.entities.pharmacy_entities import PharmacySale, PharmacySaleStatus

            sale = db.query(PharmacySale).filter(PharmacySale.id == charge.source_id).first()
            return bool(sale and sale.status in (PharmacySaleStatus.completed, PharmacySaleStatus.partially_returned))
    except Exception:
        return False
    return False
