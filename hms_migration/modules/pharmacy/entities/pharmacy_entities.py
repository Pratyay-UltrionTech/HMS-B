"""
Target-native SQLAlchemy entities for Pharmacy domain.

Tables:
- medicine_categories
- pharmacy_suppliers
- medicines
- medicine_batches
- medicine_inventory
- pharmacy_purchases
- pharmacy_purchase_items
- stock_transactions
- stock_adjustments
- pharmacy_sales
- pharmacy_sale_items
- pharmacy_returns
- pharmacy_rx_requests
- pharmacy_rx_request_items
"""

from __future__ import annotations

from datetime import date, datetime
import enum
from typing import TYPE_CHECKING
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

from hms_migration.infrastructure.postgres.base import Base

if TYPE_CHECKING:
    from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
    from hms_migration.modules.doctors.entities.doctor import HospitalUser
    from hms_migration.modules.patients.entities.patient import Patient


class MedicineType(str, enum.Enum):
    tablet = "tablet"
    capsule = "capsule"
    injection = "injection"
    syrup = "syrup"
    cream = "cream"
    drops = "drops"
    powder = "powder"
    ointment = "ointment"
    consumable = "consumable"


class MedicineUnit(str, enum.Enum):
    tablet = "tablet"
    bottle = "bottle"
    strip = "strip"
    vial = "vial"
    ampoule = "ampoule"
    tube = "tube"
    pack = "pack"


class PurchasePaymentStatus(str, enum.Enum):
    unpaid = "unpaid"
    partial = "partial"
    paid = "paid"


class StockTransactionType(str, enum.Enum):
    purchase = "purchase"
    sale = "sale"
    return_in = "return_in"  # customer return -> stock up
    return_out = "return_out"  # purchase return -> stock down
    adjustment = "adjustment"
    expired = "expired"
    damage = "damage"
    manual_correction = "manual_correction"


class StockAdjustmentType(str, enum.Enum):
    damage = "damage"
    expired = "expired"
    lost = "lost"
    manual_correction = "manual_correction"


class PharmacySaleType(str, enum.Enum):
    walk_in = "walk_in"
    prescription = "prescription"
    patient = "patient"


class PharmacySaleStatus(str, enum.Enum):
    completed = "completed"
    cancelled = "cancelled"
    returned = "returned"
    partially_returned = "partially_returned"


class PharmacyPaymentStatus(str, enum.Enum):
    unpaid = "unpaid"
    paid = "paid"
    partial = "partial"


class PharmacyRxRequestStatus(str, enum.Enum):
    pending = "pending"
    partially_dispensed = "partially_dispensed"
    dispensed = "dispensed"
    completed = "completed"
    cancelled = "cancelled"


class MedicineCategory(Base):
    __tablename__ = "medicine_categories"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_medicine_category_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PharmacySupplier(Base):
    __tablename__ = "pharmacy_suppliers"
    __table_args__ = (UniqueConstraint("hospital_id", "company_name", name="uq_pharmacy_supplier_company"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Medicine(Base):
    """Medicine master entity."""

    __tablename__ = "medicines"
    __table_args__ = (
        UniqueConstraint(
            "hospital_id", "brand_name", "strength", "manufacturer",
            name="uq_medicine_brand_strength_mfr",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicine_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    medicine_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    generic_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    brand_name: Mapped[str] = mapped_column(String(255), nullable=False)
    manufacturer: Mapped[str] = mapped_column(String(255), nullable=False)
    medicine_type: Mapped[MedicineType] = mapped_column(
        Enum(MedicineType, name="medicine_type"), nullable=False, default=MedicineType.tablet
    )
    strength: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    unit: Mapped[MedicineUnit] = mapped_column(
        Enum(MedicineUnit, name="medicine_unit"), nullable=False, default=MedicineUnit.tablet
    )
    hsn_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gst_percent: Mapped[float] = mapped_column(Float, nullable=False, default=12.0)
    selling_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    purchase_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mrp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    minimum_stock: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    maximum_stock: Mapped[float] = mapped_column(Float, nullable=False, default=500.0)
    reorder_level: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
    storage_condition: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_schedule_drug: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_prescription: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    barcode_value: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    qr_code: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    category: Mapped["MedicineCategory | None"] = relationship()
    batches: Mapped[list["MedicineBatch"]] = relationship(back_populates="medicine")
    inventory: Mapped["MedicineInventory | None"] = relationship(back_populates="medicine", uselist=False)


class MedicineBatch(Base):
    __tablename__ = "medicine_batches"
    __table_args__ = (
        UniqueConstraint("hospital_id", "medicine_id", "batch_number", name="uq_medicine_batch_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pharmacy_suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    batch_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    manufacture_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    purchase_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    selling_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mrp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    initial_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    available_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    barcode_value: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    barcode_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scanner_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    medicine: Mapped["Medicine"] = relationship(back_populates="batches")
    supplier: Mapped["PharmacySupplier | None"] = relationship()


class MedicineInventory(Base):
    """Denormalized stock summary per medicine."""

    __tablename__ = "medicine_inventory"
    __table_args__ = (UniqueConstraint("hospital_id", "medicine_id", name="uq_medicine_inventory"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    current_stock: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reserved_stock: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    available_stock: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    nearest_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    batch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    medicine: Mapped["Medicine"] = relationship(back_populates="inventory")


class PharmacyPurchase(Base):
    __tablename__ = "pharmacy_purchases"
    __table_args__ = (UniqueConstraint("hospital_id", "invoice_number", name="uq_pharmacy_purchase_invoice"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pharmacy_suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_status: Mapped[PurchasePaymentStatus] = mapped_column(
        Enum(PurchasePaymentStatus, name="purchase_payment_status"),
        nullable=False,
        default=PurchasePaymentStatus.unpaid,
    )
    subtotal: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gst_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    net_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    supplier: Mapped["PharmacySupplier"] = relationship()
    items: Mapped[list["PharmacyPurchaseItem"]] = relationship(
        back_populates="purchase", cascade="all, delete-orphan"
    )


class PharmacyPurchaseItem(Base):
    __tablename__ = "pharmacy_purchase_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pharmacy_purchases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicine_batches.id", ondelete="SET NULL"), nullable=True
    )
    batch_number: Mapped[str] = mapped_column(String(64), nullable=False)
    manufacture_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    purchase_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mrp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    selling_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    free_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gst_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gst_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    net_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    purchase: Mapped["PharmacyPurchase"] = relationship(back_populates="items")
    medicine: Mapped["Medicine"] = relationship()


class StockTransaction(Base):
    __tablename__ = "stock_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicine_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    transaction_type: Mapped[StockTransactionType] = mapped_column(
        Enum(StockTransactionType, name="stock_transaction_type"), nullable=False, index=True
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)  # signed: +in / -out
    quantity_before: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quantity_after: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reference_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class StockAdjustment(Base):
    __tablename__ = "stock_adjustments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicine_batches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    adjustment_type: Mapped[StockAdjustmentType] = mapped_column(
        Enum(StockAdjustmentType, name="stock_adjustment_type"), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    medicine: Mapped["Medicine"] = relationship()
    batch: Mapped["MedicineBatch"] = relationship()


class PharmacySale(Base):
    __tablename__ = "pharmacy_sales"
    __table_args__ = (UniqueConstraint("hospital_id", "invoice_number", name="uq_pharmacy_sale_invoice"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sale_type: Mapped[PharmacySaleType] = mapped_column(
        Enum(PharmacySaleType, name="pharmacy_sale_type"),
        nullable=False,
        default=PharmacySaleType.walk_in,
    )
    status: Mapped[PharmacySaleStatus] = mapped_column(
        Enum(PharmacySaleStatus, name="pharmacy_sale_status"),
        nullable=False,
        default=PharmacySaleStatus.completed,
        index=True,
    )
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    prescription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    pharmacy_rx_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pharmacy_rx_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    doctor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sale_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    subtotal: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gst_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    net_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    amount_paid: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    payment_status: Mapped[PharmacyPaymentStatus] = mapped_column(
        Enum(PharmacyPaymentStatus, name="pharmacy_payment_status"),
        nullable=False,
        default=PharmacyPaymentStatus.paid,
    )
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False, default="cash")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_charge_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    patient: Mapped["Patient | None"] = relationship()
    items: Mapped[list["PharmacySaleItem"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan"
    )


class PharmacySaleItem(Base):
    __tablename__ = "pharmacy_sale_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sale_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pharmacy_sales.id", ondelete="CASCADE"), nullable=False, index=True
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicine_batches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    batch_number: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    purchase_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gst_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gst_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    net_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    returned_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    sale: Mapped["PharmacySale"] = relationship(back_populates="items")
    medicine: Mapped["Medicine"] = relationship()
    batch: Mapped["MedicineBatch"] = relationship()


class PharmacyReturn(Base):
    __tablename__ = "pharmacy_returns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    return_type: Mapped[str] = mapped_column(String(32), nullable=False)  # customer | purchase
    sale_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pharmacy_sales.id", ondelete="SET NULL"), nullable=True, index=True
    )
    purchase_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pharmacy_purchases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    medicine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicine_batches.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    refund_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PharmacyRxRequest(Base):
    __tablename__ = "pharmacy_rx_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prescription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    patient_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    patient_phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    doctor_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[PharmacyRxRequestStatus] = mapped_column(
        Enum(PharmacyRxRequestStatus, name="pharmacy_rx_request_status"),
        nullable=False,
        default=PharmacyRxRequestStatus.pending,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    items: Mapped[list["PharmacyRxRequestItem"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )


class PharmacyRxRequestItem(Base):
    __tablename__ = "pharmacy_rx_request_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pharmacy_rx_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    medicine_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="SET NULL"), nullable=True
    )
    medicine_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    dosage: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dispensed_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    request: Mapped["PharmacyRxRequest"] = relationship(back_populates="items")
    medicine: Mapped["Medicine | None"] = relationship()
