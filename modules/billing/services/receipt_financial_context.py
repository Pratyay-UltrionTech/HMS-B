"""
Authoritative Receipt Financial Context Service.

Calculates canonical pre-payment and post-payment balances, encounter remaining balance,
and global patient outstanding for payment receipts according to Billing ledger invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingPayment,
    BillingPaymentAllocation,
    BillingReceipt,
    FinancialAccount,
)
from modules.billing.services.billing_service import patient_ledger_totals


@dataclass
class ReceiptFinancialContext:
    payment_timestamp: datetime
    amount_received: float
    amount_allocated: float
    account_id: UUID | None
    account_number: str | None
    encounter_identifier: str | None
    account_balance_before_payment: float | None
    account_balance_after_payment: float
    patient_overall_outstanding_after_payment: float
    available_deposit_balance_after_payment: float
    net_patient_balance_after_deposits: float
    is_legacy_estimate: bool = False


def _calculate_financial_context(
    db: Session,
    hospital_id: UUID,
    receipt: BillingReceipt,
) -> ReceiptFinancialContext:
    """Internal calculation of pre-payment, post-payment, and global balances (READ-ONLY)."""
    payment: BillingPayment | None = None
    if receipt.payment_id:
        payment = (
            db.query(BillingPayment)
            .filter(
                BillingPayment.hospital_id == hospital_id,
                BillingPayment.id == receipt.payment_id,
            )
            .first()
        )

    # 1. Authoritative timestamp
    ts: datetime
    if payment and payment.created_at:
        ts = payment.created_at
    elif receipt.created_at:
        ts = receipt.created_at
    elif receipt.payment_date:
        ts = datetime.combine(receipt.payment_date, datetime.min.time())
    else:
        ts = datetime.now()

    amount_received = float(receipt.amount)

    # 2. Allocations & targeted account / charges
    allocations: list[BillingPaymentAllocation] = []
    if payment:
        allocations = (
            db.query(BillingPaymentAllocation)
            .filter(
                BillingPaymentAllocation.hospital_id == hospital_id,
                BillingPaymentAllocation.payment_id == payment.id,
            )
            .all()
        )

    # Calculate net effective allocation for this payment
    reversed_map: dict[UUID, float] = {}
    for alloc in allocations:
        if alloc.reversal_of_id:
            reversed_map[alloc.reversal_of_id] = (
                reversed_map.get(alloc.reversal_of_id, 0.0) + float(alloc.allocated_amount or 0.0)
            )

    charge_allocated: dict[UUID, float] = {}
    total_effective_allocated = 0.0
    for alloc in allocations:
        if alloc.reversal_of_id is None and float(alloc.allocated_amount or 0.0) > 0:
            eff = round(float(alloc.allocated_amount) + reversed_map.get(alloc.id, 0.0), 2)
            if eff > 0:
                charge_allocated[alloc.charge_id] = charge_allocated.get(alloc.charge_id, 0.0) + eff
                total_effective_allocated += eff

    # 3. Resolve Account or Encounter
    target_account: FinancialAccount | None = None
    if payment and payment.account_id:
        target_account = (
            db.query(FinancialAccount)
            .filter(
                FinancialAccount.hospital_id == hospital_id,
                FinancialAccount.id == payment.account_id,
            )
            .first()
        )

    charges: list[BillingCharge] = []
    if charge_allocated:
        charges = (
            db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.id.in_(list(charge_allocated.keys())),
            )
            .all()
        )

    if not target_account and charges:
        # Check if charges share an account
        acc_ids = {c.account_id for c in charges if c.account_id}
        if len(acc_ids) == 1:
            target_account = (
                db.query(FinancialAccount)
                .filter(
                    FinancialAccount.hospital_id == hospital_id,
                    FinancialAccount.id == list(acc_ids)[0],
                )
                .first()
            )

    account_id: UUID | None = target_account.id if target_account else None
    account_number: str | None = target_account.account_number if target_account else None
    encounter_identifier: str | None = None

    if target_account:
        encounter_identifier = f"Account #{target_account.account_number}"
    elif charges:
        for c in charges:
            if c.source_type and getattr(c.source_type, "value", str(c.source_type)) == "consultation" and c.source_id:
                try:
                    from modules.appointments.entities.appointment import Appointment
                    appt = db.query(Appointment).filter(Appointment.id == c.source_id).first()
                    if appt and appt.op_id:
                        encounter_identifier = f"OP Token {appt.op_id}"
                        break
                except Exception:
                    pass
        if not encounter_identifier and charges:
            encounter_identifier = "Direct Services"

    # 4. Account balance after and before payment
    account_balance_after = 0.0
    account_balance_before: float | None = None

    if target_account:
        acc_ledger = patient_ledger_totals(
            db,
            hospital_id,
            receipt.patient_id,
            account_id=target_account.id,
        )
        account_balance_after = float(acc_ledger["outstanding"])
        account_balance_before = round(account_balance_after + total_effective_allocated, 2)
    elif charges:
        # Balance of the specific charges touched by this payment
        account_balance_after = round(
            sum(max(0.0, float(c.net_amount or 0.0) - float(c.amount_paid or 0.0)) for c in charges),
            2,
        )
        account_balance_before = round(account_balance_after + total_effective_allocated, 2)
    else:
        account_balance_after = 0.0
        account_balance_before = 0.0

    # 5. Global patient balances after payment
    global_ledger = patient_ledger_totals(db, hospital_id, receipt.patient_id)
    patient_overall_outstanding_after = float(global_ledger["outstanding"])
    available_deposit_balance_after = float(global_ledger["total_deposits_available"])
    net_patient_balance_after_deposits = float(global_ledger["net_patient_balance"])

    return ReceiptFinancialContext(
        payment_timestamp=ts,
        amount_received=amount_received,
        amount_allocated=total_effective_allocated,
        account_id=account_id,
        account_number=account_number,
        encounter_identifier=encounter_identifier,
        account_balance_before_payment=account_balance_before,
        account_balance_after_payment=account_balance_after,
        patient_overall_outstanding_after_payment=patient_overall_outstanding_after,
        available_deposit_balance_after_payment=available_deposit_balance_after,
        net_patient_balance_after_deposits=net_patient_balance_after_deposits,
        is_legacy_estimate=False,
    )


def build_receipt_financial_context(
    db: Session,
    hospital_id: UUID,
    receipt: BillingReceipt,
) -> ReceiptFinancialContext:
    """Build canonical financial context for a billing receipt (STRICTLY READ-ONLY).

    NEVER modifies BillingReceipt snapshot columns or writes to the database.
    - If historical snapshot exists on the receipt, returns stored snapshot values directly.
    - If historical snapshot is NULL (legacy receipt), derives current balances for display only
      and marks is_legacy_estimate=True without persisting anything to the database.
    """
    amount_received = float(receipt.amount)
    payment: BillingPayment | None = None
    if receipt.payment_id:
        payment = (
            db.query(BillingPayment)
            .filter(
                BillingPayment.hospital_id == hospital_id,
                BillingPayment.id == receipt.payment_id,
            )
            .first()
        )

    ts: datetime
    if payment and payment.created_at:
        ts = payment.created_at
    elif receipt.created_at:
        ts = receipt.created_at
    elif receipt.payment_date:
        ts = datetime.combine(receipt.payment_date, datetime.min.time())
    else:
        ts = datetime.now()

    # 1. Fast path: Read from immutable snapshots if already persisted on the receipt
    if receipt.account_balance_after_snapshot is not None:
        target_account: FinancialAccount | None = None
        if receipt.account_id_snapshot:
            target_account = (
                db.query(FinancialAccount)
                .filter(
                    FinancialAccount.hospital_id == hospital_id,
                    FinancialAccount.id == receipt.account_id_snapshot,
                )
                .first()
            )
        account_number = target_account.account_number if target_account else None
        encounter_identifier = f"Account #{account_number}" if account_number else "This Encounter / Account"
        return ReceiptFinancialContext(
            payment_timestamp=ts,
            amount_received=amount_received,
            amount_allocated=float(receipt.amount_allocated_snapshot or 0.0),
            account_id=receipt.account_id_snapshot,
            account_number=account_number,
            encounter_identifier=encounter_identifier,
            account_balance_before_payment=float(receipt.account_balance_before_snapshot) if receipt.account_balance_before_snapshot is not None else None,
            account_balance_after_payment=float(receipt.account_balance_after_snapshot),
            patient_overall_outstanding_after_payment=float(receipt.patient_outstanding_after_snapshot or 0.0),
            available_deposit_balance_after_payment=float(receipt.available_deposit_after_snapshot or 0.0),
            net_patient_balance_after_deposits=float(receipt.net_patient_balance_after_snapshot or 0.0),
            is_legacy_estimate=False,
        )

    # 2. Legacy receipt without stored snapshot: compute current financial state for display only
    # MUST NOT WRITE OR FLUSH OR COMMIT ANYTHING
    ctx = _calculate_financial_context(db, hospital_id, receipt)
    ctx.is_legacy_estimate = True
    return ctx


def compute_receipt_snapshots(
    db: Session,
    hospital_id: UUID,
    receipt: BillingReceipt,
) -> ReceiptFinancialContext:
    """Computes and populates immutable snapshot fields on a NEW receipt upon issuance.

    Must ONLY be called during receipt creation/issuance before transaction commit.
    Once written, these snapshot values represent the immutable point-in-time financial truth.
    """
    if receipt.account_balance_after_snapshot is not None:
        # Already has snapshots; do not overwrite historical data
        return build_receipt_financial_context(db, hospital_id, receipt)

    ctx = _calculate_financial_context(db, hospital_id, receipt)
    receipt.account_id_snapshot = ctx.account_id
    receipt.amount_allocated_snapshot = Decimal(str(round(ctx.amount_allocated, 2)))
    receipt.account_balance_before_snapshot = (
        Decimal(str(round(ctx.account_balance_before_payment, 2)))
        if ctx.account_balance_before_payment is not None
        else None
    )
    receipt.account_balance_after_snapshot = Decimal(str(round(ctx.account_balance_after_payment, 2)))
    receipt.patient_outstanding_after_snapshot = Decimal(str(round(ctx.patient_overall_outstanding_after_payment, 2)))
    receipt.available_deposit_after_snapshot = Decimal(str(round(ctx.available_deposit_balance_after_payment, 2)))
    receipt.net_patient_balance_after_snapshot = Decimal(str(round(ctx.net_patient_balance_after_deposits, 2)))
    db.flush()
    return ctx
