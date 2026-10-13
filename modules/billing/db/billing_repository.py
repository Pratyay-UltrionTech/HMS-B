"""Target architecture repository for Billing data access.

Conforms to UltrionTech-Backend-Template modules/billing/db/ specification.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
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
        self, *, page: int = 1, page_size: int = 15,
        patient_id: UUID | None = None, from_date: date | None = None,
        to_date: date | None = None, payment_method: BillingPaymentMethod | None = None,
        search: str | None = None, collected_by: str | None = None,
        invoice_number: str | None = None, sort: str = "newest",
    ) -> dict[str, Any]:
        # EXISTS avoids duplicate rows when a payment has multiple receipts.
        # Column projections bypass ORM relationship loading entirely.
        q = self.db.query(BillingPayment.id, BillingPayment.amount).filter(
            BillingPayment.hospital_id == self.hospital_id)
        if patient_id:
            q = q.filter(BillingPayment.patient_id == patient_id)
        if from_date:
            q = q.filter(BillingPayment.payment_date >= from_date)
        if to_date:
            q = q.filter(BillingPayment.payment_date <= to_date)
        if payment_method:
            q = q.filter(BillingPayment.payment_method == payment_method)
        def contains(column, value):
            escaped = value.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            return column.ilike(f"%{escaped}%", escape="!")
        if collected_by and collected_by.strip():
            q = q.filter(contains(BillingPayment.received_by_name, collected_by))
        receipt_scope = (
            BillingReceipt.payment_id == BillingPayment.id,
            BillingReceipt.hospital_id == self.hospital_id,
            BillingReceipt.status != BillingReceiptStatus.cancelled,
        )
        if invoice_number and invoice_number.strip():
            invoice_match = self.db.query(BillingReceipt.id).join(
                BillingInvoice, BillingInvoice.id == BillingReceipt.linked_invoice_id
            ).filter(*receipt_scope, BillingInvoice.hospital_id == self.hospital_id,
                     contains(BillingInvoice.invoice_number, invoice_number)).correlate(BillingPayment).exists()
            q = q.filter(invoice_match)
        if search and search.strip():
            patient_match = self.db.query(Patient.id).filter(
                Patient.id == BillingPayment.patient_id, Patient.hospital_id == self.hospital_id,
                or_(contains(Patient.name, search), contains(Patient.uhid, search))).correlate(BillingPayment).exists()
            receipt_match = self.db.query(BillingReceipt.id).outerjoin(
                BillingInvoice, (BillingInvoice.id == BillingReceipt.linked_invoice_id)
                & (BillingInvoice.hospital_id == self.hospital_id)
            ).filter(*receipt_scope, or_(contains(BillingReceipt.receipt_number, search),
                                       contains(BillingInvoice.invoice_number, search))).correlate(BillingPayment).exists()
            q = q.filter(or_(patient_match, receipt_match,
                             contains(BillingPayment.reference_number, search)))
        total, collected = q.with_entities(
            func.count(BillingPayment.id), func.coalesce(func.sum(BillingPayment.amount), 0)
        ).one()
        total_pages = (total + page_size - 1) // page_size
        page = min(page, max(1, total_pages))
        columns = [BillingPayment.id, BillingPayment.patient_id, BillingPayment.amount,
                   BillingPayment.payment_date, BillingPayment.payment_method,
                   BillingPayment.reference_number, BillingPayment.received_by_name,
                   BillingPayment.created_at, Patient.name.label("patient_name"),
                   Patient.uhid.label("patient_uhid")]
        order = {
            "newest": BillingPayment.created_at.desc(), "oldest": BillingPayment.created_at.asc(),
            "amount_desc": BillingPayment.amount.desc(), "amount_asc": BillingPayment.amount.asc(),
        }[sort]
        rows = q.with_entities(*columns).outerjoin(
            Patient, (Patient.id == BillingPayment.patient_id) & (Patient.hospital_id == self.hospital_id)
        ).order_by(order, BillingPayment.id.desc()).limit(page_size).offset((page - 1) * page_size).all()
        items = [dict(row._mapping) for row in rows]
        if items:
            receipts = self.db.query(BillingReceipt.payment_id, BillingReceipt.id, BillingReceipt.receipt_number).filter(
                BillingReceipt.hospital_id == self.hospital_id,
                BillingReceipt.payment_id.in_([item["id"] for item in items]),
                BillingReceipt.status != BillingReceiptStatus.cancelled,
            ).order_by(BillingReceipt.created_at.asc(), BillingReceipt.id.asc()).all()
            by_payment = {}
            for receipt in receipts:
                by_payment.setdefault(receipt.payment_id, receipt)
            for item in items:
                receipt = by_payment.get(item["id"])
                if receipt:
                    item.update(receipt_id=receipt.id, receipt_number=receipt.receipt_number)
        return dict(items=items, total=total, page=page, page_size=page_size,
                    total_pages=total_pages, total_collected=float(collected))

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
        page: int = 1,
        page_size: int = 15,
        patient_id: UUID | None = None,
        status: BillingInvoiceStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        search: str | None = None,
        sort: str = "newest",
    ) -> dict[str, Any]:
        q = self.db.query(BillingInvoice.id, BillingInvoice.grand_total).filter(
            BillingInvoice.hospital_id == self.hospital_id
        )
        if patient_id:
            q = q.filter(BillingInvoice.patient_id == patient_id)
        if status:
            q = q.filter(BillingInvoice.status == status)
        if from_date:
            q = q.filter(BillingInvoice.invoice_date >= from_date)
        if to_date:
            q = q.filter(BillingInvoice.invoice_date <= to_date)

        def contains(column, value):
            escaped = value.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            return column.ilike(f"%{escaped}%", escape="!")

        if search and search.strip():
            patient_match = self.db.query(Patient.id).filter(
                Patient.id == BillingInvoice.patient_id,
                Patient.hospital_id == self.hospital_id,
                or_(contains(Patient.name, search), contains(Patient.uhid, search)),
            ).correlate(BillingInvoice).exists()
            q = q.filter(or_(patient_match, contains(BillingInvoice.invoice_number, search)))

        total, total_amount = q.with_entities(
            func.count(BillingInvoice.id),
            func.coalesce(func.sum(BillingInvoice.grand_total), 0),
        ).one()

        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        page = min(page, max(1, total_pages)) if total > 0 else 1
        offset = (page - 1) * page_size

        columns = [
            BillingInvoice.id,
            BillingInvoice.hospital_id,
            BillingInvoice.patient_id,
            BillingInvoice.account_id,
            BillingInvoice.invoice_number,
            BillingInvoice.invoice_date,
            BillingInvoice.subtotal,
            BillingInvoice.discount_amount,
            BillingInvoice.taxable_amount,
            BillingInvoice.tax_amount,
            BillingInvoice.cgst_amount,
            BillingInvoice.sgst_amount,
            BillingInvoice.igst_amount,
            BillingInvoice.grand_total,
            BillingInvoice.status,
            BillingInvoice.created_by_name,
            BillingInvoice.created_at,
            Patient.name.label("patient_name"),
            Patient.uhid.label("patient_uhid"),
        ]

        sort_map = {
            "newest": (BillingInvoice.created_at.desc(),),
            "oldest": (BillingInvoice.created_at.asc(),),
            "amount_desc": (BillingInvoice.grand_total.desc(),),
            "amount_asc": (BillingInvoice.grand_total.asc(),),
            "date_desc": (BillingInvoice.invoice_date.desc(), BillingInvoice.created_at.desc()),
            "date_asc": (BillingInvoice.invoice_date.asc(), BillingInvoice.created_at.asc()),
        }
        order = sort_map.get(sort, (BillingInvoice.created_at.desc(),))

        rows = (
            q.with_entities(*columns)
            .outerjoin(
                Patient,
                (Patient.id == BillingInvoice.patient_id) & (Patient.hospital_id == self.hospital_id),
            )
            .order_by(*order, BillingInvoice.id.desc())
            .limit(page_size)
            .offset(offset)
            .all()
        )
        items = [dict(row._mapping) for row in rows]
        for it in items:
            for k in (
                "subtotal",
                "discount_amount",
                "taxable_amount",
                "tax_amount",
                "cgst_amount",
                "sgst_amount",
                "igst_amount",
                "grand_total",
            ):
                if k in it:
                    it[k] = float(it[k]) if it[k] is not None else 0.0
            if "created_by_name" in it and it["created_by_name"] is None:
                it["created_by_name"] = ""

        return dict(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            total_amount=float(total_amount),
        )

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
        admission_id: UUID | None = None,
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
        if admission_id:
            q = q.filter(BillingDeposit.admission_id == admission_id)
        if status:
            q = q.filter(BillingDeposit.status == status)
        if from_date:
            q = q.filter(BillingDeposit.deposit_date >= from_date)
        if to_date:
            q = q.filter(BillingDeposit.deposit_date <= to_date)
        return q.order_by(BillingDeposit.created_at.desc()).limit(limit).offset(offset).all()

    def list_deposits_paginated(
        self,
        *,
        patient_id: UUID | None = None,
        account_id: UUID | None = None,
        admission_id: UUID | None = None,
        status: DepositStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        search: str | None = None,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 15,
    ) -> dict[str, Any]:
        q = self.db.query(BillingDeposit).filter(BillingDeposit.hospital_id == self.hospital_id)
        if patient_id:
            q = q.filter(BillingDeposit.patient_id == patient_id)
        if account_id:
            q = q.filter(BillingDeposit.account_id == account_id)
        if admission_id:
            q = q.filter(BillingDeposit.admission_id == admission_id)
        if status:
            q = q.filter(BillingDeposit.status == status)
        if from_date:
            q = q.filter(BillingDeposit.deposit_date >= from_date)
        if to_date:
            q = q.filter(BillingDeposit.deposit_date <= to_date)

        def contains(column, value):
            escaped = value.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            return column.ilike(f"%{escaped}%", escape="!")

        if search and search.strip():
            patient_match = (
                self.db.query(Patient.id)
                .filter(
                    Patient.id == BillingDeposit.patient_id,
                    Patient.hospital_id == self.hospital_id,
                    or_(contains(Patient.name, search), contains(Patient.uhid, search)),
                )
                .correlate(BillingDeposit)
                .exists()
            )
            q = q.filter(
                or_(
                    patient_match,
                    contains(BillingDeposit.deposit_number, search),
                    contains(BillingDeposit.reference_number, search),
                )
            )

        total, total_deposited, total_available = q.with_entities(
            func.count(BillingDeposit.id),
            func.coalesce(func.sum(BillingDeposit.original_amount), 0),
            func.coalesce(func.sum(BillingDeposit.available_amount), 0),
        ).one()

        total = int(total or 0)
        total_deposited = round(float(total_deposited or 0), 2)
        total_available = round(float(total_available or 0), 2)

        if total > 0:
            dep_subq = q.with_entities(BillingDeposit.id)
            total_allocated_raw = (
                self.db.query(func.coalesce(func.sum(BillingPaymentAllocation.allocated_amount), 0))
                .filter(
                    BillingPaymentAllocation.hospital_id == self.hospital_id,
                    BillingPaymentAllocation.deposit_id.in_(dep_subq),
                    BillingPaymentAllocation.payment_id.is_(None),
                )
                .scalar()
            )
            total_allocated = round(max(0.0, float(total_allocated_raw or 0)), 2)

            total_refunded_raw = (
                self.db.query(func.coalesce(func.sum(BillingRefund.amount), 0))
                .filter(
                    BillingRefund.hospital_id == self.hospital_id,
                    BillingRefund.deposit_id.in_(dep_subq),
                    BillingRefund.status.in_([RefundStatus.processed, RefundStatus.approved]),
                )
                .scalar()
            )
            total_refunded = round(max(0.0, float(total_refunded_raw or 0)), 2)
        else:
            total_allocated = 0.0
            total_refunded = 0.0

        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        page = min(page, max(1, total_pages)) if total > 0 else 1
        offset = (page - 1) * page_size

        sort_map = {
            "newest": (BillingDeposit.created_at.desc(),),
            "oldest": (BillingDeposit.created_at.asc(),),
            "amount_desc": (BillingDeposit.original_amount.desc(),),
            "amount_asc": (BillingDeposit.original_amount.asc(),),
            "date_desc": (BillingDeposit.deposit_date.desc(), BillingDeposit.created_at.desc()),
            "date_asc": (BillingDeposit.deposit_date.asc(), BillingDeposit.created_at.asc()),
        }
        order = sort_map.get(sort, (BillingDeposit.created_at.desc(),))

        items = (
            q.options(joinedload(BillingDeposit.patient))
            .order_by(*order, BillingDeposit.id.desc())
            .limit(page_size)
            .offset(offset)
            .all()
        )
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_deposited": total_deposited,
            "total_available": total_available,
            "total_allocated": total_allocated,
            "total_refunded": total_refunded,
        }

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
