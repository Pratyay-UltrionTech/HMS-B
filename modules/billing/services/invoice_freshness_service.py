"""Invoice freshness validation service.

Verifies that finalized invoices accurately reflect the current financial state
of all billable charges belonging to their episode/financial account.
Blocks discharge / financial clearance if charges have been added, removed, repriced,
or changed in quantity since invoice generation.
"""

from __future__ import annotations

from typing import Tuple
from uuid import UUID
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
)


def verify_account_invoice_freshness(
    db: Session,
    hospital_id: UUID,
    account_id: UUID,
) -> tuple[bool, str | None]:
    """Check if all charges on an account are accurately invoiced across active invoices.

    Returns:
        (is_fresh, reason_if_stale)
    """
    if not account_id:
        return True, None

    # Load all current non-cancelled charges belonging to this financial account
    current_charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.account_id == account_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .all()
    )
    current_charge_by_id = {c.id: c for c in current_charges}

    # Load all active generated/paid invoices for this account
    active_invoices = (
        db.query(BillingInvoice)
        .filter(
            BillingInvoice.hospital_id == hospital_id,
            BillingInvoice.account_id == account_id,
            BillingInvoice.status.in_([BillingInvoiceStatus.generated, BillingInvoiceStatus.paid]),
        )
        .all()
    )
    if not active_invoices:
        return False, "A generated final invoice must exist for the admission account before discharge."

    active_invoice_ids = [inv.id for inv in active_invoices]

    # Load all lines across these active invoices
    invoice_lines = (
        db.query(BillingInvoiceLine)
        .filter(
            BillingInvoiceLine.hospital_id == hospital_id,
            BillingInvoiceLine.invoice_id.in_(active_invoice_ids),
        )
        .all()
    )
    lines_by_charge_id = {line.charge_id: line for line in invoice_lines if line.charge_id}

    # 1. Detect newly added or un-invoiced charges on this account
    for c in current_charges:
        if c.id not in lines_by_charge_id:
            return False, f"New billable charge '{c.description}' was added after invoice generation."

    # 2. Detect charges that were cancelled after the invoice was generated
    for line in invoice_lines:
        if line.charge_id and line.charge_id not in current_charge_by_id:
            return False, f"Charge '{line.description}' was cancelled after invoice generation."

    # 3. Detect repriced charges, quantity changes, or net amount changes
    for c in current_charges:
        line = lines_by_charge_id.get(c.id)
        if not line:
            continue

        c_qty = float(c.quantity if getattr(c, "quantity", None) is not None else 1.0)
        l_qty = float(line.quantity if getattr(line, "quantity", None) is not None else 1.0)
        if abs(c_qty - l_qty) > 0.005:
            return False, f"Charge '{c.description}' quantity changed from {l_qty} to {c_qty}."

        c_rate = float(c.unit_price if getattr(c, "unit_price", None) not in (None, 0.0) else (c.charge_amount or 0))
        l_rate = float(line.rate or 0.0)
        if abs(round(c_rate, 2) - round(l_rate, 2)) > 0.01:
            return False, f"Charge '{c.description}' rate changed from ₹{l_rate:.2f} to ₹{c_rate:.2f}."

        c_net = float(c.net_amount or 0.0)
        l_amt = float(line.amount or 0.0)
        if abs(round(c_net, 2) - round(l_amt, 2)) > 0.01:
            return False, f"Charge '{c.description}' amount changed from ₹{l_amt:.2f} to ₹{c_net:.2f}."

    return True, None


def verify_invoice_freshness(
    db: Session,
    hospital_id: UUID,
    invoice: BillingInvoice,
) -> tuple[bool, str | None]:
    """Backwards-compatible wrapper delegating to verify_account_invoice_freshness."""
    if not invoice or not invoice.account_id:
        return True, None
    return verify_account_invoice_freshness(db, hospital_id, invoice.account_id)


def check_invoice_reissue_eligibility(
    db: Session,
    hospital_id: UUID,
    invoice: BillingInvoice,
) -> tuple[bool, str | None]:
    """Check if an invoice document is stale and eligible for reissue.

    An invoice is reissue-eligible if and only if its own snapshot is no longer accurate
    due to underlying charges being cancelled, reversed, repriced, or changed in quantity/rate.
    A fresh/current invoice is NOT reissue-eligible (cannot be casually replaced).

    Returns:
        (is_eligible, reason_if_stale)
    """
    if not invoice or invoice.status == BillingInvoiceStatus.cancelled:
        return False, "Cancelled invoices cannot be reissued."

    lines = [ln for ln in (invoice.lines or []) if ln.charge_id]
    if not lines:
        return False, "Invoice has no linked charges to reissue."

    charge_ids = [ln.charge_id for ln in lines]
    charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.id.in_(charge_ids),
        )
        .all()
    )
    charge_map = {c.id: c for c in charges}

    # 1. Detect if any charge was cancelled, reversed, or deleted
    for ln in lines:
        c = charge_map.get(ln.charge_id)
        if not c or c.status == BillingChargeStatus.cancelled:
            return True, f"Charge '{ln.description}' was cancelled or reversed."

    # 2. Detect if any charge was repriced, quantity changed, or rate changed
    for ln in lines:
        c = charge_map[ln.charge_id]
        c_qty = float(c.quantity if getattr(c, "quantity", None) is not None else 1.0)
        l_qty = float(ln.quantity if getattr(ln, "quantity", None) is not None else 1.0)
        if abs(c_qty - l_qty) > 0.005:
            return True, f"Charge '{c.description}' quantity changed from {l_qty:g} to {c_qty:g}."

        c_rate = float(c.unit_price if getattr(c, "unit_price", None) not in (None, 0.0) else (c.charge_amount or 0.0))
        l_rate = float(ln.rate or 0.0)
        if abs(round(c_rate, 2) - round(l_rate, 2)) > 0.01:
            return True, f"Charge '{c.description}' rate changed from ₹{l_rate:.2f} to ₹{c_rate:.2f}."

        c_net = float(c.net_amount or 0.0)
        l_amt = float(ln.amount or 0.0)
        if abs(round(c_net, 2) - round(l_amt, 2)) > 0.01:
            return True, f"Charge '{c.description}' amount changed from ₹{l_amt:.2f} to ₹{c_net:.2f}."

    return False, None

