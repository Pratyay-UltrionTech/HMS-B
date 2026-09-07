"""Target architecture repository for Billing data access.

Conforms to UltrionTech-Backend-Template modules/billing/db/ specification.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

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
from hms_migration.modules.patients.entities.patient import Patient


class BillingRepository:
    """Encapsulates database queries for Billing charges, payments, invoices, and receipts."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_patient(self, patient_id: UUID) -> Patient | None:
        return (
            self.db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == self.hospital_id)
            .first()
        )

    # ── Charges ─────────────────────────────────────────────────────────────

    def list_charges(
        self,
        *,
        patient_id: UUID | None = None,
        status: BillingChargeStatus | None = None,
        source_type: BillingSourceType | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        search: str | None = None,
    ) -> list[BillingCharge]:
        q = (
            self.db.query(BillingCharge)
            .options(joinedload(BillingCharge.patient))
            .filter(BillingCharge.hospital_id == self.hospital_id)
        )
        if patient_id:
            q = q.filter(BillingCharge.patient_id == patient_id)
        if status:
            q = q.filter(BillingCharge.status == status)
        if source_type:
            q = q.filter(BillingCharge.source_type == source_type)
        if from_date:
            start_dt = datetime.combine(from_date, time.min).replace(tzinfo=timezone.utc)
            q = q.filter(BillingCharge.created_at >= start_dt)
        if to_date:
            end_dt = datetime.combine(to_date, time.max).replace(tzinfo=timezone.utc)
            q = q.filter(BillingCharge.created_at <= end_dt)
        if search:
            term = f"%{search.strip()}%"
            q = q.join(Patient, Patient.id == BillingCharge.patient_id).filter(
                (BillingCharge.description.ilike(term))
                | (Patient.name.ilike(term))
                | (Patient.uhid.ilike(term))
            )
        return q.order_by(BillingCharge.created_at.desc()).all()

    def get_charge_by_id(self, charge_id: UUID) -> BillingCharge | None:
        return (
            self.db.query(BillingCharge)
            .options(joinedload(BillingCharge.patient))
            .filter(BillingCharge.id == charge_id, BillingCharge.hospital_id == self.hospital_id)
            .first()
        )

    # ── Payments ────────────────────────────────────────────────────────────

    def list_payments(
        self,
        *,
        patient_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        payment_method: BillingPaymentMethod | None = None,
    ) -> list[BillingPayment]:
        q = (
            self.db.query(BillingPayment)
            .options(joinedload(BillingPayment.patient))
            .filter(BillingPayment.hospital_id == self.hospital_id)
        )
        if patient_id:
            q = q.filter(BillingPayment.patient_id == patient_id)
        if from_date:
            q = q.filter(BillingPayment.payment_date >= from_date)
        if to_date:
            q = q.filter(BillingPayment.payment_date <= to_date)
        if payment_method:
            q = q.filter(BillingPayment.payment_method == payment_method)
        return q.order_by(BillingPayment.created_at.desc()).all()

    def get_payment_by_id(self, payment_id: UUID) -> BillingPayment | None:
        return (
            self.db.query(BillingPayment)
            .options(joinedload(BillingPayment.patient))
            .filter(BillingPayment.id == payment_id, BillingPayment.hospital_id == self.hospital_id)
            .first()
        )

    # ── Invoices ────────────────────────────────────────────────────────────

    def list_invoices(
        self,
        *,
        patient_id: UUID | None = None,
        status: BillingInvoiceStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        search: str | None = None,
    ) -> list[BillingInvoice]:
        q = (
            self.db.query(BillingInvoice)
            .options(joinedload(BillingInvoice.patient), joinedload(BillingInvoice.lines))
            .filter(BillingInvoice.hospital_id == self.hospital_id)
        )
        if patient_id:
            q = q.filter(BillingInvoice.patient_id == patient_id)
        if status:
            q = q.filter(BillingInvoice.status == status)
        if from_date:
            q = q.filter(BillingInvoice.invoice_date >= from_date)
        if to_date:
            q = q.filter(BillingInvoice.invoice_date <= to_date)
        if search:
            term = f"%{search.strip()}%"
            q = q.join(Patient, Patient.id == BillingInvoice.patient_id).filter(
                (BillingInvoice.invoice_number.ilike(term))
                | (Patient.name.ilike(term))
                | (Patient.uhid.ilike(term))
            )
        return q.order_by(BillingInvoice.created_at.desc()).all()

    def get_invoice_by_id(self, invoice_id: UUID) -> BillingInvoice | None:
        return (
            self.db.query(BillingInvoice)
            .options(joinedload(BillingInvoice.patient), joinedload(BillingInvoice.lines))
            .filter(BillingInvoice.id == invoice_id, BillingInvoice.hospital_id == self.hospital_id)
            .first()
        )

    # ── Receipts ────────────────────────────────────────────────────────────

    def list_receipts(
        self,
        *,
        patient_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[BillingReceipt]:
        q = (
            self.db.query(BillingReceipt)
            .options(joinedload(BillingReceipt.patient))
            .filter(BillingReceipt.hospital_id == self.hospital_id)
        )
        if patient_id:
            q = q.filter(BillingReceipt.patient_id == patient_id)
        if from_date:
            q = q.filter(BillingReceipt.payment_date >= from_date)
        if to_date:
            q = q.filter(BillingReceipt.payment_date <= to_date)
        return q.order_by(BillingReceipt.created_at.desc()).all()

    def get_receipt_by_id(self, receipt_id: UUID) -> BillingReceipt | None:
        return (
            self.db.query(BillingReceipt)
            .options(joinedload(BillingReceipt.patient))
            .filter(BillingReceipt.id == receipt_id, BillingReceipt.hospital_id == self.hospital_id)
            .first()
        )
