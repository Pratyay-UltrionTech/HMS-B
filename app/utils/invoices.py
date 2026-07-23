"""Invoice & receipt helpers: hospital-scoped numbering, snapshots, printable HTML."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    BillingCharge,
    BillingChargeStatus,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingReceipt,
    BillingReceiptStatus,
    Hospital,
    Patient,
)
from app.utils.billing import patient_ledger_totals

SOURCE_CATEGORY_LABELS: dict[str, str] = {
    "consultation": "Consultation Charges",
    "laboratory": "Laboratory Charges",
    "radiology": "Radiology Charges",
    "admission": "Admission Charges",
    "bed": "Bed Charges",
    "ot": "OT Charges",
    "other": "Manual Charges",
    "adjustment": "Manual Charges",
}


def _esc(s: str | None) -> str:
    return (s or "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def next_invoice_number(db: Session, hospital_id: UUID, year: int | None = None) -> str:
    y = year or date.today().year
    prefix = f"INV-{y}-"
    rows = (
        db.query(BillingInvoice.invoice_number)
        .filter(
            BillingInvoice.hospital_id == hospital_id,
            BillingInvoice.invoice_number.like(f"{prefix}%"),
        )
        .all()
    )
    max_seq = 0
    for (num,) in rows:
        try:
            max_seq = max(max_seq, int(str(num).split("-")[-1]))
        except (ValueError, IndexError):
            continue
    return f"{prefix}{max_seq + 1:05d}"


def next_receipt_number(db: Session, hospital_id: UUID, year: int | None = None) -> str:
    y = year or date.today().year
    prefix = f"RCPT-{y}-"
    rows = (
        db.query(BillingReceipt.receipt_number)
        .filter(
            BillingReceipt.hospital_id == hospital_id,
            BillingReceipt.receipt_number.like(f"{prefix}%"),
        )
        .all()
    )
    max_seq = 0
    for (num,) in rows:
        try:
            max_seq = max(max_seq, int(str(num).split("-")[-1]))
        except (ValueError, IndexError):
            continue
    return f"{prefix}{max_seq + 1:05d}"


def charges_already_invoiced(db: Session, hospital_id: UUID, charge_ids: list[UUID]) -> list[UUID]:
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


def refresh_invoice_paid_status(db: Session, hospital_id: UUID, patient_id: UUID) -> None:
    """Mark generated invoices as paid when all linked charges are paid."""
    invoices = (
        db.query(BillingInvoice)
        .options(joinedload(BillingInvoice.lines))
        .filter(
            BillingInvoice.hospital_id == hospital_id,
            BillingInvoice.patient_id == patient_id,
            BillingInvoice.status == BillingInvoiceStatus.generated,
        )
        .all()
    )
    for inv in invoices:
        charge_ids = [ln.charge_id for ln in inv.lines if ln.charge_id]
        if not charge_ids:
            continue
        open_count = (
            db.query(func.count(BillingCharge.id))
            .filter(
                BillingCharge.id.in_(charge_ids),
                BillingCharge.status.in_(
                    [BillingChargeStatus.pending, BillingChargeStatus.partially_paid]
                ),
            )
            .scalar()
            or 0
        )
        if int(open_count) == 0:
            inv.status = BillingInvoiceStatus.paid


def create_invoice_from_charges(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    charge_ids: list[UUID],
    invoice_date: date | None = None,
    tax_amount: float = 0.0,
    notes: str | None = None,
    created_by_name: str = "",
) -> BillingInvoice:
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
    tax = round(max(0.0, float(tax_amount or 0)), 2)
    net_sum = round(sum(float(c.net_amount or 0) for c in charges), 2)
    grand_total = round(net_sum + tax, 2)

    inv = BillingInvoice(
        hospital_id=hospital_id,
        patient_id=patient_id,
        invoice_number=next_invoice_number(db, hospital_id),
        invoice_date=invoice_date or date.today(),
        subtotal=subtotal,
        discount_amount=discount_amount,
        tax_amount=tax,
        grand_total=grand_total,
        status=BillingInvoiceStatus.generated,
        notes=notes,
        created_by_name=created_by_name or "Staff",
    )
    db.add(inv)
    db.flush()

    for idx, c in enumerate(charges):
        src = c.source_type.value if c.source_type else "other"
        db.add(
            BillingInvoiceLine(
                hospital_id=hospital_id,
                invoice_id=inv.id,
                charge_id=c.id,
                source_type=src,
                description=c.description[:512],
                quantity=1.0,
                rate=round(float(c.charge_amount or 0), 2),
                amount=round(float(c.net_amount or 0), 2),
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


def invoice_to_dict(inv: BillingInvoice, patient: Patient | None = None) -> dict:
    p = patient or inv.patient
    lines = sorted(inv.lines or [], key=lambda ln: ln.sort_order)
    return {
        "id": inv.id,
        "hospital_id": inv.hospital_id,
        "patient_id": inv.patient_id,
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date,
        "subtotal": inv.subtotal,
        "discount_amount": inv.discount_amount,
        "tax_amount": inv.tax_amount,
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
                "category_label": SOURCE_CATEGORY_LABELS.get(ln.source_type, "Manual Charges"),
                "description": ln.description,
                "quantity": ln.quantity,
                "rate": ln.rate,
                "amount": ln.amount,
                "sort_order": ln.sort_order,
            }
            for ln in lines
        ],
    }


def receipt_to_dict(r: BillingReceipt, patient: Patient | None = None) -> dict:
    p = patient or r.patient
    return {
        "id": r.id,
        "hospital_id": r.hospital_id,
        "patient_id": r.patient_id,
        "payment_id": r.payment_id,
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


def invoice_html(
    inv: BillingInvoice,
    hospital: Hospital | None,
    patient: Patient | None,
    outstanding: float = 0.0,
    auto_print: bool = True,
) -> str:
    hosp_name = _esc(hospital.name if hospital else "Hospital")
    hosp_addr = _esc(hospital.address if hospital else "")
    hosp_phone = _esc(hospital.phone if hospital else "")
    hosp_email = _esc(hospital.email if hospital else "")
    p_name = _esc(patient.name if patient else None)
    p_uhid = _esc(patient.uhid if patient else None)
    lines = sorted(inv.lines or [], key=lambda ln: ln.sort_order)
    rows = "".join(
        f"<tr><td>{_esc(SOURCE_CATEGORY_LABELS.get(ln.source_type, ln.source_type))}<br/>"
        f"<span style='color:#64748b;font-size:12px'>{_esc(ln.description)}</span></td>"
        f"<td class='num'>{ln.quantity:g}</td>"
        f"<td class='num'>₹{float(ln.rate):,.2f}</td>"
        f"<td class='num'>₹{float(ln.amount):,.2f}</td></tr>"
        for ln in lines
    ) or "<tr><td colspan='4'>No lines</td></tr>"
    print_script = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{_esc(inv.invoice_number)}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 800px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #047857; margin: 0 0 4px; font-size: 22px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 4px; }}
  .grid {{ display: flex; justify-content: space-between; gap: 24px; margin: 20px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; font-size: 13px; vertical-align: top; }}
  th {{ background: #ecfdf5; }}
  .num {{ text-align: right; white-space: nowrap; }}
  .totals {{ margin-top: 16px; width: 280px; margin-left: auto; }}
  .totals td {{ border: none; padding: 4px 0; }}
  .totals .grand {{ font-weight: 800; font-size: 15px; border-top: 2px solid #0f172a; padding-top: 8px; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <h1>{hosp_name}</h1>
  <p class="meta">{hosp_addr}</p>
  <p class="meta">Phone: {hosp_phone} · Email: {hosp_email}</p>
  <hr style="border:none;border-top:2px solid #047857;margin:16px 0"/>
  <div class="grid">
    <div>
      <p><strong>Invoice</strong></p>
      <p class="meta">No: {_esc(inv.invoice_number)}</p>
      <p class="meta">Date: {inv.invoice_date.strftime('%d %b %Y') if inv.invoice_date else '—'}</p>
      <p class="meta">Status: {_esc(inv.status.value if inv.status else None)}</p>
    </div>
    <div>
      <p><strong>Bill To</strong></p>
      <p class="meta">{p_name}</p>
      <p class="meta">UHID: {p_uhid}</p>
    </div>
  </div>
  <table>
    <thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <table class="totals">
    <tr><td>Subtotal</td><td class="num">₹{float(inv.subtotal):,.2f}</td></tr>
    <tr><td>Discount</td><td class="num">₹{float(inv.discount_amount):,.2f}</td></tr>
    <tr><td>Tax</td><td class="num">₹{float(inv.tax_amount):,.2f}</td></tr>
    <tr class="grand"><td>Grand Total</td><td class="num">₹{float(inv.grand_total):,.2f}</td></tr>
    <tr><td>Outstanding</td><td class="num">₹{float(outstanding):,.2f}</td></tr>
  </table>
  {f'<p class="meta" style="margin-top:16px">Notes: {_esc(inv.notes)}</p>' if inv.notes else ''}
  <p class="meta" style="margin-top:24px">Prepared by: {_esc(inv.created_by_name)}</p>
  {print_script}
</body></html>"""


def receipt_html(
    receipt: BillingReceipt,
    hospital: Hospital | None,
    patient: Patient | None,
    auto_print: bool = True,
) -> str:
    hosp_name = _esc(hospital.name if hospital else "Hospital")
    hosp_addr = _esc(hospital.address if hospital else "")
    hosp_phone = _esc(hospital.phone if hospital else "")
    hosp_email = _esc(hospital.email if hospital else "")
    method = receipt.payment_method.value if receipt.payment_method else "cash"
    if method == "bank_transfer":
        method_label = "Bank"
    else:
        method_label = method.replace("_", " ").title()
    print_script = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{_esc(receipt.receipt_number)}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 640px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #047857; margin: 0 0 4px; font-size: 22px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 4px; }}
  .box {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-top: 20px; }}
  .row {{ display: flex; justify-content: space-between; padding: 6px 0; font-size: 14px; border-bottom: 1px dashed #e2e8f0; }}
  .row:last-child {{ border-bottom: none; }}
  .amount {{ font-size: 28px; font-weight: 800; color: #047857; margin: 12px 0; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <h1>{hosp_name}</h1>
  <p class="meta">{hosp_addr}</p>
  <p class="meta">Phone: {hosp_phone} · Email: {hosp_email}</p>
  <hr style="border:none;border-top:2px solid #047857;margin:16px 0"/>
  <p><strong>Payment Receipt</strong></p>
  <p class="meta">Receipt No: {_esc(receipt.receipt_number)}</p>
  <p class="meta">Date: {receipt.payment_date.strftime('%d %b %Y') if receipt.payment_date else '—'}</p>
  <div class="box">
    <div class="row"><span>Patient</span><strong>{_esc(patient.name if patient else None)}</strong></div>
    <div class="row"><span>UHID</span><strong>{_esc(patient.uhid if patient else None)}</strong></div>
    <div class="row"><span>Payment Method</span><strong>{_esc(method_label)}</strong></div>
    <div class="row"><span>Reference No</span><strong>{_esc(receipt.reference_number)}</strong></div>
    <p class="amount">₹{float(receipt.amount):,.2f}</p>
    <p class="meta">Amount Received</p>
    <div class="row"><span>Collected By</span><strong>{_esc(receipt.collected_by_name)}</strong></div>
  </div>
  {f'<p class="meta" style="margin-top:16px">Notes: {_esc(receipt.notes)}</p>' if receipt.notes else ''}
  {print_script}
</body></html>"""


def patient_outstanding_for_invoice(db: Session, hospital_id: UUID, patient_id: UUID) -> float:
    return float(patient_ledger_totals(db, hospital_id, patient_id)["outstanding"])
