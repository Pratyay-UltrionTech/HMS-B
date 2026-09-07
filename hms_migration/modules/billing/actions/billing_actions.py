"""Business actions for Billing domain.

Conforms to UltrionTech-Backend-Template modules/billing/actions/ specification.
Implements charge workflows, payments, invoice snapshots, receipts, and patient ledgers.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.billing.contracts.billing_contracts import (
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
from hms_migration.modules.billing.db.billing_repository import BillingRepository
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingReceipt,
    BillingReceiptStatus,
    BillingSourceType,
)
from hms_migration.modules.billing.services.billing_service import (
    build_ledger_entries,
    charge_to_dict,
    compute_net,
    create_payment,
    ensure_charge,
    patient_ledger_totals,
    payment_to_dict,
)
from hms_migration.modules.billing.services.invoice_service import (
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
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.audit.service import write_audit_log


def _actor(user: dict[str, Any]) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _get_patient_or_404(repo: BillingRepository, patient_id: UUID) -> Patient:
    patient = repo.get_patient(patient_id)
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    return patient


def _payment_response_dto(
    db: Session,
    pay: BillingPayment,
    patient: Patient | None = None,
    *,
    receipt_id: UUID | None = None,
    receipt_number: str | None = None,
) -> BillingPaymentResponse:
    d = payment_to_dict(pay, patient)
    if receipt_id and receipt_number:
        d["receipt_id"] = receipt_id
        d["receipt_number"] = receipt_number
    else:
        rcpt = (
            db.query(BillingReceipt)
            .filter(
                BillingReceipt.hospital_id == pay.hospital_id,
                BillingReceipt.payment_id == pay.id,
                BillingReceipt.status != BillingReceiptStatus.cancelled,
            )
            .first()
        )
        if rcpt:
            d["receipt_id"] = rcpt.id
            d["receipt_number"] = rcpt.receipt_number
    return BillingPaymentResponse.model_validate(d)


# ── Charge Actions ──────────────────────────────────────────────────────────

class ListChargesAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        status: BillingChargeStatus | None = None,
        source_type: BillingSourceType | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        search: str | None = None,
    ) -> list[BillingChargeResponse]:
        charges = self.repo.list_charges(
            patient_id=patient_id,
            status=status,
            source_type=source_type,
            from_date=from_date,
            to_date=to_date,
            search=search,
        )
        return [BillingChargeResponse.model_validate(charge_to_dict(c)) for c in charges]


class CreateChargeAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingChargeCreate, user: dict[str, Any]) -> BillingChargeResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        row = ensure_charge(
            self.db,
            hospital_id=self.repo.hospital_id,
            patient_id=payload.patient_id,
            source_type=payload.source_type,
            source_id=payload.source_id,
            description=payload.description,
            charge_amount=payload.charge_amount,
            discount_amount=payload.discount_amount,
            discount_percent=payload.discount_percent,
            notes=payload.notes,
            created_by_name=_actor(user),
        )
        self.db.commit()
        self.db.refresh(row)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="billing_charge",
            entity_id=row.id,
            summary=f"Charge ₹{row.net_amount:.2f} created for {patient.name} ({row.description[:40]})",
        )
        self.db.commit()
        return BillingChargeResponse.model_validate(charge_to_dict(row, patient))


class UpdateChargeAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, charge_id: UUID, payload: BillingChargeUpdate, user: dict[str, Any]) -> BillingChargeResponse:
        charge = self.repo.get_charge_by_id(charge_id)
        if not charge:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Charge not found")
        if charge.status == BillingChargeStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot edit a cancelled charge")

        if payload.description is not None:
            charge.description = payload.description.strip()[:512]
        if payload.notes is not None:
            charge.notes = payload.notes

        disc_amt = payload.discount_amount if payload.discount_amount is not None else float(charge.discount_amount or 0)
        disc_pct = payload.discount_percent if payload.discount_percent is not None else charge.discount_percent
        disc, net = compute_net(float(charge.charge_amount or 0), disc_amt, disc_pct)
        charge.discount_amount = disc
        charge.discount_percent = disc_pct
        charge.net_amount = net

        if payload.status is not None:
            charge.status = payload.status
        else:
            paid = float(charge.amount_paid or 0)
            if net <= 0 or paid >= net:
                charge.status = BillingChargeStatus.paid
                charge.amount_paid = net
            elif paid > 0:
                charge.status = BillingChargeStatus.partially_paid
            else:
                charge.status = BillingChargeStatus.pending

        self.db.commit()
        self.db.refresh(charge)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="update",
            entity_type="billing_charge",
            entity_id=charge.id,
            summary=f"Updated charge {charge.id}",
        )
        self.db.commit()
        return BillingChargeResponse.model_validate(charge_to_dict(charge))


class CancelChargeAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, charge_id: UUID, user: dict[str, Any]) -> BillingChargeResponse:
        charge = self.repo.get_charge_by_id(charge_id)
        if not charge:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Charge not found")
        charge.status = BillingChargeStatus.cancelled
        self.db.commit()
        self.db.refresh(charge)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="cancel",
            entity_type="billing_charge",
            entity_id=charge.id,
            summary=f"Cancelled charge {charge.id} ({charge.description[:32]})",
        )
        self.db.commit()
        return BillingChargeResponse.model_validate(charge_to_dict(charge))


# ── Payment Actions ─────────────────────────────────────────────────────────

class ListPaymentsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        payment_method: BillingPaymentMethod | None = None,
    ) -> list[BillingPaymentResponse]:
        payments = self.repo.list_payments(
            patient_id=patient_id,
            from_date=from_date,
            to_date=to_date,
            payment_method=payment_method,
        )
        return [_payment_response_dto(self.db, p) for p in payments]


class CreatePaymentAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingPaymentCreate, user: dict[str, Any]) -> BillingPaymentResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        pdate = payload.payment_date or date.today()
        pay = create_payment(
            self.db,
            hospital_id=self.repo.hospital_id,
            patient_id=payload.patient_id,
            amount=payload.amount,
            payment_date=pdate,
            payment_method=payload.payment_method,
            notes=payload.notes,
            received_by_name=_actor(user),
            allocate=True,
        )
        receipt = issue_receipt_for_payment(
            self.db,
            pay,
            linked_invoice_id=payload.linked_invoice_id,
            reference_number=payload.reference_number,
        )
        refresh_invoice_paid_status(self.db, self.repo.hospital_id, payload.patient_id)
        self.db.commit()
        self.db.refresh(pay)
        self.db.refresh(receipt)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="billing_payment",
            entity_id=pay.id,
            summary=f"Payment ₹{pay.amount:.2f} received for {patient.name} ({receipt.receipt_number})",
        )
        self.db.commit()
        return _payment_response_dto(
            self.db, pay, patient, receipt_id=receipt.id, receipt_number=receipt.receipt_number
        )


# ── Invoice Actions ─────────────────────────────────────────────────────────

class ListInvoicesAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        status: BillingInvoiceStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        search: str | None = None,
    ) -> list[BillingInvoiceResponse]:
        invoices = self.repo.list_invoices(
            patient_id=patient_id,
            status=status,
            from_date=from_date,
            to_date=to_date,
            search=search,
        )
        return [BillingInvoiceResponse.model_validate(invoice_to_dict(inv)) for inv in invoices]


class GetInvoiceAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, invoice_id: UUID) -> BillingInvoiceResponse:
        inv = self.repo.get_invoice_by_id(invoice_id)
        if not inv:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
        return BillingInvoiceResponse.model_validate(invoice_to_dict(inv))


class CreateInvoiceAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingInvoiceCreate, user: dict[str, Any]) -> BillingInvoiceResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        try:
            inv = create_invoice_from_charges(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                charge_ids=payload.charge_ids,
                invoice_date=payload.invoice_date,
                tax_amount=payload.tax_amount,
                notes=payload.notes,
                created_by_name=_actor(user),
            )
            self.db.commit()
            self.db.refresh(inv)
        except ValueError as e:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="billing_invoice",
            entity_id=inv.id,
            summary=f"Invoice {inv.invoice_number} (₹{inv.grand_total:.2f}) created for {patient.name}",
        )
        self.db.commit()
        return BillingInvoiceResponse.model_validate(invoice_to_dict(inv, patient))


class CancelInvoiceAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, invoice_id: UUID, user: dict[str, Any]) -> BillingInvoiceResponse:
        inv = self.repo.get_invoice_by_id(invoice_id)
        if not inv:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
        inv.status = BillingInvoiceStatus.cancelled
        self.db.commit()
        self.db.refresh(inv)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="cancel",
            entity_type="billing_invoice",
            entity_id=inv.id,
            summary=f"Cancelled invoice {inv.invoice_number}",
        )
        self.db.commit()
        return BillingInvoiceResponse.model_validate(invoice_to_dict(inv))


class PrintInvoiceAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, invoice_id: UUID, auto_print: bool = True) -> str:
        inv = self.repo.get_invoice_by_id(invoice_id)
        if not inv:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
        hospital = self.db.query(Hospital).filter(Hospital.id == self.repo.hospital_id).first()
        patient = self.repo.get_patient(inv.patient_id)
        out = patient_outstanding_for_invoice(self.db, self.repo.hospital_id, inv.patient_id)
        return invoice_html(inv, hospital, patient, outstanding=out, auto_print=auto_print)


# ── Receipt Actions ─────────────────────────────────────────────────────────

class ListReceiptsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(
        self,
        *,
        patient_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[BillingReceiptResponse]:
        receipts = self.repo.list_receipts(patient_id=patient_id, from_date=from_date, to_date=to_date)
        return [BillingReceiptResponse.model_validate(receipt_to_dict(r)) for r in receipts]


class GetReceiptAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, receipt_id: UUID) -> BillingReceiptResponse:
        r = self.repo.get_receipt_by_id(receipt_id)
        if not r:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
        return BillingReceiptResponse.model_validate(receipt_to_dict(r))


class CreateReceiptAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, payload: BillingReceiptCreate, user: dict[str, Any]) -> BillingReceiptResponse:
        patient = _get_patient_or_404(self.repo, payload.patient_id)
        pdate = payload.payment_date or date.today()
        pay: BillingPayment | None = None
        if payload.create_payment:
            pay = create_payment(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                amount=payload.amount,
                payment_date=pdate,
                payment_method=payload.payment_method,
                notes=payload.notes,
                received_by_name=_actor(user),
                allocate=True,
            )
        try:
            rcpt = issue_receipt(
                self.db,
                hospital_id=self.repo.hospital_id,
                patient_id=payload.patient_id,
                amount=payload.amount,
                payment_date=pdate,
                payment_method=payload.payment_method,
                collected_by_name=_actor(user),
                reference_number=payload.reference_number,
                notes=payload.notes,
                payment_id=pay.id if pay else None,
                linked_invoice_id=payload.linked_invoice_id,
            )
            refresh_invoice_paid_status(self.db, self.repo.hospital_id, payload.patient_id)
            self.db.commit()
            self.db.refresh(rcpt)
        except ValueError as e:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="billing_receipt",
            entity_id=rcpt.id,
            summary=f"Receipt {rcpt.receipt_number} issued for {patient.name}",
        )
        self.db.commit()
        return BillingReceiptResponse.model_validate(receipt_to_dict(rcpt, patient))


class CancelReceiptAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, receipt_id: UUID, user: dict[str, Any]) -> BillingReceiptResponse:
        r = self.repo.get_receipt_by_id(receipt_id)
        if not r:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
        r.status = BillingReceiptStatus.cancelled
        self.db.commit()
        self.db.refresh(r)
        write_audit_log(
            self.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="cancel",
            entity_type="billing_receipt",
            entity_id=r.id,
            summary=f"Cancelled receipt {r.receipt_number}",
        )
        self.db.commit()
        return BillingReceiptResponse.model_validate(receipt_to_dict(r))


class PrintReceiptAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, receipt_id: UUID, auto_print: bool = True) -> str:
        r = self.repo.get_receipt_by_id(receipt_id)
        if not r:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
        hospital = self.db.query(Hospital).filter(Hospital.id == self.repo.hospital_id).first()
        patient = self.repo.get_patient(r.patient_id)
        return receipt_html(r, hospital, patient, auto_print=auto_print)


# ── Ledger & Dashboard Actions ──────────────────────────────────────────────

class GetPatientLedgerAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, patient_id: UUID) -> PatientLedgerResponse:
        patient = _get_patient_or_404(self.repo, patient_id)
        totals = patient_ledger_totals(self.db, self.repo.hospital_id, patient_id)
        charges = (
            self.db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == self.repo.hospital_id,
                BillingCharge.patient_id == patient_id,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .order_by(BillingCharge.created_at.desc())
            .all()
        )
        payments = (
            self.db.query(BillingPayment)
            .filter(
                BillingPayment.hospital_id == self.repo.hospital_id,
                BillingPayment.patient_id == patient_id,
            )
            .order_by(BillingPayment.created_at.desc())
            .all()
        )
        invoices = (
            self.db.query(BillingInvoice)
            .options(joinedload(BillingInvoice.lines))
            .filter(
                BillingInvoice.hospital_id == self.repo.hospital_id,
                BillingInvoice.patient_id == patient_id,
                BillingInvoice.status != BillingInvoiceStatus.cancelled,
            )
            .order_by(BillingInvoice.created_at.desc())
            .all()
        )
        receipts = (
            self.db.query(BillingReceipt)
            .filter(
                BillingReceipt.hospital_id == self.repo.hospital_id,
                BillingReceipt.patient_id == patient_id,
                BillingReceipt.status != BillingReceiptStatus.cancelled,
            )
            .order_by(BillingReceipt.created_at.desc())
            .all()
        )
        entries = build_ledger_entries(self.db, self.repo.hospital_id, patient_id)
        return PatientLedgerResponse(
            patient_id=patient_id,
            patient_name=patient.name,
            patient_uhid=patient.uhid,
            total_charges=totals["total_charges"],
            total_paid=totals["total_paid"],
            outstanding=totals["outstanding"],
            charge_count=totals["charge_count"],
            payment_count=totals["payment_count"],
            charges=[BillingChargeResponse.model_validate(charge_to_dict(c, patient)) for c in charges],
            payments=[_payment_response_dto(self.db, p, patient) for p in payments],
            invoices=[BillingInvoiceResponse.model_validate(invoice_to_dict(inv, patient)) for inv in invoices],
            receipts=[BillingReceiptResponse.model_validate(receipt_to_dict(r, patient)) for r in receipts],
            entries=[LedgerEntry.model_validate(e) for e in entries],
        )


class GetPatientSummaryAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self, patient_id: UUID) -> PatientFinancialSummary:
        _get_patient_or_404(self.repo, patient_id)
        totals = patient_ledger_totals(self.db, self.repo.hospital_id, patient_id)
        entries = build_ledger_entries(self.db, self.repo.hospital_id, patient_id)[:10]
        return PatientFinancialSummary(
            patient_id=patient_id,
            total_charges=totals["total_charges"],
            total_paid=totals["total_paid"],
            outstanding=totals["outstanding"],
            recent_entries=[LedgerEntry.model_validate(e) for e in entries],
        )


class GetBillingDashboardAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = BillingRepository(db, hospital_id)

    def execute(self) -> BillingDashboardResponse:
        hid = self.repo.hospital_id
        today_start = datetime.combine(date.today(), time.min).replace(tzinfo=timezone.utc)
        today_end = datetime.combine(date.today(), time.max).replace(tzinfo=timezone.utc)

        todays_charges = (
            self.db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
            .filter(
                BillingCharge.hospital_id == hid,
                BillingCharge.created_at >= today_start,
                BillingCharge.created_at <= today_end,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .scalar()
            or 0.0
        )
        todays_collections = (
            self.db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
            .filter(
                BillingPayment.hospital_id == hid,
                BillingPayment.created_at >= today_start,
                BillingPayment.created_at <= today_end,
            )
            .scalar()
            or 0.0
        )
        total_charges = (
            self.db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
            .filter(BillingCharge.hospital_id == hid, BillingCharge.status != BillingChargeStatus.cancelled)
            .scalar()
            or 0.0
        )
        total_paid = (
            self.db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
            .filter(BillingPayment.hospital_id == hid)
            .scalar()
            or 0.0
        )
        outstanding_total = round(max(0.0, float(total_charges) - float(total_paid)), 2)

        pending_count = (
            self.db.query(func.count(BillingCharge.id))
            .filter(
                BillingCharge.hospital_id == hid,
                BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
            )
            .scalar()
            or 0
        )

        # Revenue by source
        todays_ot = (
            self.db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
            .filter(
                BillingCharge.hospital_id == hid,
                BillingCharge.source_type == BillingSourceType.ot,
                BillingCharge.created_at >= today_start,
                BillingCharge.created_at <= today_end,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .scalar()
            or 0.0
        )
        todays_ipd = (
            self.db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
            .filter(
                BillingCharge.hospital_id == hid,
                BillingCharge.source_type.in_([BillingSourceType.bed, BillingSourceType.admission]),
                BillingCharge.created_at >= today_start,
                BillingCharge.created_at <= today_end,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .scalar()
            or 0.0
        )

        # Outstanding by category
        cat_rows = (
            self.db.query(BillingCharge.source_type, func.coalesce(func.sum(BillingCharge.net_amount - BillingCharge.amount_paid), 0.0))
            .filter(
                BillingCharge.hospital_id == hid,
                BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
            )
            .group_by(BillingCharge.source_type)
            .all()
        )
        outstanding_by_cat: dict[str, float] = {
            (r[0].value if r[0] else "other"): round(float(r[1] or 0), 2)
            for r in cat_rows
        }

        # Daily counts
        today_inv_count = (
            self.db.query(func.count(BillingInvoice.id))
            .filter(BillingInvoice.hospital_id == hid, BillingInvoice.invoice_date == date.today())
            .scalar()
            or 0
        )
        today_rcpt_count = (
            self.db.query(func.count(BillingReceipt.id))
            .filter(BillingReceipt.hospital_id == hid, BillingReceipt.payment_date == date.today())
            .scalar()
            or 0
        )
        total_inv = (
            self.db.query(func.coalesce(func.sum(BillingInvoice.grand_total), 0.0))
            .filter(BillingInvoice.hospital_id == hid, BillingInvoice.status != BillingInvoiceStatus.cancelled)
            .scalar()
            or 0.0
        )

        recent_charges = (
            self.db.query(BillingCharge)
            .options(joinedload(BillingCharge.patient))
            .filter(BillingCharge.hospital_id == hid, BillingCharge.status != BillingChargeStatus.cancelled)
            .order_by(BillingCharge.created_at.desc())
            .limit(5)
            .all()
        )
        recent_payments = (
            self.db.query(BillingPayment)
            .options(joinedload(BillingPayment.patient))
            .filter(BillingPayment.hospital_id == hid)
            .order_by(BillingPayment.created_at.desc())
            .limit(5)
            .all()
        )

        return BillingDashboardResponse(
            todays_charges=round(float(todays_charges), 2),
            todays_collections=round(float(todays_collections), 2),
            outstanding_total=outstanding_total,
            pending_charges_count=int(pending_count),
            todays_ot_revenue=round(float(todays_ot), 2),
            todays_ipd_revenue=round(float(todays_ipd), 2),
            outstanding_by_category=outstanding_by_cat,
            today_invoice_count=int(today_inv_count),
            today_receipt_count=int(today_rcpt_count),
            total_invoiced=round(float(total_inv), 2),
            total_collected=round(float(total_paid), 2),
            recent_charges=[BillingChargeResponse.model_validate(charge_to_dict(c)) for c in recent_charges],
            recent_payments=[_payment_response_dto(self.db, p) for p in recent_payments],
        )
