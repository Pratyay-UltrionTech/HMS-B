"""Target architecture entities for Billing, Ledger, Invoices, Receipts, and Payments.

Conforms to UltrionTech-Backend-Template modules/billing/entities/ specification.
Maps directly to billing_charges, billing_payments, billing_invoices,
billing_invoice_lines, billing_receipts, and consultation_pricing on target Base.
"""

from __future__ import annotations

from datetime import date, datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from typing import TYPE_CHECKING

from hms_migration.infrastructure.postgres.base import Base

if TYPE_CHECKING:
    from hms_migration.modules.patients.entities.patient import Patient


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


class BillingCharge(Base):
    """Unified patient charge / billable transaction (ledger debit)."""

    __tablename__ = "billing_charges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[BillingSourceType] = mapped_column(
        Enum(BillingSourceType, name="billing_source_type"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    charge_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    amount_paid: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[BillingChargeStatus] = mapped_column(
        Enum(BillingChargeStatus, name="billing_charge_status"),
        nullable=False,
        default=BillingChargeStatus.pending,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])


class BillingPayment(Base):
    """Patient payment collection (ledger credit)."""

    __tablename__ = "billing_payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[BillingPaymentMethod] = mapped_column(
        Enum(BillingPaymentMethod, name="billing_payment_method"),
        nullable=False,
        default=BillingPaymentMethod.cash,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])


class BillingInvoice(Base):
    """Hospital-scoped patient invoice generated from ledger charges."""

    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint("hospital_id", "invoice_number", name="uq_billing_invoice_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    subtotal: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tax_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    grand_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[BillingInvoiceStatus] = mapped_column(
        Enum(BillingInvoiceStatus, name="billing_invoice_status"),
        nullable=False,
        default=BillingInvoiceStatus.generated,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    lines: Mapped[list[BillingInvoiceLine]] = relationship(
        "BillingInvoiceLine", back_populates="invoice", cascade="all, delete-orphan", order_by="BillingInvoiceLine.sort_order"
    )


class BillingInvoiceLine(Base):
    """Immutable snapshot of a charge on an invoice."""

    __tablename__ = "billing_invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    charge_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_charges.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, default="other")
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoice: Mapped[BillingInvoice] = relationship("BillingInvoice", back_populates="lines")


class BillingReceipt(Base):
    """Payment receipt document (hospital-scoped numbering)."""

    __tablename__ = "billing_receipts"
    __table_args__ = (
        UniqueConstraint("hospital_id", "receipt_number", name="uq_billing_receipt_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_payments.id", ondelete="SET NULL"), nullable=True, index=True
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
    amount: Mapped[float] = mapped_column(Float, nullable=False)
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
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])


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
        UUID(as_uuid=True), nullable=False, index=True
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
