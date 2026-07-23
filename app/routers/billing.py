from datetime import date, datetime, time, timezone
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    BillingCharge,
    BillingChargeStatus,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPayment,
    BillingReceipt,
    BillingReceiptStatus,
    BillingSourceType,
    Hospital,
    Patient,
)
from app.schemas_billing import (
    BillingChargeCreate,
    BillingChargeResponse,
    BillingChargeUpdate,
    BillingDashboardResponse,
    BillingInvoiceCreate,
    BillingInvoiceResponse,
    BillingPaymentCreate,
    BillingPaymentResponse,
    BillingReceiptCreate,
    BillingReceiptResponse,
    LedgerEntry,
    PatientFinancialSummary,
    PatientLedgerResponse,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user
from app.utils.billing import (
    build_ledger_entries,
    cancel_charge_for_source,
    charge_to_dict,
    compute_net,
    create_payment,
    ensure_charge,
    patient_ledger_totals,
    payment_to_dict,
)
from app.utils.invoices import (
    create_invoice_from_charges,
    invoice_html,
    invoice_to_dict,
    issue_receipt,
    issue_receipt_for_payment,
    patient_outstanding_for_invoice,
    receipt_html,
    receipt_to_dict,
    refresh_invoice_paid_status,
)

router = APIRouter(prefix="/billing", tags=["billing"])


def _actor(user: dict) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _get_patient(db: Session, patient_id: UUID, hospital_id: UUID) -> Patient:
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    return patient


def _payment_response(db: Session, pay: BillingPayment, patient: Patient | None = None) -> BillingPaymentResponse:
    data = payment_to_dict(pay, patient)
    receipt = (
        db.query(BillingReceipt)
        .filter(
            BillingReceipt.hospital_id == pay.hospital_id,
            BillingReceipt.payment_id == pay.id,
            BillingReceipt.status != BillingReceiptStatus.cancelled,
        )
        .first()
    )
    data["receipt_id"] = receipt.id if receipt else None
    data["receipt_number"] = receipt.receipt_number if receipt else None
    return BillingPaymentResponse.model_validate(data)


def _get_invoice(db: Session, invoice_id: UUID, hospital_id: UUID) -> BillingInvoice:
    inv = (
        db.query(BillingInvoice)
        .options(joinedload(BillingInvoice.lines), joinedload(BillingInvoice.patient))
        .filter(BillingInvoice.id == invoice_id, BillingInvoice.hospital_id == hospital_id)
        .first()
    )
    if not inv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return inv


def _get_receipt(db: Session, receipt_id: UUID, hospital_id: UUID) -> BillingReceipt:
    row = (
        db.query(BillingReceipt)
        .options(joinedload(BillingReceipt.patient))
        .filter(BillingReceipt.id == receipt_id, BillingReceipt.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
    return row


def _html_response(html: str, filename: str, download: bool) -> StreamingResponse:
    disposition = "attachment" if download else "inline"
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


# ── Dashboard ──────────────────────────────────────────────────────────────────
@router.get("/dashboard", response_model=BillingDashboardResponse)
def dashboard(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = datetime.now(timezone.utc).date()
    day_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
    day_end = datetime.combine(today, time.max, tzinfo=timezone.utc)

    todays_charges = (
        db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
            BillingCharge.created_at >= day_start,
            BillingCharge.created_at <= day_end,
        )
        .scalar()
        or 0
    )
    todays_collections = (
        db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
        .filter(
            BillingPayment.hospital_id == hospital_id,
            BillingPayment.payment_date == today,
        )
        .scalar()
        or 0
    )

    total_net = (
        db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .scalar()
        or 0
    )
    total_paid = (
        db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
        .filter(BillingPayment.hospital_id == hospital_id)
        .scalar()
        or 0
    )
    pending_count = (
        db.query(func.count(BillingCharge.id))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status.in_(
                [BillingChargeStatus.pending, BillingChargeStatus.partially_paid]
            ),
        )
        .scalar()
        or 0
    )

    todays_ot_revenue = (
        db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
            BillingCharge.source_type == BillingSourceType.ot,
            BillingCharge.created_at >= day_start,
            BillingCharge.created_at <= day_end,
        )
        .scalar()
        or 0
    )
    todays_ipd_revenue = (
        db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
            BillingCharge.source_type.in_([BillingSourceType.admission, BillingSourceType.bed]),
            BillingCharge.created_at >= day_start,
            BillingCharge.created_at <= day_end,
        )
        .scalar()
        or 0
    )

    category_keys = [
        BillingSourceType.consultation,
        BillingSourceType.laboratory,
        BillingSourceType.radiology,
        BillingSourceType.admission,
        BillingSourceType.bed,
        BillingSourceType.ot,
    ]
    outstanding_by_category: dict[str, float] = {k.value: 0.0 for k in category_keys}
    open_charges = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status.in_(
                [BillingChargeStatus.pending, BillingChargeStatus.partially_paid]
            ),
        )
        .all()
    )
    for c in open_charges:
        key = c.source_type.value if c.source_type else "other"
        due = round(max(0.0, float(c.net_amount or 0) - float(c.amount_paid or 0)), 2)
        if key in outstanding_by_category:
            outstanding_by_category[key] = round(outstanding_by_category[key] + due, 2)
        else:
            outstanding_by_category[key] = due

    today_invoice_count = (
        db.query(func.count(BillingInvoice.id))
        .filter(
            BillingInvoice.hospital_id == hospital_id,
            BillingInvoice.invoice_date == today,
            BillingInvoice.status != BillingInvoiceStatus.cancelled,
        )
        .scalar()
        or 0
    )
    today_receipt_count = (
        db.query(func.count(BillingReceipt.id))
        .filter(
            BillingReceipt.hospital_id == hospital_id,
            BillingReceipt.payment_date == today,
            BillingReceipt.status != BillingReceiptStatus.cancelled,
        )
        .scalar()
        or 0
    )
    total_invoiced = (
        db.query(func.coalesce(func.sum(BillingInvoice.grand_total), 0.0))
        .filter(
            BillingInvoice.hospital_id == hospital_id,
            BillingInvoice.status != BillingInvoiceStatus.cancelled,
        )
        .scalar()
        or 0
    )
    total_collected = (
        db.query(func.coalesce(func.sum(BillingReceipt.amount), 0.0))
        .filter(
            BillingReceipt.hospital_id == hospital_id,
            BillingReceipt.status != BillingReceiptStatus.cancelled,
        )
        .scalar()
        or 0
    )

    recent_charges = (
        db.query(BillingCharge)
        .options(joinedload(BillingCharge.patient))
        .filter(BillingCharge.hospital_id == hospital_id)
        .order_by(BillingCharge.created_at.desc())
        .limit(15)
        .all()
    )
    recent_payments = (
        db.query(BillingPayment)
        .options(joinedload(BillingPayment.patient))
        .filter(BillingPayment.hospital_id == hospital_id)
        .order_by(BillingPayment.created_at.desc())
        .limit(15)
        .all()
    )
    return BillingDashboardResponse(
        todays_charges=round(float(todays_charges), 2),
        todays_collections=round(float(todays_collections), 2),
        outstanding_total=round(max(0.0, float(total_net) - float(total_paid)), 2),
        pending_charges_count=int(pending_count),
        todays_ot_revenue=round(float(todays_ot_revenue), 2),
        todays_ipd_revenue=round(float(todays_ipd_revenue), 2),
        outstanding_by_category=outstanding_by_category,
        today_invoice_count=int(today_invoice_count),
        today_receipt_count=int(today_receipt_count),
        total_invoiced=round(float(total_invoiced), 2),
        total_collected=round(float(total_collected), 2),
        recent_charges=[BillingChargeResponse.model_validate(charge_to_dict(c)) for c in recent_charges],
        recent_payments=[_payment_response(db, p) for p in recent_payments],
    )


# ── Charges ────────────────────────────────────────────────────────────────────
@router.get("/charges", response_model=list[BillingChargeResponse])
def list_charges(
    patient_id: UUID | None = Query(default=None),
    status_filter: BillingChargeStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(BillingCharge)
        .options(joinedload(BillingCharge.patient))
        .filter(BillingCharge.hospital_id == hospital_id)
    )
    if patient_id:
        q = q.filter(BillingCharge.patient_id == patient_id)
    if status_filter:
        q = q.filter(BillingCharge.status == status_filter)
    rows = q.order_by(BillingCharge.created_at.desc()).limit(300).all()
    return [BillingChargeResponse.model_validate(charge_to_dict(c)) for c in rows]


@router.post("/charges", response_model=BillingChargeResponse, status_code=status.HTTP_201_CREATED)
def create_manual_charge(
    payload: BillingChargeCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = _get_patient(db, payload.patient_id, hospital_id)
    charge = ensure_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient.id,
        source_type=payload.source_type,
        source_id=payload.source_id,
        description=payload.description,
        charge_amount=payload.charge_amount,
        discount_amount=payload.discount_amount,
        discount_percent=payload.discount_percent,
        notes=payload.notes,
        created_by_name=_actor(user),
    )
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="billing_charge",
        entity_id=charge.id,
        summary=f"Charge {charge.description} ₹{charge.net_amount} for {patient.name}",
    )
    db.commit()
    db.refresh(charge)
    return BillingChargeResponse.model_validate(charge_to_dict(charge, patient))


@router.put("/charges/{charge_id}", response_model=BillingChargeResponse)
def update_charge(
    charge_id: UUID,
    payload: BillingChargeUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    charge = (
        db.query(BillingCharge)
        .options(joinedload(BillingCharge.patient))
        .filter(BillingCharge.id == charge_id, BillingCharge.hospital_id == hospital_id)
        .first()
    )
    if not charge:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Charge not found")
    data = payload.model_dump(exclude_unset=True)
    if "description" in data and data["description"]:
        charge.description = data["description"].strip()
    if "notes" in data:
        charge.notes = data["notes"]
    if "status" in data and data["status"] is not None:
        charge.status = data["status"]
    if "discount_amount" in data or "discount_percent" in data:
        disc_amt = data.get("discount_amount", charge.discount_amount)
        disc_pct = data.get("discount_percent", charge.discount_percent)
        disc, net = compute_net(charge.charge_amount, disc_amt or 0, disc_pct)
        charge.discount_amount = disc
        charge.discount_percent = disc_pct
        charge.net_amount = net
        if charge.amount_paid > net:
            charge.amount_paid = net
        if charge.status != BillingChargeStatus.cancelled:
            if net <= 0 or charge.amount_paid >= net:
                charge.status = BillingChargeStatus.paid
            elif charge.amount_paid > 0:
                charge.status = BillingChargeStatus.partially_paid
            else:
                charge.status = BillingChargeStatus.pending
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="billing_charge",
        entity_id=charge.id,
        summary=f"Updated charge {charge.description}",
    )
    db.commit()
    db.refresh(charge)
    return BillingChargeResponse.model_validate(charge_to_dict(charge))


@router.post("/charges/{charge_id}/cancel", response_model=BillingChargeResponse)
def cancel_charge(
    charge_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    charge = (
        db.query(BillingCharge)
        .options(joinedload(BillingCharge.patient))
        .filter(BillingCharge.id == charge_id, BillingCharge.hospital_id == hospital_id)
        .first()
    )
    if not charge:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Charge not found")
    charge.status = BillingChargeStatus.cancelled
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="billing_charge",
        entity_id=charge.id,
        summary=f"Cancelled charge {charge.description}",
    )
    db.commit()
    return BillingChargeResponse.model_validate(charge_to_dict(charge))


# ── Payments ───────────────────────────────────────────────────────────────────
@router.get("/payments", response_model=list[BillingPaymentResponse])
def list_payments(
    patient_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(BillingPayment)
        .options(joinedload(BillingPayment.patient))
        .filter(BillingPayment.hospital_id == hospital_id)
    )
    if patient_id:
        q = q.filter(BillingPayment.patient_id == patient_id)
    rows = q.order_by(BillingPayment.created_at.desc()).limit(300).all()
    return [_payment_response(db, p) for p in rows]


@router.post("/payments", response_model=BillingPaymentResponse, status_code=status.HTTP_201_CREATED)
def record_payment(
    payload: BillingPaymentCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = _get_patient(db, payload.patient_id, hospital_id)
    pay = create_payment(
        db,
        hospital_id=hospital_id,
        patient_id=patient.id,
        amount=payload.amount,
        payment_date=payload.payment_date or date.today(),
        payment_method=payload.payment_method,
        notes=payload.notes.strip() if payload.notes else None,
        received_by_name=_actor(user),
    )
    issue_receipt_for_payment(
        db,
        pay,
        linked_invoice_id=payload.linked_invoice_id,
        reference_number=payload.reference_number,
    )
    refresh_invoice_paid_status(db, hospital_id, patient.id)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="billing_payment",
        entity_id=pay.id,
        summary=f"Payment ₹{pay.amount} ({pay.payment_method.value}) for {patient.name}",
    )
    db.commit()
    db.refresh(pay)
    return _payment_response(db, pay, patient)


# ── Invoices ───────────────────────────────────────────────────────────────────
@router.get("/invoices", response_model=list[BillingInvoiceResponse])
def list_invoices(
    patient_id: UUID | None = Query(default=None),
    status_filter: BillingInvoiceStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(BillingInvoice)
        .options(joinedload(BillingInvoice.lines), joinedload(BillingInvoice.patient))
        .filter(BillingInvoice.hospital_id == hospital_id)
    )
    if patient_id:
        q = q.filter(BillingInvoice.patient_id == patient_id)
    if status_filter:
        q = q.filter(BillingInvoice.status == status_filter)
    rows = q.order_by(BillingInvoice.created_at.desc()).limit(300).all()
    return [BillingInvoiceResponse.model_validate(invoice_to_dict(r)) for r in rows]


@router.get("/invoices/{invoice_id}", response_model=BillingInvoiceResponse)
def get_invoice(
    invoice_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    inv = _get_invoice(db, invoice_id, hospital_id)
    return BillingInvoiceResponse.model_validate(invoice_to_dict(inv))


@router.post("/invoices", response_model=BillingInvoiceResponse, status_code=status.HTTP_201_CREATED)
def create_invoice(
    payload: BillingInvoiceCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = _get_patient(db, payload.patient_id, hospital_id)
    try:
        inv = create_invoice_from_charges(
            db,
            hospital_id=hospital_id,
            patient_id=patient.id,
            charge_ids=payload.charge_ids,
            invoice_date=payload.invoice_date,
            tax_amount=payload.tax_amount,
            notes=payload.notes.strip() if payload.notes else None,
            created_by_name=_actor(user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="billing_invoice",
        entity_id=inv.id,
        summary=f"Invoice {inv.invoice_number} ₹{inv.grand_total} for {patient.name}",
    )
    db.commit()
    inv = _get_invoice(db, inv.id, hospital_id)
    return BillingInvoiceResponse.model_validate(invoice_to_dict(inv, patient))


@router.post("/invoices/{invoice_id}/cancel", response_model=BillingInvoiceResponse)
def cancel_invoice(
    invoice_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    inv = _get_invoice(db, invoice_id, hospital_id)
    if inv.status == BillingInvoiceStatus.cancelled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoice already cancelled")
    inv.status = BillingInvoiceStatus.cancelled
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="billing_invoice",
        entity_id=inv.id,
        summary=f"Cancelled invoice {inv.invoice_number}",
    )
    db.commit()
    inv = _get_invoice(db, invoice_id, hospital_id)
    return BillingInvoiceResponse.model_validate(invoice_to_dict(inv))


@router.get("/invoices/{invoice_id}/print")
def print_invoice(
    invoice_id: UUID,
    download: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    inv = _get_invoice(db, invoice_id, hospital_id)
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    outstanding = patient_outstanding_for_invoice(db, hospital_id, inv.patient_id)
    html = invoice_html(inv, hospital, inv.patient, outstanding=outstanding, auto_print=not download)
    return _html_response(html, f"{inv.invoice_number}.html", download)


@router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(
    invoice_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """HTML export suitable for browser Print → Save as PDF (same pattern as lab reports)."""
    inv = _get_invoice(db, invoice_id, hospital_id)
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    outstanding = patient_outstanding_for_invoice(db, hospital_id, inv.patient_id)
    html = invoice_html(inv, hospital, inv.patient, outstanding=outstanding, auto_print=False)
    return _html_response(html, f"{inv.invoice_number}.html", download=True)


# ── Receipts ───────────────────────────────────────────────────────────────────
@router.get("/receipts", response_model=list[BillingReceiptResponse])
def list_receipts(
    patient_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(BillingReceipt)
        .options(joinedload(BillingReceipt.patient))
        .filter(BillingReceipt.hospital_id == hospital_id)
    )
    if patient_id:
        q = q.filter(BillingReceipt.patient_id == patient_id)
    rows = q.order_by(BillingReceipt.created_at.desc()).limit(300).all()
    return [BillingReceiptResponse.model_validate(receipt_to_dict(r)) for r in rows]


@router.get("/receipts/{receipt_id}", response_model=BillingReceiptResponse)
def get_receipt(
    receipt_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = _get_receipt(db, receipt_id, hospital_id)
    return BillingReceiptResponse.model_validate(receipt_to_dict(row))


@router.post("/receipts", response_model=BillingReceiptResponse, status_code=status.HTTP_201_CREATED)
def create_receipt(
    payload: BillingReceiptCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = _get_patient(db, payload.patient_id, hospital_id)
    payment_id = None
    if payload.create_payment:
        pay = create_payment(
            db,
            hospital_id=hospital_id,
            patient_id=patient.id,
            amount=payload.amount,
            payment_date=payload.payment_date or date.today(),
            payment_method=payload.payment_method,
            notes=payload.notes.strip() if payload.notes else None,
            received_by_name=_actor(user),
        )
        payment_id = pay.id
        try:
            receipt = issue_receipt_for_payment(
                db,
                pay,
                linked_invoice_id=payload.linked_invoice_id,
                reference_number=payload.reference_number,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        refresh_invoice_paid_status(db, hospital_id, patient.id)
    else:
        try:
            receipt = issue_receipt(
                db,
                hospital_id=hospital_id,
                patient_id=patient.id,
                amount=payload.amount,
                payment_date=payload.payment_date or date.today(),
                payment_method=payload.payment_method,
                collected_by_name=_actor(user),
                reference_number=payload.reference_number,
                notes=payload.notes.strip() if payload.notes else None,
                payment_id=payment_id,
                linked_invoice_id=payload.linked_invoice_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="billing_receipt",
        entity_id=receipt.id,
        summary=f"Receipt {receipt.receipt_number} ₹{receipt.amount} for {patient.name}",
    )
    db.commit()
    row = _get_receipt(db, receipt.id, hospital_id)
    return BillingReceiptResponse.model_validate(receipt_to_dict(row, patient))


@router.post("/receipts/{receipt_id}/cancel", response_model=BillingReceiptResponse)
def cancel_receipt(
    receipt_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = _get_receipt(db, receipt_id, hospital_id)
    if row.status == BillingReceiptStatus.cancelled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Receipt already cancelled")
    row.status = BillingReceiptStatus.cancelled
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="billing_receipt",
        entity_id=row.id,
        summary=f"Cancelled receipt {row.receipt_number}",
    )
    db.commit()
    row = _get_receipt(db, receipt_id, hospital_id)
    return BillingReceiptResponse.model_validate(receipt_to_dict(row))


@router.get("/receipts/{receipt_id}/print")
def print_receipt(
    receipt_id: UUID,
    download: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = _get_receipt(db, receipt_id, hospital_id)
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    html = receipt_html(row, hospital, row.patient, auto_print=not download)
    return _html_response(html, f"{row.receipt_number}.html", download)


@router.get("/receipts/{receipt_id}/pdf")
def download_receipt_pdf(
    receipt_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = _get_receipt(db, receipt_id, hospital_id)
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    html = receipt_html(row, hospital, row.patient, auto_print=False)
    return _html_response(html, f"{row.receipt_number}.html", download=True)


# ── Patient ledger / summary ───────────────────────────────────────────────────
@router.get("/patients/{patient_id}/ledger", response_model=PatientLedgerResponse)
def patient_ledger(
    patient_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = _get_patient(db, patient_id, hospital_id)
    totals = patient_ledger_totals(db, hospital_id, patient_id)
    charges = (
        db.query(BillingCharge)
        .options(joinedload(BillingCharge.patient))
        .filter(BillingCharge.hospital_id == hospital_id, BillingCharge.patient_id == patient_id)
        .order_by(BillingCharge.created_at.desc())
        .all()
    )
    payments = (
        db.query(BillingPayment)
        .options(joinedload(BillingPayment.patient))
        .filter(BillingPayment.hospital_id == hospital_id, BillingPayment.patient_id == patient_id)
        .order_by(BillingPayment.created_at.desc())
        .all()
    )
    invoices = (
        db.query(BillingInvoice)
        .options(joinedload(BillingInvoice.lines), joinedload(BillingInvoice.patient))
        .filter(BillingInvoice.hospital_id == hospital_id, BillingInvoice.patient_id == patient_id)
        .order_by(BillingInvoice.created_at.desc())
        .all()
    )
    receipts = (
        db.query(BillingReceipt)
        .options(joinedload(BillingReceipt.patient))
        .filter(BillingReceipt.hospital_id == hospital_id, BillingReceipt.patient_id == patient_id)
        .order_by(BillingReceipt.created_at.desc())
        .all()
    )
    entries = [LedgerEntry.model_validate(e) for e in build_ledger_entries(db, hospital_id, patient_id)]
    return PatientLedgerResponse(
        patient_id=patient.id,
        patient_name=patient.name,
        patient_uhid=patient.uhid,
        total_charges=totals["total_charges"],
        total_paid=totals["total_paid"],
        outstanding=totals["outstanding"],
        charge_count=totals["charge_count"],
        payment_count=totals["payment_count"],
        charges=[BillingChargeResponse.model_validate(charge_to_dict(c, patient)) for c in charges],
        payments=[_payment_response(db, p, patient) for p in payments],
        invoices=[BillingInvoiceResponse.model_validate(invoice_to_dict(i, patient)) for i in invoices],
        receipts=[BillingReceiptResponse.model_validate(receipt_to_dict(r, patient)) for r in receipts],
        entries=entries,
    )


@router.get("/patients/{patient_id}/summary", response_model=PatientFinancialSummary)
def patient_summary(
    patient_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    _get_patient(db, patient_id, hospital_id)
    totals = patient_ledger_totals(db, hospital_id, patient_id)
    entries = build_ledger_entries(db, hospital_id, patient_id)[:8]
    return PatientFinancialSummary(
        patient_id=patient_id,
        total_charges=totals["total_charges"],
        total_paid=totals["total_paid"],
        outstanding=totals["outstanding"],
        recent_entries=[LedgerEntry.model_validate(e) for e in entries],
    )


# Re-export helpers used by other routers (avoid circular imports via utils)
__all__ = ["router", "ensure_charge", "cancel_charge_for_source"]
