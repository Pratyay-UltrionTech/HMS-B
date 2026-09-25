"""Target architecture entities for Billing, Ledger, Invoices, Receipts, Payments,
Financial Accounts, Allocations, Deposits, and Refunds.

Conforms to UltrionTech-Backend-Template modules/billing/entities/ specification.
Maps directly to:
- financial_accounts
- billing_charges
- billing_payments
- billing_payment_allocations
- billing_deposits
- billing_refunds
- billing_invoices
- billing_invoice_lines
- billing_receipts
- consultation_pricing
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from typing import TYPE_CHECKING

from infrastructure.postgres.base import Base

if TYPE_CHECKING:
    from modules.patients.entities.patient import Patient


# ── Enumerations ─────────────────────────────────────────────────────────────

class FinancialAccountType(str, enum.Enum):
    """Episode boundary or grouping for patient financial accounts."""

    opd = "opd"
    ipd = "ipd"
    emergency = "emergency"
    daycare = "daycare"
    general = "general"


class FinancialAccountStatus(str, enum.Enum):
    """Lifecycle settlement status of a financial account."""

    open = "open"
    cleared = "cleared"
    closed = "closed"


class BillingSourceType(str, enum.Enum):
    """Source domain generating billable transaction."""

    consultation = "consultation"
    laboratory = "laboratory"
    radiology = "radiology"
    ot = "ot"
    bed = "bed"
    admission = "admission"
    pharmacy = "pharmacy"
    other = "other"
    adjustment = "adjustment"


class BillingChargeStatus(str, enum.Enum):
    """Lifecycle settlement status of a billing charge."""

    pending = "pending"
    paid = "paid"
    partially_paid = "partially_paid"
    cancelled = "cancelled"


class BillingPaymentMethod(str, enum.Enum):
    """Tender method for payment collection."""

    cash = "cash"
    card = "card"
    upi = "upi"
    bank_transfer = "bank_transfer"
    other = "other"


class BillingInvoiceStatus(str, enum.Enum):
    """Lifecycle state of a finalized invoice."""

    draft = "draft"
    generated = "generated"
    paid = "paid"
    cancelled = "cancelled"


class BillingReceiptStatus(str, enum.Enum):
    """Lifecycle state of an issued receipt."""

    issued = "issued"
    cancelled = "cancelled"


class DepositStatus(str, enum.Enum):
    """Lifecycle state of an advance deposit."""

    available = "available"
    partially_allocated = "partially_allocated"
    exhausted = "exhausted"
    refunded = "refunded"


class RefundStatus(str, enum.Enum):
    """Status of a financial refund voucher."""

    draft = "draft"
    approved = "approved"
    processed = "processed"
    rejected = "rejected"


# ── Financial Account (Episode Boundary) ─────────────────────────────────────

class FinancialAccount(Base):
    """
    Financial container / episode account for an admission, visit, or emergency stay.
    Prevents cross-encounter debt leakage and scopes financial clearance.
    """

    __tablename__ = "financial_accounts"
    __table_args__ = (
        UniqueConstraint("hospital_id", "account_number", name="uq_financial_account_number"),
        Index(
            "uq_financial_accounts_open_ipd_admission",
            "hospital_id",
            "admission_id",
            unique=True,
            postgresql_where=text("admission_id IS NOT NULL AND account_type = 'ipd' AND status = 'open'"),
            sqlite_where=text("admission_id IS NOT NULL AND account_type = 'ipd' AND status = 'open'"),
        ),
        Index("ix_financial_accounts_patient_status", "hospital_id", "patient_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_type: Mapped[FinancialAccountType] = mapped_column(
        Enum(FinancialAccountType, name="financial_account_type"),
        nullable=False,
        default=FinancialAccountType.general,
        index=True,
    )
    status: Mapped[FinancialAccountStatus] = mapped_column(
        Enum(FinancialAccountStatus, name="financial_account_status"),
        nullable=False,
        default=FinancialAccountStatus.open,
        index=True,
    )
    # Pointers to source clinical episode if applicable
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    charges: Mapped[list["BillingCharge"]] = relationship(
        "BillingCharge", back_populates="account"
    )
    payments: Mapped[list["BillingPayment"]] = relationship(
        "BillingPayment", back_populates="account"
    )
    deposits: Mapped[list["BillingDeposit"]] = relationship(
        "BillingDeposit", back_populates="account"
    )


# ── Billing Charge ───────────────────────────────────────────────────────────

class BillingCharge(Base):
    """Unified patient charge / billable transaction (ledger debit)."""

    __tablename__ = "billing_charges"
    __table_args__ = (
        Index("ix_billing_charges_hospital_created", "hospital_id", "created_at"),
        Index("ix_billing_charges_account", "hospital_id", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_type: Mapped[BillingSourceType] = mapped_column(
        Enum(BillingSourceType, name="billing_source_type"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("1.0"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    charge_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    discount_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    gst_rate: Mapped[float | None] = mapped_column(Float, nullable=True, default=0.0)
    hsn_sac_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    status: Mapped[BillingChargeStatus] = mapped_column(
        Enum(BillingChargeStatus, name="billing_charge_status"),
        nullable=False,
        default=BillingChargeStatus.pending,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    account: Mapped["FinancialAccount | None"] = relationship("FinancialAccount", back_populates="charges")
    allocations: Mapped[list["BillingPaymentAllocation"]] = relationship(
        "BillingPaymentAllocation", back_populates="charge"
    )


# ── Billing Payment ──────────────────────────────────────────────────────────

class BillingPayment(Base):
    """Patient payment collection (ledger credit)."""

    __tablename__ = "billing_payments"
    __table_args__ = (
        Index("ix_billing_payments_hospital_created", "hospital_id", "created_at"),
        Index("ix_billing_payments_account", "hospital_id", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[BillingPaymentMethod] = mapped_column(
        Enum(BillingPaymentMethod, name="billing_payment_method"),
        nullable=False,
        default=BillingPaymentMethod.cash,
    )
    reference_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    account: Mapped["FinancialAccount | None"] = relationship("FinancialAccount", back_populates="payments")
    allocations: Mapped[list["BillingPaymentAllocation"]] = relationship(
        "BillingPaymentAllocation", back_populates="payment"
    )
    refunds: Mapped[list["BillingRefund"]] = relationship(
        "BillingRefund", back_populates="payment"
    )


# ── Billing Payment Allocation (Selective Settlement) ───────────────────────

class BillingPaymentAllocation(Base):
    """
    Explicit, auditable allocation of tender (from payment or deposit) to a charge.
    Enables selective payment and partial item settlement.
    """

    __tablename__ = "billing_payment_allocations"
    __table_args__ = (
        Index("ix_allocations_charge", "charge_id"),
        Index("ix_allocations_payment", "payment_id"),
        Index("ix_allocations_deposit", "deposit_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_payments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    deposit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_deposits.id", ondelete="CASCADE"), nullable=True, index=True
    )
    charge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_charges.id", ondelete="CASCADE"), nullable=False, index=True
    )
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    payment: Mapped["BillingPayment | None"] = relationship("BillingPayment", back_populates="allocations")
    deposit: Mapped["BillingDeposit | None"] = relationship("BillingDeposit", back_populates="allocations")
    charge: Mapped["BillingCharge"] = relationship("BillingCharge", back_populates="allocations")


# ── Billing Deposit (Advance / Unearned Revenue) ────────────────────────────

class BillingDeposit(Base):
    """
    Patient advance deposit held as liability before service execution.
    Tracks available balance, drawdown allocations, and refunds.
    """

    __tablename__ = "billing_deposits"
    __table_args__ = (
        UniqueConstraint("hospital_id", "deposit_number", name="uq_billing_deposit_number"),
        Index("ix_billing_deposits_patient", "hospital_id", "patient_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deposit_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    deposit_date: Mapped[date] = mapped_column(Date, nullable=False)
    deposit_type: Mapped[str] = mapped_column(String(32), nullable=False, default="admission")  # admission, surgical, general
    payment_method: Mapped[BillingPaymentMethod] = mapped_column(
        Enum(BillingPaymentMethod, name="billing_payment_method", create_type=False),
        nullable=False,
        default=BillingPaymentMethod.cash,
    )
    original_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    available_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[DepositStatus] = mapped_column(
        Enum(DepositStatus, name="billing_deposit_status"),
        nullable=False,
        default=DepositStatus.available,
        index=True,
    )
    reference_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    account: Mapped["FinancialAccount | None"] = relationship("FinancialAccount", back_populates="deposits")
    allocations: Mapped[list["BillingPaymentAllocation"]] = relationship(
        "BillingPaymentAllocation", back_populates="deposit"
    )
    refunds: Mapped[list["BillingRefund"]] = relationship(
        "BillingRefund", back_populates="deposit"
    )


# ── Billing Refund (Formal Refund Voucher) ───────────────────────────────────

class BillingRefund(Base):
    """
    Documented refund returned to patient from a payment or unused deposit.
    Maintains link to original tender, prevents over-refunding, and generates voucher.
    """

    __tablename__ = "billing_refunds"
    __table_args__ = (
        UniqueConstraint("hospital_id", "refund_number", name="uq_billing_refund_number"),
        Index("ix_billing_refunds_patient", "hospital_id", "patient_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_payments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deposit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_deposits.id", ondelete="SET NULL"), nullable=True, index=True
    )
    refund_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    refund_date: Mapped[date] = mapped_column(Date, nullable=False)
    refund_method: Mapped[BillingPaymentMethod] = mapped_column(
        Enum(BillingPaymentMethod, name="billing_payment_method", create_type=False),
        nullable=False,
        default=BillingPaymentMethod.cash,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[RefundStatus] = mapped_column(
        Enum(RefundStatus, name="billing_refund_status"),
        nullable=False,
        default=RefundStatus.processed,
        index=True,
    )
    approved_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    processed_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    payment: Mapped["BillingPayment | None"] = relationship("BillingPayment", back_populates="refunds")
    deposit: Mapped["BillingDeposit | None"] = relationship("BillingDeposit", back_populates="refunds")


# ── Billing Invoice & Lines ──────────────────────────────────────────────────

class BillingInvoice(Base):
    """Hospital-scoped patient invoice generated from ledger charges."""

    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint("hospital_id", "invoice_number", name="uq_billing_invoice_number"),
        Index("ix_billing_invoices_hospital_created", "hospital_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    taxable_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    igst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    status: Mapped[BillingInvoiceStatus] = mapped_column(
        Enum(BillingInvoiceStatus, name="billing_invoice_status"),
        nullable=False,
        default=BillingInvoiceStatus.generated,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    lines: Mapped[list[BillingInvoiceLine]] = relationship(
        "BillingInvoiceLine", back_populates="invoice", cascade="all, delete-orphan", order_by="BillingInvoiceLine.sort_order"
    )


class BillingInvoiceLine(Base):
    """Immutable snapshot of a charge on an invoice with HSN and tax decomposition."""

    __tablename__ = "billing_invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    charge_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_charges.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, default="other")
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("1.0"))
    rate: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    hsn_sac_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True, default=Decimal("0.0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.0"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoice: Mapped[BillingInvoice] = relationship("BillingInvoice", back_populates="lines")


# ── Billing Receipt ──────────────────────────────────────────────────────────

class BillingReceipt(Base):
    """Payment receipt document (hospital-scoped numbering)."""

    __tablename__ = "billing_receipts"
    __table_args__ = (
        UniqueConstraint("hospital_id", "receipt_number", name="uq_billing_receipt_number"),
        Index("ix_billing_receipts_hospital_created", "hospital_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_payments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deposit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_deposits.id", ondelete="SET NULL"), nullable=True, index=True
    )
    linked_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    receipt_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[BillingPaymentMethod] = mapped_column(
        Enum(BillingPaymentMethod, name="billing_payment_method", create_type=False),
        nullable=False,
        default=BillingPaymentMethod.cash,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    reference_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[BillingReceiptStatus] = mapped_column(
        Enum(BillingReceiptStatus, name="billing_receipt_status"),
        nullable=False,
        default=BillingReceiptStatus.issued,
        index=True,
    )
    collected_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])


# ── Consultation Pricing ─────────────────────────────────────────────────────

class ConsultationPricing(Base):
    """Wing + Department + Doctor + Appointment Type -> consultation fee."""

    __tablename__ = "consultation_pricing"
    __table_args__ = (
        UniqueConstraint(
            "hospital_id",
            "wing_id",
            "department_id",
            "doctor_id",
            "appointment_type_id",
            name="uq_consultation_pricing_combo",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appointment_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointment_types.id", ondelete="CASCADE"), nullable=False, index=True
    )
    consultation_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    followup_free_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
