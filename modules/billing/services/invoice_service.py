"""Invoice and receipt numbering sequences, line snapshots, and HTML rendering service.

Conforms to UltrionTech-Backend-Template modules/billing/services/ specification.
Replaces app.utils.invoices with target-native business logic and entities.
"""

from __future__ import annotations

from datetime import date
import re
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingReceipt,
    BillingReceiptStatus,
    BillingRefund,
    DepositStatus,
    RefundStatus,
)
from modules.billing.services.billing_service import patient_ledger_totals
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital

SOURCE_CATEGORY_LABELS: dict[str, str] = {
    "consultation": "Consultation Charges",
    "laboratory": "Laboratory Charges",
    "radiology": "Radiology Charges",
    "admission": "Admission Charges",
    "bed": "Bed Charges",
    "ot": "OT Charges",
    "pharmacy": "Pharmacy Charges",
    "other": "Manual Charges",
    "adjustment": "Manual Charges",
}


def _esc(s: str | None, default: str = "—") -> str:
    if s is None or s == "":
        return default
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _clean_service_display(value: str | None) -> str:
    """Fix duplicated clinician prefixes in generated labels without rewriting descriptions."""
    text = value or "—"
    return re.sub(r"(^|[-–—:]\s*)(?:Dr\.?\s*){2,}", r"\1Dr. ", text, flags=re.IGNORECASE)


def _max_existing_seq(db: Session, column, hospital_id: UUID, prefix: str) -> int:
    """Return the highest numeric suffix already stored for ``prefix``.

    Used once per number generation to self-heal atomic counters that were
    introduced after rows numbered with the legacy read-MAX-then-+1 scheme
    already existed (e.g. RCPT-2026-00001 predating the counter row).
    """
    max_seq = 0
    for (num,) in (
        db.query(column)
        .filter(
            BillingInvoice.hospital_id == hospital_id
            if column is BillingInvoice.invoice_number
            else BillingReceipt.hospital_id == hospital_id,
            column.like(f"{prefix}%"),
        )
        .all()
    ):
        try:
            max_seq = max(max_seq, int(str(num).split("-")[-1]))
        except (ValueError, IndexError):
            continue
    return max_seq


def next_invoice_number(db: Session, hospital_id: UUID, year: int | None = None) -> str:
    """Generate sequential invoice number scoped to hospital and year atomically."""
    from shared.database.sequences import (
        counter_exists as _exists,
        ensure_counter_at_least as _ensure,
        next_invoice_number as _atomic_next_inv,
    )
    y = year or date.today().year
    counter_name = f"invoice_{y}"
    if not _exists(db, hospital_id, counter_name):
        _ensure(db, hospital_id, counter_name, _max_existing_seq(
            db, BillingInvoice.invoice_number, hospital_id, f"INV-{y}-"))
    return _atomic_next_inv(db, hospital_id, year)


def next_receipt_number(db: Session, hospital_id: UUID, year: int | None = None) -> str:
    """Generate sequential payment receipt number scoped to hospital and year atomically."""
    from shared.database.sequences import (
        counter_exists as _exists,
        ensure_counter_at_least as _ensure,
        next_receipt_number as _atomic_next_rcpt,
    )
    y = year or date.today().year
    counter_name = f"receipt_{y}"
    if not _exists(db, hospital_id, counter_name):
        _ensure(db, hospital_id, counter_name, _max_existing_seq(
            db, BillingReceipt.receipt_number, hospital_id, f"RCPT-{y}-"))
    return _atomic_next_rcpt(db, hospital_id, year)



def charges_already_invoiced(
    db: Session, hospital_id: UUID, charge_ids: list[UUID]
) -> list[UUID]:
    """Find charge IDs that already exist on a non-cancelled invoice."""
    if not charge_ids:
        return []
    rows = (
        db.query(BillingInvoiceLine.charge_id)
        .join(BillingInvoice, BillingInvoice.id == BillingInvoiceLine.invoice_id)
        .filter(
            BillingInvoiceLine.hospital_id == hospital_id,
            BillingInvoiceLine.charge_id.in_(charge_ids),
            BillingInvoice.status != BillingInvoiceStatus.cancelled,
        )
        .all()
    )
    return [r[0] for r in rows if r[0]]


def refresh_invoice_paid_status(
    db: Session, hospital_id: UUID, patient_id: UUID
) -> None:
    """Re-evaluate generated/paid invoices from the current state of their charges.

    generated → paid when every linked charge is settled (paid or cancelled) and
    at least one is paid; paid → generated when any linked charge is open again
    (refund / allocation reversal). Invoice lines are snapshots and never mutated.
    """
    db.flush()
    invoices = (
        db.query(BillingInvoice)
        .options(joinedload(BillingInvoice.lines))
        .filter(
            BillingInvoice.hospital_id == hospital_id,
            BillingInvoice.patient_id == patient_id,
            BillingInvoice.status.in_(
                [BillingInvoiceStatus.generated, BillingInvoiceStatus.paid]
            ),
        )
        .all()
    )
    all_charge_ids = {
        ln.charge_id for inv in invoices for ln in inv.lines if ln.charge_id
    }
    status_by_charge: dict = {}
    if all_charge_ids:
        status_by_charge = {
            row[0]: row[1]
            for row in db.query(BillingCharge.id, BillingCharge.status)
            .filter(BillingCharge.id.in_(all_charge_ids))
            .all()
        }
    open_states = {BillingChargeStatus.pending, BillingChargeStatus.partially_paid}
    for inv in invoices:
        charge_ids = [ln.charge_id for ln in inv.lines if ln.charge_id]
        if not charge_ids:
            continue
        states = [status_by_charge.get(cid) for cid in charge_ids]
        if any(s in open_states for s in states):
            if inv.status == BillingInvoiceStatus.paid:
                inv.status = BillingInvoiceStatus.generated
        elif any(s == BillingChargeStatus.paid for s in states):
            inv.status = BillingInvoiceStatus.paid


def create_invoice_from_charges(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    charge_ids: list[UUID],
    account_id: UUID | None = None,
    invoice_date: date | None = None,
    tax_amount: float = 0.0,
    cgst_amount: float = 0.0,
    sgst_amount: float = 0.0,
    igst_amount: float = 0.0,
    notes: str | None = None,
    created_by_name: str = "",
) -> BillingInvoice:
    """Snapshot selected charges into a finalized formal invoice with statutory GST breakdown."""
    if not charge_ids:
        raise ValueError("Select at least one charge")

    unique_ids = list(dict.fromkeys(charge_ids))
    already = charges_already_invoiced(db, hospital_id, unique_ids)
    if already:
        raise ValueError("One or more charges are already on an active invoice")

    charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.patient_id == patient_id,
            BillingCharge.id.in_(unique_ids),
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .order_by(BillingCharge.created_at.asc())
        .all()
    )
    if len(charges) != len(unique_ids):
        raise ValueError("One or more charges were not found for this patient")

    subtotal = round(sum(float(c.charge_amount or 0) for c in charges), 2)
    discount_amount = round(sum(float(c.discount_amount or 0) for c in charges), 2)
    taxable_amount = round(max(0.0, subtotal - discount_amount), 2)

    # Calculate tax components if not explicitly provided
    calc_tax = round(sum(float(c.tax_amount or 0) for c in charges), 2)
    final_tax = round(max(float(tax_amount or 0), calc_tax), 2)

    c_gst = round(float(cgst_amount or 0), 2)
    s_gst = round(float(sgst_amount or 0), 2)
    i_gst = round(float(igst_amount or 0), 2)

    if final_tax > 0 and (c_gst + s_gst + i_gst) == 0:
        # Default to intra-state split (CGST 50% + SGST 50%)
        c_gst = round(final_tax / 2.0, 2)
        s_gst = round(final_tax - c_gst, 2)

    net_sum = round(sum(float(c.net_amount or 0) for c in charges), 2)
    grand_total = round(taxable_amount + final_tax, 2)

    resolved_account_id = account_id or charges[0].account_id if charges else None

    inv = BillingInvoice(
        hospital_id=hospital_id,
        patient_id=patient_id,
        account_id=resolved_account_id,
        invoice_number=next_invoice_number(db, hospital_id),
        invoice_date=invoice_date or date.today(),
        subtotal=subtotal,
        discount_amount=discount_amount,
        taxable_amount=taxable_amount,
        tax_amount=final_tax,
        cgst_amount=c_gst,
        sgst_amount=s_gst,
        igst_amount=i_gst,
        grand_total=grand_total,
        status=BillingInvoiceStatus.generated,
        notes=notes,
        created_by_name=created_by_name or "Staff",
    )
    db.add(inv)
    db.flush()

    for idx, c in enumerate(charges):
        src = c.source_type.value if c.source_type else "other"
        qty = float(c.quantity if getattr(c, "quantity", None) is not None else 1.0)
        rate = float(c.unit_price if getattr(c, "unit_price", None) not in (None, 0.0) else (c.charge_amount or 0))
        hsn = getattr(c, "hsn_sac_code", None)
        rate_gst = float(getattr(c, "gst_rate", None) or 0.0)
        c_tax = float(getattr(c, "tax_amount", None) or 0.0)
        c_cgst = round(c_tax / 2.0, 2) if c_tax > 0 else 0.0
        c_sgst = round(c_tax - c_cgst, 2) if c_tax > 0 else 0.0

        db.add(
            BillingInvoiceLine(
                hospital_id=hospital_id,
                invoice_id=inv.id,
                charge_id=c.id,
                source_type=src,
                description=c.description[:512],
                quantity=qty,
                rate=round(rate, 2),
                amount=round(float(c.net_amount or 0), 2),
                hsn_sac_code=hsn,
                gst_rate=rate_gst,
                cgst_amount=c_cgst,
                sgst_amount=c_sgst,
                sort_order=idx,
            )
        )
    db.flush()

    if all(c.status == BillingChargeStatus.paid for c in charges):
        inv.status = BillingInvoiceStatus.paid

    return inv


def issue_receipt(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    amount: float,
    payment_date: date,
    payment_method: BillingPaymentMethod,
    collected_by_name: str = "",
    reference_number: str | None = None,
    notes: str | None = None,
    payment_id: UUID | None = None,
    linked_invoice_id: UUID | None = None,
) -> BillingReceipt:
    """Create receipt record for payment."""
    if amount <= 0:
        raise ValueError("Receipt amount must be greater than zero")
    if linked_invoice_id:
        inv = (
            db.query(BillingInvoice)
            .filter(
                BillingInvoice.id == linked_invoice_id,
                BillingInvoice.hospital_id == hospital_id,
                BillingInvoice.patient_id == patient_id,
                BillingInvoice.status != BillingInvoiceStatus.cancelled,
            )
            .first()
        )
        if not inv:
            raise ValueError("Linked invoice not found")

    receipt = BillingReceipt(
        hospital_id=hospital_id,
        patient_id=patient_id,
        payment_id=payment_id,
        linked_invoice_id=linked_invoice_id,
        receipt_number=next_receipt_number(db, hospital_id),
        payment_date=payment_date,
        payment_method=payment_method,
        amount=round(float(amount), 2),
        reference_number=reference_number,
        notes=notes,
        status=BillingReceiptStatus.issued,
        collected_by_name=collected_by_name or "Staff",
    )
    db.add(receipt)
    db.flush()
    return receipt


def issue_receipt_for_payment(
    db: Session,
    payment: BillingPayment,
    *,
    linked_invoice_id: UUID | None = None,
    reference_number: str | None = None,
) -> BillingReceipt:
    """Issue or retrieve existing receipt for a payment."""
    existing = (
        db.query(BillingReceipt)
        .filter(
            BillingReceipt.hospital_id == payment.hospital_id,
            BillingReceipt.payment_id == payment.id,
            BillingReceipt.status != BillingReceiptStatus.cancelled,
        )
        .first()
    )
    if existing:
        return existing
    return issue_receipt(
        db,
        hospital_id=payment.hospital_id,
        patient_id=payment.patient_id,
        amount=float(payment.amount),
        payment_date=payment.payment_date,
        payment_method=payment.payment_method,
        collected_by_name=payment.received_by_name or "Staff",
        reference_number=reference_number,
        notes=payment.notes,
        payment_id=payment.id,
        linked_invoice_id=linked_invoice_id,
    )


def invoice_to_dict(inv: BillingInvoice, patient: Patient | None = None, *, include_lines: bool = True) -> dict:
    """
    Serialize BillingInvoice ORM model to dictionary with patient context.
    """
    p = patient or inv.patient
    lines = sorted(inv.lines or [], key=lambda ln: ln.sort_order) if include_lines else []
    return {
        "id": inv.id,
        "hospital_id": inv.hospital_id,
        "patient_id": inv.patient_id,
        "account_id": getattr(inv, "account_id", None),
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date,
        "subtotal": inv.subtotal,
        "discount_amount": inv.discount_amount,
        "taxable_amount": getattr(inv, "taxable_amount", 0.0) or 0.0,
        "tax_amount": inv.tax_amount,
        "cgst_amount": getattr(inv, "cgst_amount", 0.0) or 0.0,
        "sgst_amount": getattr(inv, "sgst_amount", 0.0) or 0.0,
        "igst_amount": getattr(inv, "igst_amount", 0.0) or 0.0,
        "grand_total": inv.grand_total,
        "status": inv.status,
        "notes": inv.notes,
        "created_by_name": inv.created_by_name,
        "created_at": inv.created_at,
        "updated_at": inv.updated_at,
        "patient_name": p.name if p else None,
        "patient_uhid": p.uhid if p else None,
        "lines": [
            {
                "id": ln.id,
                "charge_id": ln.charge_id,
                "source_type": ln.source_type,
                "category_label": SOURCE_CATEGORY_LABELS.get(
                    ln.source_type, "Manual Charges"
                ),
                "description": ln.description,
                "quantity": ln.quantity,
                "rate": ln.rate,
                "amount": ln.amount,
                "hsn_sac_code": getattr(ln, "hsn_sac_code", None),
                "gst_rate": getattr(ln, "gst_rate", 0.0) or 0.0,
                "cgst_amount": getattr(ln, "cgst_amount", 0.0) or 0.0,
                "sgst_amount": getattr(ln, "sgst_amount", 0.0) or 0.0,
                "sort_order": ln.sort_order,
            }
            for ln in lines
        ],
    }


def receipt_to_dict(r: BillingReceipt, patient: Patient | None = None) -> dict:
    """Serialize BillingReceipt ORM model to dictionary with patient context."""
    p = patient or r.patient
    return {
        "id": r.id,
        "hospital_id": r.hospital_id,
        "patient_id": r.patient_id,
        "payment_id": r.payment_id,
        "deposit_id": getattr(r, "deposit_id", None),
        "linked_invoice_id": r.linked_invoice_id,
        "receipt_number": r.receipt_number,
        "payment_date": r.payment_date,
        "payment_method": r.payment_method,
        "amount": r.amount,
        "reference_number": r.reference_number,
        "notes": r.notes,
        "status": r.status,
        "collected_by_name": r.collected_by_name,
        "created_at": r.created_at,
        "patient_name": p.name if p else None,
        "patient_uhid": p.uhid if p else None,
    }


def amount_to_words_inr(amount: float | int) -> str:
    """Convert a numeric INR amount into words according to the Indian numbering system.

    Example: 1250.50 -> 'Rupees One Thousand Two Hundred Fifty and Fifty Paise Only'
    """
    try:
        amt = round(float(amount), 2)
    except (ValueError, TypeError):
        return "Rupees Zero Only"

    if amt < 0:
        return "Minus " + amount_to_words_inr(abs(amt))
    if amt == 0:
        return "Rupees Zero Only"

    rupees = int(amt)
    paise = int(round((amt - rupees) * 100))

    ones = [
        "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen"
    ]
    tens = [
        "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"
    ]

    def _two_digits(n: int) -> str:
        if n == 0:
            return ""
        if n < 20:
            return ones[n]
        t = tens[n // 10]
        o = ones[n % 10]
        return f"{t} {o}".strip() if o else t

    def _three_digits(n: int) -> str:
        h = n // 100
        rem = n % 100
        parts = []
        if h > 0:
            parts.append(f"{ones[h]} Hundred")
        if rem > 0:
            parts.append(_two_digits(rem))
        return " ".join(parts)

    def _convert_integer(n: int) -> str:
        if n == 0:
            return "Zero"
        crore = n // 10000000
        n %= 10000000
        lakh = n // 100000
        n %= 100000
        thousand = n // 1000
        n %= 1000
        remainder = n

        chunks = []
        if crore > 0:
            chunks.append(f"{_convert_integer(crore)} Crore")
        if lakh > 0:
            chunks.append(f"{_two_digits(lakh)} Lakh")
        if thousand > 0:
            chunks.append(f"{_two_digits(thousand)} Thousand")
        if remainder > 0:
            chunks.append(_three_digits(remainder))
        return " ".join(chunks)

    rupee_words = _convert_integer(rupees)
    if paise > 0:
        paise_words = _two_digits(paise)
        return f"Rupees {rupee_words} and {paise_words} Paise Only"
    return f"Rupees {rupee_words} Only"


def _shared_doc_styles() -> str:
    """Shared modern hospital financial document design system for screen and A4 printing.

    Features calm hospital-grade aesthetics, crisp typography, clean data hierarchy,
    subtle dividers, zero table clutter, robust print stylesheets, and B&W print compatibility.
    """
    return """
  :root {
    --space-xs: 4px;
    --space-sm: 8px;
    --space-md: 14px;
    --space-lg: 22px;
    --space-xl: 32px;
  }
  @page {
    size: A4 portrait;
    margin: 12mm 14mm 14mm 14mm;
  }
  *, *::before, *::after {
    box-sizing: border-box;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    color: #0f172a;
    background: #f8fafc;
    margin: 0;
    padding: 24px 16px;
    font-size: 11px;
    line-height: 1.45;
    -webkit-font-smoothing: antialiased;
  }
  .doc-container {
    max-width: 820px;
    margin: 0 auto;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 0;
    box-shadow: none;
    padding: 36px 40px;
  }
  .receipt-document {
    max-width: 780px;
  }

  /* ── Hospital Header ─────────────────────────────────────────── */
  .hosp-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 24px;
    border-bottom: 1.5px solid #0f172a;
    padding-bottom: 18px;
    margin-bottom: var(--space-xl);
  }
  .hosp-brand {
    display: flex;
    align-items: flex-start;
    gap: 14px;
    flex: 1;
    min-width: 0;
  }
  .hospital-logo {
    max-height: 52px;
    max-width: 84px;
    object-fit: contain;
  }
  .hospital-mark {
    width: 42px;
    height: 42px;
    border-radius: 0;
    background: #0f172a;
    color: #ffffff;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .hospital-mark svg {
    width: 22px;
    height: 22px;
  }
  .hosp-identity {
    flex: 1;
    min-width: 0;
  }
  .hosp-title {
    font-size: 19px;
    font-weight: 700;
    color: #0f172a;
    line-height: 1.25;
    letter-spacing: -0.01em;
    margin: 0 0 4px;
  }
  .hosp-meta {
    font-size: 10px;
    color: #475569;
    margin: 2px 0;
    line-height: 1.4;
  }
  .hosp-tax-line {
    font-size: 9.5px;
    color: #475569;
    margin-top: 5px;
    line-height: 1.4;
    font-weight: 500;
  }

  /* ── Header Document Meta ────────────────────────────────────── */
  .doc-header-meta {
    text-align: right;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    flex-shrink: 0;
  }
  .doc-category-label {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #64748b;
    margin-bottom: 2px;
  }
  .doc-main-title {
    font-size: 18px;
    font-weight: 800;
    color: #0f172a;
    margin: 0;
    letter-spacing: -0.01em;
    text-transform: uppercase;
  }
  .doc-ref-number {
    font-size: 13.5px;
    font-weight: 700;
    color: #1e293b;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    margin-top: 4px;
  }
  .doc-date-text {
    font-size: 10.5px;
    color: #64748b;
    margin-top: 2px;
  }
  .doc-status-wrap {
    margin-top: 6px;
  }

  /* ── Status Text & Rectangular Tags (No Pills) ───────────────── */
  .status-tag, .status-badge {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 0;
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border: 1px solid currentColor;
    background: transparent;
  }
  .status-paid, .status-issued {
    color: #15803d;
    border-color: #86efac;
    background: #f0fdf4;
  }
  .status-partial {
    color: #b45309;
    border-color: #fde68a;
    background: #fffbeb;
  }
  .status-unpaid {
    color: #b91c1c;
    border-color: #fca5a5;
    background: #fef2f2;
  }
  .status-void, .status-cancelled {
    color: #475569;
    border-color: #cbd5e1;
    background: #f8fafc;
  }
  .status-refunded {
    color: #991b1b;
    border-color: #fca5a5;
    background: #fef2f2;
  }

  /* ── Typographic Metadata Columns (No Boxes) ─────────────────── */
  .meta-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    column-gap: 40px;
    margin-bottom: var(--space-xl);
    padding-bottom: var(--space-lg);
    border-bottom: 1px solid #e2e8f0;
  }
  .meta-col, .meta-card {
    background: transparent;
    border: none;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .meta-col-header, .meta-card-title {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #334155;
    margin-bottom: var(--space-sm);
    padding-bottom: 4px;
    border-bottom: 1px solid #cbd5e1;
  }
  .meta-row {
    display: grid;
    grid-template-columns: 105px 1fr;
    column-gap: 8px;
    padding: 2.5px 0;
    font-size: 11px;
    align-items: baseline;
  }
  .meta-label {
    color: #64748b;
    font-weight: 500;
    font-size: 10.5px;
  }
  .meta-val {
    color: #0f172a;
    font-weight: 600;
    word-break: break-word;
  }
  .meta-val-primary {
    font-size: 12.5px;
    font-weight: 700;
    color: #0f172a;
  }

  /* ── Itemized Services Table (Invoice) ────────────────────────── */
  table.items-table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    margin: var(--space-lg) 0 var(--space-xl);
    table-layout: fixed;
  }
  table.items-table thead th {
    background: #f8fafc;
    color: #475569;
    font-size: 9.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 8px 10px;
    border-top: 1px solid #cbd5e1;
    border-bottom: 1.5px solid #94a3b8;
    text-align: left;
  }
  table.items-table tbody td {
    padding: 9px 10px;
    font-size: 10.5px;
    border-bottom: 1px solid #f1f5f9;
    vertical-align: top;
    color: #1e293b;
  }
  table.items-table tbody tr:nth-child(even) td {
    background: #fafbfc;
  }
  .num {
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .cen {
    text-align: center;
    white-space: nowrap;
  }
  .service-cell {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .service-title {
    font-weight: 600;
    color: #0f172a;
    line-height: 1.35;
  }
  .service-meta {
    font-size: 9.5px;
    color: #64748b;
  }

  /* ── Totals Area ─────────────────────────────────────────────── */
  .totals-area {
    display: flex;
    justify-content: flex-end;
    margin: var(--space-xl) 0 var(--space-xl);
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .totals-table {
    width: 320px;
    border-collapse: collapse;
    font-size: 11px;
  }
  .totals-table td {
    padding: 4px 8px;
    color: #475569;
    border: none;
  }
  .totals-table td.num {
    color: #0f172a;
    font-weight: 500;
  }
  .totals-table tr.discount-row td {
    color: #166534;
  }
  .totals-table tr.grand-row td {
    border-top: 1.5px solid #0f172a;
    border-bottom: 1.5px solid #0f172a;
    font-weight: 800;
    font-size: 13.5px;
    color: #0f172a;
    padding: 8px 8px;
  }

  /* ── Payment Settlement & History (No Boxes, Calm Typography) ─── */
  .settlement-section {
    margin: var(--space-xl) 0 var(--space-xl);
    padding-top: var(--space-lg);
    border-top: 1px solid #e2e8f0;
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .section-title {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #334155;
    margin-bottom: var(--space-sm);
  }
  .settlement-table-wrap {
    display: flex;
    justify-content: flex-end;
  }
  .settlement-table {
    width: 320px;
    border-collapse: collapse;
    font-size: 11px;
  }
  .settlement-table td {
    padding: 4px 8px;
    color: #475569;
    border: none;
  }
  .settlement-table td.num {
    color: #0f172a;
    font-weight: 600;
  }
  .settlement-table tr.settlement-due-row td {
    border-top: 1.5px solid #0f172a;
    font-weight: 800;
    font-size: 13px;
    color: #0f172a;
    padding-top: 8px;
  }
  .settlement-table tr.settlement-due-row.due-positive td.num {
    color: #b91c1c;
  }
  .settlement-table tr.settlement-due-row.due-zero td.num {
    color: #15803d;
  }

  /* ── Payment History Section ─────────────────────────────────── */
  .payment-history-section {
    margin: var(--space-xl) 0 var(--space-lg);
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .payment-history-table {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    font-size: 10.5px;
    margin-top: var(--space-sm);
  }
  .payment-history-table th {
    border-top: 1px solid #cbd5e1;
    border-bottom: 1.5px solid #cbd5e1;
    padding: 6px 8px;
    font-size: 9.5px;
    font-weight: 600;
    color: #475569;
    text-align: left;
    background: #f8fafc;
  }
  .payment-history-table td {
    padding: 7px 8px;
    border-bottom: 1px solid #f1f5f9;
    color: #1e293b;
  }

  /* ── Amount in Words ─────────────────────────────────────────── */
  .words-block {
    margin: var(--space-lg) 0;
    padding-top: var(--space-sm);
    font-size: 10.5px;
    color: #475569;
    border-top: 1px solid #f1f5f9;
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .words-block strong {
    color: #0f172a;
    font-weight: 600;
  }

  /* ── Signatures & Closing ────────────────────────────────────── */
  .signatures {
    display: flex;
    justify-content: space-between;
    margin-top: 44px;
    padding-top: 16px;
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .sig-col {
    width: 36%;
    text-align: center;
  }
  .sig-line {
    border-top: 1.5px solid #94a3b8;
    padding-top: 6px;
    font-size: 11px;
    font-weight: 600;
    color: #334155;
  }
  .sig-sub {
    font-size: 9.5px;
    font-weight: 400;
    color: #64748b;
    margin-top: 3px;
    display: block;
  }
  .doc-footer {
    margin-top: 24px;
    text-align: center;
    font-size: 9.5px;
    color: #64748b;
    border-top: 1px solid #e2e8f0;
    padding-top: 8px;
  }

  /* ── Receipt Specific Typographic Styling (No Boxes/Cards) ───── */
  .receipt-amount-section {
    margin: var(--space-xl) 0 var(--space-xl);
    padding: 16px 0;
    border-top: 1px solid #e2e8f0;
    border-bottom: 1px solid #e2e8f0;
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .receipt-amount-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
  }
  .receipt-amount-label {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #475569;
  }
  .receipt-ack-subtitle {
    font-size: 9.5px;
    font-weight: 500;
    color: #64748b;
    margin-top: 2px;
  }
  .receipt-hero-amount {
    font-size: 32px;
    font-weight: 800;
    color: #0f172a;
    line-height: 1.1;
    margin: 8px 0 14px;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }
  .receipt-tender-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 16px;
    padding: 10px 0 4px;
    border-top: 1px solid #f1f5f9;
  }
  .receipt-tender-col {
    display: flex;
    flex-direction: column;
  }
  .tender-label {
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #64748b;
    margin-bottom: 2px;
    font-weight: 600;
  }
  .tender-val {
    font-size: 11.5px;
    font-weight: 700;
    color: #0f172a;
  }

  .allocation-section {
    margin: var(--space-xl) 0 var(--space-lg);
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .allocation-inv-ref {
    font-size: 10.5px;
    color: #475569;
    margin: 4px 0 12px;
  }
  .allocation-table {
    width: 100%;
    border-collapse: collapse;
  }
  .allocation-table th {
    background: #f8fafc;
    border-top: 1px solid #cbd5e1;
    border-bottom: 1.5px solid #cbd5e1;
    padding: 7px 10px;
    font-size: 9.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #475569;
    text-align: left;
  }
  .allocation-table td {
    padding: 8px 10px;
    font-size: 10.5px;
    border-bottom: 1px solid #f1f5f9;
    color: #1e293b;
    vertical-align: top;
  }
  .allocation-table tr.allocation-total-row td {
    border-top: 1.5px solid #0f172a;
    border-bottom: 1.5px solid #0f172a;
    font-weight: 700;
    font-size: 12px;
    color: #0f172a;
    padding: 9px 10px;
    background: #fafbfc;
  }
  .allocation-empty {
    color: #64748b;
    font-style: italic;
    padding: 12px 0;
    margin: 0;
    font-size: 10.5px;
  }

  /* ── Screen & Print Breakpoints ──────────────────────────────── */
  @media screen and (max-width: 700px) {
    body { padding: 8px; }
    .doc-container { padding: 16px; }
    .hosp-header { flex-direction: column; align-items: stretch; gap: 12px; }
    .doc-header-meta { text-align: left; align-items: flex-start; }
    .meta-grid { grid-template-columns: 1fr; gap: 12px; }
    .settlement-table-wrap { justify-content: stretch; }
    .settlement-table, .totals-table { width: 100%; }
    .receipt-hero-amount { font-size: 26px; }
    table.items-table th, table.items-table td { padding: 5px; font-size: 9px; }
  }

  @media print {
    @page {
      size: A4 portrait;
      margin: 10mm 12mm 12mm 12mm;
    }
    body {
      padding: 0 !important;
      background: #ffffff !important;
      font-size: 10px;
    }
    .doc-container {
      border: none !important;
      border-radius: 0 !important;
      box-shadow: none !important;
      padding: 0 !important;
      max-width: 100% !important;
    }
    .receipt-document {
      max-width: 100% !important;
    }
    table.items-table {
      break-inside: auto;
    }
    table.items-table thead {
      display: table-header-group;
    }
    table.items-table tr {
      break-inside: avoid;
      page-break-inside: avoid;
    }
    .hosp-header, .meta-grid, .totals-area, .settlement-section, .payment-history-section, .receipt-amount-section, .allocation-section, .words-block, .signatures, .doc-footer {
      break-inside: avoid;
      page-break-inside: avoid;
    }
    * {
      box-shadow: none !important;
    }
    .no-print {
      display: none !important;
    }
  }
"""


def _render_hospital_header(
    hospital: Hospital | None,
    doc_name: str,
    doc_number: str = "",
    doc_date: str = "",
    status_label: str = "",
    status_class: str = "status-paid",
) -> str:
    """Render unified hospital branding banner and document discovery header."""
    hosp_name = _esc(hospital.name if hospital else "Hospital")
    hosp_addr = _esc(hospital.address if hospital else "")
    hosp_phone = _esc(hospital.phone if hospital else "")
    hosp_email = _esc(hospital.email if hospital else "")
    gstin_value = (getattr(hospital, "gstin", None) or "").strip()
    pan_value = (getattr(hospital, "pan", None) or "").strip()
    state_value = (getattr(hospital, "state_code", None) or "").strip()

    tax_parts = []
    if gstin_value:
        tax_parts.append(f"<strong>GSTIN:</strong> {_esc(gstin_value)}")
    if pan_value:
        tax_parts.append(f"<strong>PAN:</strong> {_esc(pan_value)}")
    if state_value:
        tax_parts.append(f"<strong>State Code:</strong> {_esc(state_value)}")
    tax_info_html = f'<div class="hosp-tax-line">{" &nbsp;·&nbsp; ".join(tax_parts)}</div>' if tax_parts else ""

    contact_parts = []
    if hosp_phone:
        contact_parts.append(f"Phone: {hosp_phone}")
    if hosp_email:
        contact_parts.append(f"Email: {hosp_email}")
    contact_line = " · ".join(contact_parts)

    logo_url = (getattr(hospital, "icon_url", None) or "").strip()
    if logo_url:
        logo_html = f'<img class="hospital-logo" src="{_esc(logo_url)}" alt="" />'
    else:
        logo_html = (
            '<div class="hospital-mark" aria-hidden="true">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M12 6v12M6 12h12"/></svg>'
            '</div>'
        )

    # Determine titles and category labels based on doc_name
    if doc_name in ("TAX INVOICE", "ITEMIZED BILL / TAX INVOICE", "ITEMIZED BILL"):
        cat_label = "TAX INVOICE"
        main_title = "ITEMIZED BILL"
    elif doc_name == "PAYMENT RECEIPT":
        cat_label = "OFFICIAL RECEIPT"
        main_title = "PAYMENT RECEIPT"
    elif doc_name == "ADVANCE / DEPOSIT RECEIPT":
        cat_label = "ADVANCE DEPOSIT"
        main_title = "ADVANCE / DEPOSIT RECEIPT"
    elif doc_name == "REFUND VOUCHER":
        cat_label = "DEBIT VOUCHER"
        main_title = "REFUND VOUCHER"
    else:
        cat_label = _esc(doc_name)
        main_title = _esc(doc_name)

    doc_meta_parts = []
    if cat_label:
        doc_meta_parts.append(f'<div class="doc-category-label">{cat_label}</div>')
    doc_meta_parts.append(f'<h1 class="doc-main-title">{main_title}</h1>')
    if doc_number:
        doc_meta_parts.append(f'<div class="doc-ref-number">{_esc(doc_number)}</div>')
    if doc_date:
        doc_meta_parts.append(f'<div class="doc-date-text">Date: {_esc(doc_date)}</div>')
    if status_label:
        doc_meta_parts.append(f'<div class="doc-status-wrap"><span class="status-tag {status_class}"><strong>{_esc(status_label)}</strong></span></div>')

    doc_meta_html = "".join(doc_meta_parts)

    return f"""
    <div class="hosp-header">
      <div class="hosp-brand">
        {logo_html}
        <div class="hosp-identity">
          <div class="hosp-title">{hosp_name}</div>
          {f'<div class="hosp-meta">{hosp_addr}</div>' if hosp_addr else ''}
          {f'<div class="hosp-meta">{contact_line}</div>' if contact_line else ''}
          {tax_info_html}
        </div>
      </div>
      <div class="doc-header-meta">
        {doc_meta_html}
      </div>
    </div>
    """


def _resolve_encounter_details(db: Session, account_id: UUID | None) -> dict[str, str | None]:
    """Resolve IPD/OPD encounter metadata from the linked FinancialAccount without schema changes."""
    if not account_id:
        return {}
    try:
        from modules.billing.entities.billing_entities import FinancialAccount
        from modules.inpatient.entities.admission import Admission
        from modules.beds.entities.bed import Bed, Room, Ward
        from modules.doctors.entities.doctor import HospitalUser

        acc = db.query(FinancialAccount).filter(FinancialAccount.id == account_id).first()
        if not acc or not acc.admission_id:
            return {}

        adm = (
            db.query(Admission)
            .options(
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(Admission.id == acc.admission_id)
            .first()
        )
        if not adm:
            return {}

        adm_dt = adm.admitted_at.strftime("%d/%m/%Y %I:%M %p") if adm.admitted_at else None
        dis_dt = adm.discharged_at.strftime("%d/%m/%Y %I:%M %p") if adm.discharged_at else None

        ward_name = adm.ward.name if adm.ward else None
        room_code = adm.room.room_code if adm.room else None
        bed_code = adm.bed.bed_code if adm.bed else None

        loc_parts = []
        if ward_name:
            loc_parts.append(ward_name)
        if room_code:
            loc_parts.append(f"Room {room_code}")
        if bed_code:
            loc_parts.append(f"Bed {bed_code}")
        bed_info = " / ".join(loc_parts) if loc_parts else None

        doc_name = adm.doctor.name if adm.doctor else None

        return {
            "ip_number": adm.ip_id or f"IP-{str(adm.id)[:8].upper()}",
            "admission_date": adm_dt,
            "discharge_date": dis_dt,
            "ward": ward_name,
            "bed_info": bed_info,
            "consultant": doc_name,
        }
    except Exception:
        return {}



def invoice_html(
    inv: BillingInvoice,
    hospital: Hospital | None,
    patient: Patient | None,
    encounter: dict[str, str | None] | None = None,
    auto_print: bool = True,
    settlement: dict | None = None,
    **kwargs,
) -> str:
    """Render full HTML document for a patient itemized bill / tax invoice.

    Presents clean itemization, accurate tax breakdowns, and an authoritative
    three-metric payment settlement summary with clear visual hierarchy.
    """
    p_name = _esc(patient.name if patient else "—")
    p_uhid = _esc(patient.uhid if patient else "—")
    p_phone = _esc(getattr(patient, "mobile", None) or getattr(patient, "phone", None) or "")

    age = getattr(patient, "age", None)
    gender = getattr(patient, "gender", None)
    age_gender_parts = []
    if age is not None:
        age_gender_parts.append(f"{age} Yrs")
    if gender:
        age_gender_parts.append(gender.capitalize())
    p_age_gender = _esc(" / ".join(age_gender_parts) if age_gender_parts else "—")

    inv_date = inv.invoice_date.strftime("%d/%m/%Y") if inv.invoice_date else "—"

    grand_total = float(inv.grand_total or 0.0)
    raw_status = (inv.status.value if inv.status else "generated").lower()
    settlement = settlement or {}
    payment_records = kwargs.get("payment_records") or []
    payment_rows = settlement.get("payments") or payment_records

    amt_paid_val = settlement.get("amount_paid")
    if amt_paid_val is None:
        amt_paid_val = getattr(inv, "amount_paid", None)
    if (amt_paid_val is None or float(amt_paid_val) == 0.0) and payment_rows:
        amt_paid_val = sum(float(p.get("amount", 0) or 0) for p in payment_rows)
    if amt_paid_val is None and (raw_status == "paid"):
        amt_paid_val = grand_total

    amount_paid = min(grand_total, max(0.0, float(amt_paid_val or 0.0)))
    balance_due = max(0.0, round(grand_total - amount_paid, 2))

    # Evaluate lifecycle status based on authoritative accounting data
    if raw_status in ("cancelled", "void"):
        status_label = "CANCELLED"
        status_class = "status-cancelled"
    elif raw_status == "paid" or (grand_total > 0 and balance_due == 0):
        status_label = "PAID"
        status_class = "status-paid"
    elif amount_paid > 0 and balance_due > 0:
        status_label = "PARTIALLY PAID"
        status_class = "status-partial"
    else:
        status_label = "UNPAID" if raw_status in ("generated", "draft") else raw_status.upper()
        status_class = "status-unpaid"

    enc = encounter or {}
    ip_no = enc.get("ip_number")
    adm_dt = enc.get("admission_date")
    dis_dt = enc.get("discharge_date")
    bed_info = enc.get("bed_info")
    consultant = enc.get("consultant")

    lines = sorted(inv.lines or [], key=lambda ln: ln.sort_order)
    rows = []
    for idx, ln in enumerate(lines, start=1):
        desc = _clean_service_display(ln.description)
        qty = f"{float(ln.quantity):g}"
        rate = f"₹{float(ln.rate):,.2f}"
        amt = f"₹{float(ln.amount):,.2f}"

        # Category / HSN metadata
        cat_name = SOURCE_CATEGORY_LABELS.get(getattr(ln, "source_type", ""), "")
        hsn_code = getattr(ln, "hsn_sac_code", None)
        meta_parts = []
        if cat_name and cat_name.lower() not in desc.lower():
            meta_parts.append(cat_name.replace(" Charges", ""))
        if hsn_code and str(hsn_code).strip():
            meta_parts.append(f"HSN/SAC: {str(hsn_code).strip()}")
        meta_text = " · ".join(meta_parts)

        # Line-level discount & tax calculation
        qty_num = float(ln.quantity or 1.0)
        rate_num = float(ln.rate or 0.0)
        gross_line = qty_num * rate_num
        net_line = float(ln.amount or 0.0)
        gst_r = float(getattr(ln, "gst_rate", 0.0) or 0.0)
        c_tax = float(getattr(ln, "cgst_amount", 0.0) or 0.0)
        s_tax = float(getattr(ln, "sgst_amount", 0.0) or 0.0)
        i_tax = 0.0
        if not (c_tax or s_tax) and gst_r > 0:
            line_amount = float(getattr(ln, "amount", 0.0) or 0.0)
            i_tax = round(line_amount * gst_r / (100.0 + gst_r), 2)
        tot_tax = c_tax + s_tax + i_tax
        disc_num = max(0.0, round(gross_line + tot_tax - net_line, 2))
        disc_str = f"₹{disc_num:,.2f}" if disc_num > 0 else "₹0.00"
        tax_str = f"₹{tot_tax:,.2f}" if tot_tax > 0 else "₹0.00"
        if gst_r > 0:
            tax_str += f"<br/><span style='font-size:9px;color:#64748b;'>({gst_r:g}%)</span>"

        rows.append(
            f"<tr>"
            f"<td class='cen'>{idx:02d}</td>"
            f"<td>"
            f"<div class='service-cell'>"
            f"<div class='service-title'>{_esc(desc)}</div>"
            f"{f'<div class=\"service-meta\">{_esc(meta_text)}</div>' if meta_text else ''}"
            f"</div>"
            f"</td>"
            f"<td class='num'>{qty}</td>"
            f"<td class='num'>{rate}</td>"
            f"<td class='num'>{disc_str}</td>"
            f"<td class='num'>{tax_str}</td>"
            f"<td class='num'><strong>{amt}</strong></td>"
            f"</tr>"
        )
    rows_html = "".join(rows) or "<tr><td colspan='7' class='cen' style='padding:20px 0;'>No items on this invoice</td></tr>"

    taxable = float(getattr(inv, "taxable_amount", 0.0) or (float(inv.subtotal) - float(inv.discount_amount)))
    cgst = float(getattr(inv, "cgst_amount", 0.0) or 0.0)
    sgst = float(getattr(inv, "sgst_amount", 0.0) or 0.0)
    igst = float(getattr(inv, "igst_amount", 0.0) or 0.0)
    words = amount_to_words_inr(grand_total)

    payment_history_rows = "".join(
        "<tr><td>" + _esc(row.get("method") or row.get("payment_method") or "Payment") + "</td><td class='num'>₹" +
        f"{float(row.get('amount') or 0):,.2f}</td><td>" + _esc(row.get("date") or row.get("payment_date") or "") +
        "</td><td>" + _esc(row.get("receipt") or row.get("receipt_number") or "") + "</td></tr>"
        for row in payment_rows
    )

    # Encounter rows only if present
    encounter_html = ""
    if ip_no or adm_dt or dis_dt or bed_info or consultant:
        encounter_items = [
            ("IP No", ip_no),
            ("Admission", adm_dt),
            ("Discharge", dis_dt),
            ("Ward / Bed", bed_info),
            ("Consultant", _clean_service_display(consultant)),
        ]
        encounter_html = "".join(
            f'<div class="meta-row"><span class="meta-label">{label}:</span><span class="meta-val">{_esc(value)}</span></div>'
            for label, value in encounter_items if value
        )

    due_class = "due-zero" if balance_due == 0 else "due-positive"
    print_script = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Invoice - {_esc(inv.invoice_number)}</title>
  <style>{_shared_doc_styles()}</style>
</head>
<body>
  <div class="doc-container">
    {_render_hospital_header(
        hospital,
        "ITEMIZED BILL / TAX INVOICE",
        doc_number=inv.invoice_number,
        doc_date=inv_date,
        status_label=status_label,
        status_class=status_class,
    )}

    <div class="meta-grid">
      <div class="meta-col">
        <div class="meta-col-header">Billing & Encounter Details</div>
        <div class="meta-row"><span class="meta-label">Invoice No.:</span><span class="meta-val"><strong>{_esc(inv.invoice_number)}</strong></span></div>
        <div class="meta-row"><span class="meta-label">Date:</span><span class="meta-val">{inv_date}</span></div>
        <div class="meta-row"><span class="meta-label">Status:</span><span class="meta-val"><strong>{_esc(status_label)}</strong></span></div>
        {encounter_html}
      </div>
      <div class="meta-col">
        <div class="meta-col-header">Patient Details</div>
        <div class="meta-row"><span class="meta-label">Patient Name:</span><span class="meta-val meta-val-primary">{p_name}</span></div>
        <div class="meta-row"><span class="meta-label">MRN / UHID:</span><span class="meta-val"><strong>{p_uhid}</strong></span></div>
        <div class="meta-row"><span class="meta-label">Age / Gender:</span><span class="meta-val">{p_age_gender}</span></div>
        {f'<div class="meta-row"><span class="meta-label">Contact No:</span><span class="meta-val">{p_phone}</span></div>' if p_phone else ''}
      </div>
    </div>

    <table class="items-table">
      <thead>
        <tr>
          <th style="width:36px;" class="cen">#</th>
          <th>Particulars / Service Description</th>
          <th style="width:45px;" class="num">Qty</th>
          <th style="width:75px;" class="num">Rate</th>
          <th style="width:70px;" class="num">Discount</th>
          <th style="width:75px;" class="num">Tax</th>
          <th style="width:85px;" class="num">Amount</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>

    <div class="totals-area">
      <table class="totals-table">
        <tr>
          <td>Gross Amount</td>
          <td class="num">₹{float(inv.subtotal):,.2f}</td>
        </tr>
        {f'<tr class="discount-row"><td>Discount / Concession</td><td class="num">-₹{float(inv.discount_amount):,.2f}</td></tr>' if float(inv.discount_amount) > 0 else ''}
        <tr>
          <td>Taxable Value</td>
          <td class="num">₹{taxable:,.2f}</td>
        </tr>
        {f'<tr><td>CGST</td><td class="num">₹{cgst:,.2f}</td></tr>' if cgst > 0 else ''}
        {f'<tr><td>SGST</td><td class="num">₹{sgst:,.2f}</td></tr>' if sgst > 0 else ''}
        {f'<tr><td>IGST</td><td class="num">₹{igst:,.2f}</td></tr>' if igst > 0 else ''}
        {f'<tr><td>Total Tax</td><td class="num">₹{float(inv.tax_amount):,.2f}</td></tr>' if float(inv.tax_amount) > 0 else ''}
        <tr class="grand-row">
          <td>Net Bill / Grand Total</td>
          <td class="num">₹{grand_total:,.2f}</td>
        </tr>
      </table>
    </div>

    <div class="settlement-section">
      <div class="section-title">Payment Settlement</div>
      <div class="settlement-table-wrap">
        <table class="settlement-table">
          <tr>
            <td>Invoice Total</td>
            <td class="num">₹{grand_total:,.2f}</td>
          </tr>
          <tr>
            <td>Amount Paid</td>
            <td class="num">₹{amount_paid:,.2f}</td>
          </tr>
          <tr class="settlement-due-row {due_class}">
            <td>Balance Due</td>
            <td class="num">₹{balance_due:,.2f}</td>
          </tr>
        </table>
      </div>
    </div>

    {f'''
    <div class="payment-history-section">
      <div class="section-title">Payment History</div>
      <table class="payment-history-table">
        <thead>
          <tr>
            <th>Payment Mode</th>
            <th class="num">Amount</th>
            <th>Date</th>
            <th>Receipt No.</th>
          </tr>
        </thead>
        <tbody>{payment_history_rows}</tbody>
      </table>
    </div>
    ''' if payment_history_rows else ''}

    <div class="words-block">
      <strong>Amount in Words:</strong> {words}
    </div>

    {f'<div style="margin-top:10px;font-size:10.5px;color:#475569;"><strong>Notes:</strong> {_esc(inv.notes)}</div>' if inv.notes else ''}

    <div class="signatures">
      <div class="sig-col">
        <div class="sig-line">Prepared By</div>
        <span class="sig-sub">Billing Department</span>
      </div>
      <div class="sig-col">
        <div class="sig-line">Authorized Signatory</div>
        <span class="sig-sub">Hospital Financial Authority</span>
      </div>
    </div>

    <div class="doc-footer">
      Computer-generated hospital bill. | Authorized medical invoice for accounting and insurance reimbursement.
    </div>
  </div>
  {print_script}
</body>
</html>"""


def receipt_html(
    receipt: BillingReceipt,
    hospital: Hospital | None,
    patient: Patient | None,
    linked_invoice: BillingInvoice | None = None,
    auto_print: bool = True,
    paid_against: list[dict] | None = None,
) -> str:
    """Render full HTML document for a payment receipt.

    Emphasizes AMOUNT RECEIVED, PAYMENT METHOD, RECEIPT REF, PAYMENT DATE,
    and PAYMENT ALLOCATION, clearly distinguishing it from an invoice.
    """
    p_name = _esc(patient.name if patient else "—")
    p_uhid = _esc(patient.uhid if patient else "—")
    p_phone = _esc(getattr(patient, "mobile", None) or getattr(patient, "phone", None) or "")
    rcpt_date = receipt.payment_date.strftime("%d/%m/%Y") if receipt.payment_date else "—"

    method = receipt.payment_method.value if receipt.payment_method else "cash"
    if method == "bank_transfer":
        method_label = "Bank Transfer / NEFT"
    else:
        method_label = method.replace("_", " ").title()

    amount = float(receipt.amount)
    words = amount_to_words_inr(amount)
    inv_ref = _esc(linked_invoice.invoice_number) if linked_invoice else ""
    paid_against = paid_against or []

    status_raw = (receipt.status.value if receipt.status else "issued").lower()
    if status_raw in ("cleared", "reconciled"):
        status_label = "CLEARED"
        status_class = "status-paid"
    elif status_raw in ("issued", "paid"):
        status_label = "PAID"
        status_class = "status-paid"
    elif status_raw in ("void", "cancelled"):
        status_label = "VOID"
        status_class = "status-void"
    else:
        status_label = status_raw.upper()
        status_class = "status-issued"

    header_html = _render_hospital_header(
        hospital=hospital,
        doc_name="PAYMENT RECEIPT",
        doc_number=receipt.receipt_number,
        doc_date=rcpt_date,
        status_label="",
        status_class=status_class,
    )

    paid_rows = "".join(
        f"""<tr>
          <td class="cen" style="width:36px;">{idx:02d}</td>
          <td>
            <div class="item-name">{_esc(_clean_service_display(row.get('description')))}</div>
            {f'<div class="item-meta">{_esc(row.get("category", "")).title()}{(" · " + _esc(row.get("service_code", ""))) if row.get("service_code") else ""}</div>' if (row.get('category') or row.get('service_code')) else ''}
          </td>
          <td class="num" style="width:140px;">₹{float(row.get('amount') or 0):,.2f}</td>
        </tr>"""
        for idx, row in enumerate(
            (item for item in paid_against if float(item.get("amount") or 0) > 0), start=1
        )
    )

    print_script = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Payment Receipt - {_esc(receipt.receipt_number)}</title>
  <style>{_shared_doc_styles()}</style>
</head>
<body>
  <div class="doc-container receipt-document">
    {header_html}

    <div class="meta-grid">
      <div class="meta-col">
        <div class="meta-col-header">Patient Details</div>
        <div class="meta-row"><span class="meta-label">Patient Name:</span><span class="meta-val"><strong>{p_name}</strong></span></div>
        <div class="meta-row"><span class="meta-label">UHID / MRN:</span><span class="meta-val"><strong>{p_uhid}</strong></span></div>
        {f'<div class="meta-row"><span class="meta-label">Contact No:</span><span class="meta-val">{p_phone}</span></div>' if p_phone else ''}
      </div>
      <div class="meta-col">
        <div class="meta-col-header">Payment Context</div>
        {f'<div class="meta-row"><span class="meta-label">Invoice Ref:</span><span class="meta-val"><strong>{inv_ref}</strong></span></div>' if inv_ref else '<div class="meta-row"><span class="meta-label">Settlement:</span><span class="meta-val">Direct Account Payment</span></div>'}
        <div class="meta-row"><span class="meta-label">Payment Mode:</span><span class="meta-val"><strong>{_esc(method_label)}</strong></span></div>
        {f'<div class="meta-row"><span class="meta-label">Transaction Ref:</span><span class="meta-val">{_esc(receipt.reference_number)}</span></div>' if receipt.reference_number else ''}
        <div class="meta-row"><span class="meta-label">Received By:</span><span class="meta-val">{_esc(receipt.collected_by_name or 'Cash Counter')}</span></div>
      </div>
    </div>

    <!-- HERO SECTION: What money did the hospital receive? -->
    <div class="receipt-amount-section">
      <div class="receipt-amount-header">
        <div>
          <div class="receipt-amount-label">Amount Received</div>
          <div class="receipt-ack-subtitle">Payment Acknowledgment Summary</div>
        </div>
        <span class="status-tag {status_class}">{status_label}</span>
      </div>
      <div class="receipt-hero-amount">₹{amount:,.2f}</div>
      <div class="receipt-tender-grid">
        <div class="receipt-tender-col">
          <span class="tender-label">Payment Mode</span>
          <span class="tender-val">{_esc(method_label)}</span>
        </div>
        <div class="receipt-tender-col">
          <span class="tender-label">Payment Date</span>
          <span class="tender-val">{rcpt_date}</span>
        </div>
        {f'''<div class="receipt-tender-col">
          <span class="tender-label">Transaction Ref</span>
          <span class="tender-val">{_esc(receipt.reference_number)}</span>
        </div>''' if receipt.reference_number else ''}
        {f'''<div class="receipt-tender-col">
          <span class="tender-label">Applied To</span>
          <span class="tender-val">{_esc(inv_ref)}</span>
        </div>''' if inv_ref else ''}
      </div>
      <div class="words-block" style="margin-top:12px;">
        <strong>Amount in Words:</strong> {words}
      </div>
    </div>

    <!-- Hidden compatibility table for scraping/tests -->
    <table class="payment-detail-table" style="display:none;" aria-hidden="true">
      <tbody>
        <tr><th>Amount Received</th><td class="amount">₹{amount:,.2f}</td></tr>
        <tr><th>Payment Mode</th><td>{_esc(method_label)}</td></tr>
        {f'<tr><th>Transaction Reference</th><td>{_esc(receipt.reference_number)}</td></tr>' if receipt.reference_number else ''}
        <tr><th>Received By</th><td>{_esc(receipt.collected_by_name or 'Cash Counter')}</td></tr>
      </tbody>
    </table>

    {f'<div style="margin:8px 0;font-size:10px;color:#475569;"><strong>Notes:</strong> {_esc(receipt.notes)}</div>' if receipt.notes else ''}

    <!-- PAYMENT ALLOCATION: What the payment was applied to -->
    <div class="allocation-section">
      <div class="section-title">Payment Applied To</div>
      {f'<div class="allocation-inv-ref">Paid Against Invoice: <strong>{inv_ref}</strong></div>' if inv_ref else ''}
      {f'''<table class="allocation-table">
        <thead>
          <tr>
            <th style="width:36px;" class="cen">#</th>
            <th>Particulars / Applied Charge</th>
            <th class="num" style="width:140px;">Amount Paid (₹)</th>
          </tr>
        </thead>
        <tbody>
          {paid_rows}
          <tr class="allocation-total-row">
            <td colspan="2" style="text-align:right;">Total Received</td>
            <td class="num">₹{amount:,.2f}</td>
          </tr>
        </tbody>
      </table>''' if paid_rows else '<p class="allocation-empty">Payment credited to patient ledger / invoice settlement.</p>'}
    </div>

    <div class="signatures">
      <div class="sig-col">
        <div class="sig-line">Received By</div>
        <span class="sig-sub">{_esc(receipt.collected_by_name or 'Cash Counter')}</span>
      </div>
      <div class="sig-col">
        <div class="sig-line">Authorized Signatory</div>
        <span class="sig-sub">Hospital Financial Authority</span>
      </div>
    </div>

    <div class="doc-footer">
      Official acknowledgement of payment. | Computer-generated hospital receipt.
    </div>
  </div>
  {print_script}
</body>
</html>"""


def deposit_receipt_html(
    deposit: BillingDeposit,
    hospital: Hospital | None,
    patient: Patient | None,
    encounter: dict[str, str | None] | None = None,
    auto_print: bool = True,
) -> str:
    """Render full HTML document for an advance / deposit receipt."""
    p_name = _esc(patient.name if patient else "—")
    p_uhid = _esc(patient.uhid if patient else "—")
    p_phone = _esc(getattr(patient, "mobile", None) or getattr(patient, "phone", None) or "")
    dep_date = deposit.deposit_date.strftime("%d/%m/%Y") if deposit.deposit_date else "—"

    method = deposit.payment_method.value if deposit.payment_method else "cash"
    if method == "bank_transfer":
        method_label = "Bank Transfer / NEFT"
    else:
        method_label = method.replace("_", " ").title()

    amount = float(deposit.original_amount)
    words = amount_to_words_inr(amount)

    enc = encounter or {}
    ip_no = enc.get("ip_number")

    dep_type_label = deposit.deposit_type.replace("_", " ").title()
    status_raw = (deposit.status.value if deposit.status else "available").lower()
    if status_raw in ("available", "active"):
        status_label = "AVAILABLE"
        status_class = "status-paid"
    elif status_raw in ("utilized", "adjusted"):
        status_label = "UTILIZED"
        status_class = "status-partial"
    else:
        status_label = status_raw.upper()
        status_class = "status-issued"

    header_html = _render_hospital_header(
        hospital=hospital,
        doc_name="ADVANCE / DEPOSIT RECEIPT",
        doc_number=deposit.deposit_number,
        doc_date=dep_date,
        status_label="",
        status_class=status_class,
    )

    print_script = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Advance Receipt - {_esc(deposit.deposit_number)}</title>
  <style>{_shared_doc_styles()}</style>
</head>
<body>
  <div class="doc-container receipt-document">
    {header_html}

    <div class="meta-grid">
      <div class="meta-col">
        <div class="meta-col-header">Patient Details</div>
        <div class="meta-row"><span class="meta-label">Patient Name:</span><span class="meta-val"><strong>{p_name}</strong></span></div>
        <div class="meta-row"><span class="meta-label">UHID / MRN:</span><span class="meta-val"><strong>{p_uhid}</strong></span></div>
        {f'<div class="meta-row"><span class="meta-label">Contact No:</span><span class="meta-val">{p_phone}</span></div>' if p_phone else ''}
      </div>
      <div class="meta-col">
        <div class="meta-col-header">Deposit Reference</div>
        <div class="meta-row"><span class="meta-label">Deposit Type:</span><span class="meta-val"><strong>{_esc(dep_type_label)}</strong></span></div>
        <div class="meta-row"><span class="meta-label">Payment Mode:</span><span class="meta-val"><strong>{_esc(method_label)}</strong></span></div>
        {f'<div class="meta-row"><span class="meta-label">IP / Episode No:</span><span class="meta-val">{_esc(ip_no)}</span></div>' if ip_no else ''}
        {f'<div class="meta-row"><span class="meta-label">Transaction Ref:</span><span class="meta-val">{_esc(deposit.reference_number)}</span></div>' if deposit.reference_number else ''}
        <div class="meta-row"><span class="meta-label">Received By:</span><span class="meta-val">{_esc(deposit.received_by_name or 'Cash Counter')}</span></div>
      </div>
    </div>

    <!-- Hero Section for Deposit -->
    <div class="receipt-amount-section">
      <div class="receipt-amount-header">
        <div class="receipt-amount-label">Total Advance Received</div>
        <span class="status-tag {status_class}">{status_label}</span>
      </div>
      <div class="receipt-hero-amount">₹{amount:,.2f}</div>
      <div class="receipt-tender-grid">
        <div class="receipt-tender-col">
          <span class="tender-label">Deposit Type</span>
          <span class="tender-val">{_esc(dep_type_label)}</span>
        </div>
        <div class="receipt-tender-col">
          <span class="tender-label">Payment Mode</span>
          <span class="tender-val">{_esc(method_label)}</span>
        </div>
        <div class="receipt-tender-col">
          <span class="tender-label">Date</span>
          <span class="tender-val">{dep_date}</span>
        </div>
        {f'''<div class="receipt-tender-col">
          <span class="tender-label">Reference</span>
          <span class="tender-val">{_esc(deposit.reference_number)}</span>
        </div>''' if deposit.reference_number else ''}
      </div>
      <div class="words-block" style="margin-top:12px;">
        <strong>Amount in Words:</strong> {words}
      </div>
    </div>

    <table class="items-table">
      <thead>
        <tr>
          <th style="width:36px;" class="cen">#</th>
          <th>Description / Purpose</th>
          <th style="width:120px;" class="cen">Payment Mode</th>
          <th style="width:140px;" class="num">Deposit Amount</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="cen">01</td>
          <td>
            <div class="item-name">Inpatient / Clinical Advance Deposit ({_esc(dep_type_label)})</div>
            {f"<div class='item-meta'>{_esc(deposit.notes)}</div>" if deposit.notes else ""}
          </td>
          <td class="cen">{_esc(method_label)}</td>
          <td class="num">₹{amount:,.2f}</td>
        </tr>
      </tbody>
    </table>

    <div class="totals-area">
      <table class="totals-table">
        <tr class="grand-row">
          <td>Total Advance Received</td>
          <td class="num">₹{amount:,.2f}</td>
        </tr>
      </table>
    </div>

    <div class="words-block">
      <strong>Amount in Words:</strong> {words}
    </div>

    <div class="signatures">
      <div class="sig-col">
        <div class="sig-line">Depositor / Payer Signature</div>
      </div>
      <div class="sig-col">
        <div class="sig-line">Authorized Signatory</div>
        <span class="sig-sub">{_esc(deposit.received_by_name or 'Cash Counter')}</span>
      </div>
    </div>

    <div class="doc-footer">
      Advance deposits are held in patient account and adjusted against final itemized invoices upon discharge or settlement.
    </div>
  </div>
  {print_script}
</body>
</html>"""


def refund_html(
    refund: BillingRefund,
    hospital: Hospital | None,
    patient: Patient | None,
    auto_print: bool = True,
) -> str:
    """Render full HTML document for a refund payment voucher."""
    p_name = _esc(patient.name if patient else "—")
    p_uhid = _esc(patient.uhid if patient else "—")
    p_phone = _esc(getattr(patient, "mobile", None) or getattr(patient, "phone", None) or "")
    ref_date = refund.refund_date.strftime("%d/%m/%Y") if refund.refund_date else "—"

    method = refund.refund_method.value if refund.refund_method else "cash"
    if method == "bank_transfer":
        method_label = "Bank Transfer / NEFT"
    else:
        method_label = method.replace("_", " ").title()

    amount = float(refund.amount)
    words = amount_to_words_inr(amount)

    status_raw = (refund.status.value if refund.status else "issued").lower()
    if status_raw in ("issued", "completed", "approved"):
        status_label = "REFUNDED"
        status_class = "status-refunded"
    else:
        status_label = status_raw.upper()
        status_class = "status-void"

    header_html = _render_hospital_header(
        hospital=hospital,
        doc_name="REFUND VOUCHER",
        doc_number=refund.refund_number,
        doc_date=ref_date,
        status_label="",
        status_class=status_class,
    )

    print_script = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Refund Voucher - {_esc(refund.refund_number)}</title>
  <style>{_shared_doc_styles()}</style>
</head>
<body>
  <div class="doc-container receipt-document">
    {header_html}

    <div class="meta-grid">
      <div class="meta-col">
        <div class="meta-col-header">Patient Details</div>
        <div class="meta-row"><span class="meta-label">Patient Name:</span><span class="meta-val"><strong>{p_name}</strong></span></div>
        <div class="meta-row"><span class="meta-label">UHID / MRN:</span><span class="meta-val"><strong>{p_uhid}</strong></span></div>
        {f'<div class="meta-row"><span class="meta-label">Contact No:</span><span class="meta-val">{p_phone}</span></div>' if p_phone else ''}
      </div>
      <div class="meta-col">
        <div class="meta-col-header">Voucher Details</div>
        <div class="meta-row"><span class="meta-label">Refund Mode:</span><span class="meta-val"><strong>{_esc(method_label)}</strong></span></div>
        <div class="meta-row"><span class="meta-label">Processed By:</span><span class="meta-val">{_esc(refund.processed_by_name or 'Accounts Department')}</span></div>
      </div>
    </div>

    <!-- Hero Section for Refund -->
    <div class="receipt-amount-section" style="border-top-color:#fca5a5; border-bottom-color:#fca5a5;">
      <div class="receipt-amount-header">
        <div class="receipt-amount-label" style="color:#991b1b;">Total Amount Refunded</div>
        <span class="status-tag {status_class}">{status_label}</span>
      </div>
      <div class="receipt-hero-amount" style="color:#991b1b;">₹{amount:,.2f}</div>
      <div class="receipt-tender-grid">
        <div class="receipt-tender-col">
          <span class="tender-label" style="color:#991b1b;">Refund Mode</span>
          <span class="tender-val" style="color:#7f1d1d;">{_esc(method_label)}</span>
        </div>
        <div class="receipt-tender-col">
          <span class="tender-label" style="color:#991b1b;">Date</span>
          <span class="tender-val" style="color:#7f1d1d;">{ref_date}</span>
        </div>
      </div>
      <div class="words-block" style="margin-top:12px;">
        <strong>Amount in Words:</strong> {words}
      </div>
    </div>

    <table class="items-table">
      <thead>
        <tr>
          <th style="width:36px;" class="cen">#</th>
          <th>Particulars / Reason for Refund</th>
          <th style="width:120px;" class="cen">Refund Mode</th>
          <th style="width:140px;" class="num">Refund Amount</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="cen">01</td>
          <td>
            <div class="item-name">{_esc(refund.reason or "Hospital refund / reversal")}</div>
          </td>
          <td class="cen">{_esc(method_label)}</td>
          <td class="num">₹{amount:,.2f}</td>
        </tr>
      </tbody>
    </table>

    <div class="totals-area">
      <table class="totals-table">
        <tr class="grand-row">
          <td>Total Refunded</td>
          <td class="num">₹{amount:,.2f}</td>
        </tr>
      </table>
    </div>

    <div class="words-block">
      <strong>Amount in Words:</strong> {words}
    </div>

    <div class="signatures">
      <div class="sig-col">
        <div class="sig-line">Receiver / Patient Signature</div>
      </div>
      <div class="sig-col">
        <div class="sig-line">Authorized Signatory</div>
        <span class="sig-sub">{_esc(refund.processed_by_name or 'Accounts Department')}</span>
      </div>
    </div>

    <div class="doc-footer">
      Official refund debit acknowledgment. Retain this voucher for audit and account clearance.
    </div>
  </div>
  {print_script}
</body>
</html>"""


def patient_outstanding_for_invoice(
    db: Session, hospital_id: UUID, patient_id: UUID
) -> float:
    """Compute patient ledger outstanding balance."""
    return float(patient_ledger_totals(db, hospital_id, patient_id)["outstanding"])
