"""Invoice and receipt numbering sequences, line snapshots, and HTML rendering service.

Conforms to UltrionTech-Backend-Template modules/billing/services/ specification.
Replaces app.utils.invoices with target-native business logic and entities.
"""

from __future__ import annotations

from datetime import date
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


def _esc(s: str | None) -> str:
    return (s or "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def next_invoice_number(db: Session, hospital_id: UUID, year: int | None = None) -> str:
    """Generate sequential invoice number scoped to hospital and year."""
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
    """Generate sequential payment receipt number scoped to hospital and year."""
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
    all_charge_ids = {
        ln.charge_id for inv in invoices for ln in inv.lines if ln.charge_id
    }
    open_charge_ids: set = set()
    if all_charge_ids:
        open_charge_ids = {
            row[0]
            for row in db.query(BillingCharge.id)
            .filter(
                BillingCharge.id.in_(all_charge_ids),
                BillingCharge.status.in_(
                    [BillingChargeStatus.pending, BillingChargeStatus.partially_paid]
                ),
            )
            .all()
        }
    for inv in invoices:
        charge_ids = [ln.charge_id for ln in inv.lines if ln.charge_id]
        if not charge_ids:
            continue
        open_count = sum(1 for cid in charge_ids if cid in open_charge_ids)
        if open_count == 0:
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
        hsn = getattr(c, "hsn_sac_code", None) or "9993"
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
                "hsn_sac_code": getattr(ln, "hsn_sac_code", None) or "9993",
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


def invoice_html(
    inv: BillingInvoice,
    hospital: Hospital | None,
    patient: Patient | None,
    outstanding: float = 0.0,
    auto_print: bool = True,
) -> str:
    """Render full HTML document for a patient invoice with statutory GST compliance."""
    hosp_name = _esc(hospital.name if hospital else "Hospital")
    hosp_addr = _esc(hospital.address if hospital else "")
    hosp_phone = _esc(hospital.phone if hospital else "")
    hosp_email = _esc(hospital.email if hospital else "")
    hosp_gstin = _esc(getattr(hospital, "gstin", None) or "URP")
    hosp_pan = _esc(getattr(hospital, "pan", None) or "—")
    hosp_state = _esc(getattr(hospital, "state_code", None) or "—")

    p_name = _esc(patient.name if patient else None)
    p_uhid = _esc(patient.uhid if patient else None)
    p_phone = _esc(getattr(patient, "mobile", None) or getattr(patient, "phone", None))

    lines = sorted(inv.lines or [], key=lambda ln: ln.sort_order)
    rows = "".join(
        f"<tr><td>{_esc(SOURCE_CATEGORY_LABELS.get(ln.source_type, ln.source_type))}<br/>"
        f"<span style='color:#64748b;font-size:12px'>{_esc(ln.description)}</span></td>"
        f"<td style='text-align:center'>{_esc(getattr(ln, 'hsn_sac_code', None) or '9993')}</td>"
        f"<td class='num'>{ln.quantity:g}</td>"
        f"<td class='num'>₹{float(ln.rate):,.2f}</td>"
        f"<td class='num'>₹{float(ln.amount):,.2f}</td>"
        f"<td class='num'>{float(getattr(ln, 'gst_rate', 0.0) or 0.0):g}%</td>"
        f"<td class='num'>₹{float((getattr(ln, 'cgst_amount', 0.0) or 0.0) + (getattr(ln, 'sgst_amount', 0.0) or 0.0)):,.2f}</td></tr>"
        for ln in lines
    ) or "<tr><td colspan='7'>No lines</td></tr>"

    taxable = float(getattr(inv, "taxable_amount", 0.0) or (float(inv.subtotal) - float(inv.discount_amount)))
    cgst = float(getattr(inv, "cgst_amount", 0.0) or 0.0)
    sgst = float(getattr(inv, "sgst_amount", 0.0) or 0.0)
    igst = float(getattr(inv, "igst_amount", 0.0) or 0.0)

    print_script = (
        "<script>window.onload=function(){window.print();}</script>"
        if auto_print
        else ""
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{_esc(inv.invoice_number)}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 840px; margin: 32px auto; color: #0f172a; line-height: 1.4; }}
  h1 {{ color: #047857; margin: 0 0 4px; font-size: 22px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 3px; }}
  .tax-badge {{ display: inline-block; background: #ecfdf5; color: #065f46; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .grid {{ display: flex; justify-content: space-between; gap: 24px; margin: 16px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 7px 10px; text-align: left; font-size: 12px; vertical-align: top; }}
  th {{ background: #f8fafc; font-weight: 600; }}
  .num {{ text-align: right; white-space: nowrap; }}
  .totals {{ margin-top: 16px; width: 340px; margin-left: auto; }}
  .totals td {{ border: none; padding: 3px 0; font-size: 13px; }}
  .totals .grand {{ font-weight: 800; font-size: 15px; border-top: 2px solid #0f172a; padding-top: 6px; color: #047857; }}
  .signatures {{ display: flex; justify-content: space-between; margin-top: 48px; padding-top: 24px; }}
  .sig-box {{ border-top: 1px solid #94a3b8; width: 200px; text-align: center; font-size: 12px; color: #475569; padding-top: 6px; }}
  .stamp-box {{ width: 120px; height: 70px; border: 1px dashed #cbd5e1; display: flex; align-items: center; justify-content: center; color: #94a3b8; font-size: 11px; margin-bottom: 6px; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <div style="display:flex; justify-content:space-between; align-items:flex-start;">
    <div>
      <h1>{hosp_name}</h1>
      <p class="meta">{hosp_addr}</p>
      <p class="meta">Phone: {hosp_phone} · Email: {hosp_email}</p>
    </div>
    <div style="text-align:right;">
      <span class="tax-badge">TAX INVOICE</span>
      <p class="meta" style="margin-top:6px"><strong>GSTIN:</strong> {hosp_gstin}</p>
      <p class="meta"><strong>PAN:</strong> {hosp_pan} · <strong>State:</strong> {hosp_state}</p>
    </div>
  </div>
  <hr style="border:none;border-top:2px solid #047857;margin:14px 0"/>
  <div class="grid">
    <div>
      <p><strong>Invoice Details</strong></p>
      <p class="meta">Invoice No: <strong>{_esc(inv.invoice_number)}</strong></p>
      <p class="meta">Date: {inv.invoice_date.strftime('%d %b %Y') if inv.invoice_date else '—'}</p>
      <p class="meta">Status: <span style="text-transform:uppercase;font-weight:600;">{_esc(inv.status.value if inv.status else None)}</span></p>
    </div>
    <div>
      <p><strong>Billed To (Patient)</strong></p>
      <p class="meta">Name: <strong>{p_name}</strong></p>
      <p class="meta">UHID: <strong>{p_uhid}</strong></p>
      {f'<p class="meta">Phone: {p_phone}</p>' if p_phone != '—' else ''}
    </div>
  </div>
  <table>
    <thead><tr>
      <th>Description</th>
      <th style="text-align:center">HSN/SAC</th>
      <th class="num">Qty</th>
      <th class="num">Rate</th>
      <th class="num">Taxable</th>
      <th class="num">GST %</th>
      <th class="num">Tax</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <table class="totals">
    <tr><td>Gross Charges</td><td class="num">₹{float(inv.subtotal):,.2f}</td></tr>
    <tr><td>Discount / Concession</td><td class="num">₹{float(inv.discount_amount):,.2f}</td></tr>
    <tr><td>Taxable Value</td><td class="num">₹{taxable:,.2f}</td></tr>
    {f'<tr><td>CGST</td><td class="num">₹{cgst:,.2f}</td></tr>' if cgst > 0 else ''}
    {f'<tr><td>SGST</td><td class="num">₹{sgst:,.2f}</td></tr>' if sgst > 0 else ''}
    {f'<tr><td>IGST</td><td class="num">₹{igst:,.2f}</td></tr>' if igst > 0 else ''}
    <tr><td>Total Tax</td><td class="num">₹{float(inv.tax_amount):,.2f}</td></tr>
    <tr class="grand"><td>Grand Total</td><td class="num">₹{float(inv.grand_total):,.2f}</td></tr>
    <tr><td>Ledger Outstanding</td><td class="num">₹{float(outstanding):,.2f}</td></tr>
  </table>
  {f'<p class="meta" style="margin-top:16px">Notes: {_esc(inv.notes)}</p>' if inv.notes else ''}
  
  <div class="signatures">
    <div>
      <div class="stamp-box">Hospital Seal</div>
      <div class="sig-box">Hospital Stamp</div>
    </div>
    <div>
      <div style="height:70px;"></div>
      <div class="sig-box">Authorized Signatory<br/><span style="font-size:10px;">Prepared by: {_esc(inv.created_by_name)}</span></div>
    </div>
  </div>
  {print_script}
</body></html>"""


def receipt_html(
    receipt: BillingReceipt,
    hospital: Hospital | None,
    patient: Patient | None,
    auto_print: bool = True,
) -> str:
    """Render full HTML document for a payment receipt."""
    hosp_name = _esc(hospital.name if hospital else "Hospital")
    hosp_addr = _esc(hospital.address if hospital else "")
    hosp_phone = _esc(hospital.phone if hospital else "")
    hosp_email = _esc(hospital.email if hospital else "")
    hosp_gstin = _esc(getattr(hospital, "gstin", None) or "URP")

    method = receipt.payment_method.value if receipt.payment_method else "cash"
    if method == "bank_transfer":
        method_label = "Bank Transfer"
    else:
        method_label = method.replace("_", " ").title()
    print_script = (
        "<script>window.onload=function(){window.print();}</script>"
        if auto_print
        else ""
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{_esc(receipt.receipt_number)}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 640px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #047857; margin: 0 0 4px; font-size: 22px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 4px; }}
  .box {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-top: 16px; }}
  .row {{ display: flex; justify-content: space-between; padding: 6px 0; font-size: 13px; border-bottom: 1px dashed #e2e8f0; }}
  .row:last-child {{ border-bottom: none; }}
  .amount {{ font-size: 28px; font-weight: 800; color: #047857; margin: 12px 0 4px; }}
  .signatures {{ display: flex; justify-content: space-between; margin-top: 36px; padding-top: 16px; }}
  .sig-box {{ border-top: 1px solid #94a3b8; width: 180px; text-align: center; font-size: 11px; color: #475569; padding-top: 4px; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <div style="display:flex; justify-content:space-between; align-items:flex-start;">
    <div>
      <h1>{hosp_name}</h1>
      <p class="meta">{hosp_addr}</p>
      <p class="meta">Phone: {hosp_phone} · Email: {hosp_email}</p>
    </div>
    <div style="text-align:right;">
      <span style="font-size:11px;font-weight:600;background:#ecfdf5;color:#065f46;padding:2px 8px;border-radius:4px;">MONEY RECEIPT</span>
      <p class="meta" style="margin-top:4px;">GSTIN: {hosp_gstin}</p>
    </div>
  </div>
  <hr style="border:none;border-top:2px solid #047857;margin:14px 0"/>
  <div style="display:flex;justify-content:space-between;">
    <div>
      <p><strong>Receipt No:</strong> {_esc(receipt.receipt_number)}</p>
      <p class="meta">Date: {receipt.payment_date.strftime('%d %b %Y') if receipt.payment_date else '—'}</p>
    </div>
    <div style="text-align:right;">
      <p><strong>Status:</strong> <span style="text-transform:uppercase;">{_esc(receipt.status.value if receipt.status else 'issued')}</span></p>
    </div>
  </div>
  <div class="box">
    <div class="row"><span>Patient Name</span><strong>{_esc(patient.name if patient else None)}</strong></div>
    <div class="row"><span>Patient UHID</span><strong>{_esc(patient.uhid if patient else None)}</strong></div>
    <div class="row"><span>Payment Method</span><strong>{_esc(method_label)}</strong></div>
    <div class="row"><span>Reference / Transaction ID</span><strong>{_esc(receipt.reference_number)}</strong></div>
    <p class="amount">₹{float(receipt.amount):,.2f}</p>
    <p class="meta">Amount Received</p>
    <div class="row"><span>Collected By</span><strong>{_esc(receipt.collected_by_name)}</strong></div>
  </div>
  {f'<p class="meta" style="margin-top:14px">Notes: {_esc(receipt.notes)}</p>' if receipt.notes else ''}
  
  <div class="signatures">
    <div class="sig-box">Patient Signature</div>
    <div class="sig-box">Authorized Cashier<br/><span style="font-size:10px;">{_esc(receipt.collected_by_name)}</span></div>
  </div>
  {print_script}
</body></html>"""


def refund_html(
    refund: BillingRefund,
    hospital: Hospital | None,
    patient: Patient | None,
    auto_print: bool = True,
) -> str:
    """Render full HTML document for a refund payment voucher."""
    hosp_name = _esc(hospital.name if hospital else "Hospital")
    hosp_addr = _esc(hospital.address if hospital else "")
    hosp_phone = _esc(hospital.phone if hospital else "")
    hosp_email = _esc(hospital.email if hospital else "")
    method = refund.refund_method.value if refund.refund_method else "cash"
    method_label = method.replace("_", " ").title()

    print_script = (
        "<script>window.onload=function(){window.print();}</script>"
        if auto_print
        else ""
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{_esc(refund.refund_number)}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 640px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #dc2626; margin: 0 0 4px; font-size: 22px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 4px; }}
  .box {{ border: 1px solid #fecaca; background: #fff5f5; border-radius: 8px; padding: 16px; margin-top: 16px; }}
  .row {{ display: flex; justify-content: space-between; padding: 6px 0; font-size: 13px; border-bottom: 1px dashed #fca5a5; }}
  .row:last-child {{ border-bottom: none; }}
  .amount {{ font-size: 28px; font-weight: 800; color: #dc2626; margin: 12px 0 4px; }}
  .signatures {{ display: flex; justify-content: space-between; margin-top: 36px; padding-top: 16px; }}
  .sig-box {{ border-top: 1px solid #94a3b8; width: 180px; text-align: center; font-size: 11px; color: #475569; padding-top: 4px; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <div style="display:flex; justify-content:space-between; align-items:flex-start;">
    <div>
      <h1>{hosp_name}</h1>
      <p class="meta">{hosp_addr}</p>
      <p class="meta">Phone: {hosp_phone} · Email: {hosp_email}</p>
    </div>
    <div style="text-align:right;">
      <span style="font-size:11px;font-weight:600;background:#fee2e2;color:#991b1b;padding:2px 8px;border-radius:4px;">REFUND VOUCHER</span>
      <p class="meta" style="margin-top:4px;">Voucher: {_esc(refund.refund_number)}</p>
    </div>
  </div>
  <hr style="border:none;border-top:2px solid #dc2626;margin:14px 0"/>
  <div class="box">
    <div class="row"><span>Patient Name</span><strong>{_esc(patient.name if patient else None)}</strong></div>
    <div class="row"><span>Patient UHID</span><strong>{_esc(patient.uhid if patient else None)}</strong></div>
    <div class="row"><span>Refund Date</span><strong>{refund.refund_date.strftime('%d %b %Y') if refund.refund_date else '—'}</strong></div>
    <div class="row"><span>Refund Mode</span><strong>{_esc(method_label)}</strong></div>
    <div class="row"><span>Reason</span><strong>{_esc(refund.reason)}</strong></div>
    <p class="amount">₹{float(refund.amount):,.2f}</p>
    <p class="meta">Total Amount Refunded</p>
    <div class="row"><span>Approved By</span><strong>{_esc(refund.approved_by_name)}</strong></div>
    <div class="row"><span>Processed By</span><strong>{_esc(refund.processed_by_name)}</strong></div>
  </div>
  
  <div class="signatures">
    <div class="sig-box">Receiver Signature</div>
    <div class="sig-box">Approved Signatory<br/><span style="font-size:10px;">Finance / Admin</span></div>
  </div>
  {print_script}
</body></html>"""


def patient_outstanding_for_invoice(
    db: Session, hospital_id: UUID, patient_id: UUID
) -> float:
    """Compute patient ledger outstanding balance."""
    return float(patient_ledger_totals(db, hospital_id, patient_id)["outstanding"])
