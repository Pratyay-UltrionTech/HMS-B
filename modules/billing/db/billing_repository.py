"""Target architecture repository for Billing data access.

Conforms to UltrionTech-Backend-Template modules/billing/db/ specification.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentAllocation,
    BillingPaymentMethod,
    BillingReceipt,
    BillingReceiptStatus,
    BillingRefund,
    BillingSourceType,
    DepositStatus,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    RefundStatus,
)
from modules.patients.entities.patient import Patient


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
        limit: int = 200,
        offset: int = 0,
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
        return q.order_by(BillingCharge.created_at.desc()).limit(limit).offset(offset).all()

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
        limit: int = 200,
        offset: int = 0,
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
        return q.order_by(BillingPayment.created_at.desc()).limit(limit).offset(offset).all()

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
        limit: int = 200,
        offset: int = 0,
    ) -> list[BillingInvoice]:
        q = (
            self.db.query(BillingInvoice)
            .options(
                joinedload(BillingInvoice.patient),
                # Lines are no longer serialized into the list response (the
                # Invoices tab table doesn't read them — see invoice_to_dict's
                # include_lines and ListInvoicesAction), so there's no reason
                # to eager-load them here either: this drops one query on top
                # of the smaller payload.
            )
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
        return q.order_by(BillingInvoice.created_at.desc()).limit(limit).offset(offset).all()

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
        limit: int = 200,
        offset: int = 0,
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
        return q.order_by(BillingReceipt.created_at.desc()).limit(limit).offset(offset).all()

    def get_receipt_by_id(self, receipt_id: UUID) -> BillingReceipt | None:
        return (
            self.db.query(BillingReceipt)
            .options(joinedload(BillingReceipt.patient))
            .filter(BillingReceipt.id == receipt_id, BillingReceipt.hospital_id == self.hospital_id)
            .first()
        )

    # ── Financial Accounts ──────────────────────────────────────────────────

    def list_accounts(
        self,
        *,
        patient_id: UUID | None = None,
        status: FinancialAccountStatus | None = None,
        account_type: FinancialAccountType | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[FinancialAccount]:
        q = (
            self.db.query(FinancialAccount)
            .options(joinedload(FinancialAccount.patient))
            .filter(FinancialAccount.hospital_id == self.hospital_id)
        )
        if patient_id:
            q = q.filter(FinancialAccount.patient_id == patient_id)
        if status:
            q = q.filter(FinancialAccount.status == status)
        if account_type:
            q = q.filter(FinancialAccount.account_type == account_type)
        return q.order_by(FinancialAccount.opened_at.desc()).limit(limit).offset(offset).all()

    def get_account_by_id(self, account_id: UUID) -> FinancialAccount | None:
        return (
            self.db.query(FinancialAccount)
            .options(joinedload(FinancialAccount.patient))
            .filter(FinancialAccount.id == account_id, FinancialAccount.hospital_id == self.hospital_id)
            .first()
        )

    # ── Deposits ────────────────────────────────────────────────────────────

    def list_deposits(
        self,
        *,
        patient_id: UUID | None = None,
        account_id: UUID | None = None,
        status: DepositStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[BillingDeposit]:
        q = (
            self.db.query(BillingDeposit)
            .options(joinedload(BillingDeposit.patient))
            .filter(BillingDeposit.hospital_id == self.hospital_id)
        )
        if patient_id:
            q = q.filter(BillingDeposit.patient_id == patient_id)
        if account_id:
            q = q.filter(BillingDeposit.account_id == account_id)
        if status:
            q = q.filter(BillingDeposit.status == status)
        if from_date:
            q = q.filter(BillingDeposit.deposit_date >= from_date)
        if to_date:
            q = q.filter(BillingDeposit.deposit_date <= to_date)
        return q.order_by(BillingDeposit.created_at.desc()).limit(limit).offset(offset).all()

    def get_deposit_by_id(self, deposit_id: UUID) -> BillingDeposit | None:
        return (
            self.db.query(BillingDeposit)
            .options(joinedload(BillingDeposit.patient))
            .filter(BillingDeposit.id == deposit_id, BillingDeposit.hospital_id == self.hospital_id)
            .first()
        )

    # ── Refunds ─────────────────────────────────────────────────────────────

    def list_refunds(
        self,
        *,
        patient_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        status: RefundStatus | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[BillingRefund]:
        q = (
            self.db.query(BillingRefund)
            .options(joinedload(BillingRefund.patient))
            .filter(BillingRefund.hospital_id == self.hospital_id)
        )
        if patient_id:
            q = q.filter(BillingRefund.patient_id == patient_id)
        if status:
            q = q.filter(BillingRefund.status == status)
        if from_date:
            q = q.filter(BillingRefund.refund_date >= from_date)
        if to_date:
            q = q.filter(BillingRefund.refund_date <= to_date)
        return q.order_by(BillingRefund.created_at.desc()).limit(limit).offset(offset).all()

    def get_refund_by_id(self, refund_id: UUID) -> BillingRefund | None:
        return (
            self.db.query(BillingRefund)
            .options(joinedload(BillingRefund.patient))
            .filter(BillingRefund.id == refund_id, BillingRefund.hospital_id == self.hospital_id)
            .first()
        )

    # ── Allocations ─────────────────────────────────────────────────────────

    def list_allocations_for_payment(self, payment_id: UUID) -> list[BillingPaymentAllocation]:
        return (
            self.db.query(BillingPaymentAllocation)
            .options(joinedload(BillingPaymentAllocation.charge))
            .filter(
                BillingPaymentAllocation.hospital_id == self.hospital_id,
                BillingPaymentAllocation.payment_id == payment_id,
            )
            .order_by(BillingPaymentAllocation.created_at.asc())
            .all()
        )

    def list_allocations_for_deposit(self, deposit_id: UUID) -> list[BillingPaymentAllocation]:
        return (
            self.db.query(BillingPaymentAllocation)
            .options(joinedload(BillingPaymentAllocation.charge))
            .filter(
                BillingPaymentAllocation.hospital_id == self.hospital_id,
                BillingPaymentAllocation.deposit_id == deposit_id,
            )
            .order_by(BillingPaymentAllocation.created_at.asc())
            .all()
        )
