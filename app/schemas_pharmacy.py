"""Pydantic schemas for Pharmacy module."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import (
    MedicineType,
    MedicineUnit,
    PharmacyPaymentStatus,
    PharmacyRxRequestStatus,
    PharmacySaleStatus,
    PharmacySaleType,
    PurchasePaymentStatus,
    StockAdjustmentType,
)


# ── Categories ────────────────────────────────────────────────────────────────
class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    is_active: bool = True


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    is_active: bool | None = None


class CategoryResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ── Suppliers ─────────────────────────────────────────────────────────────────
class SupplierCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    gstin: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    phone: str | None = None
    email: str | None = None
    contact_person: str | None = None
    payment_terms: str | None = None
    is_active: bool = True


class SupplierUpdate(BaseModel):
    company_name: str | None = None
    gstin: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    phone: str | None = None
    email: str | None = None
    contact_person: str | None = None
    payment_terms: str | None = None
    is_active: bool | None = None


class SupplierResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    company_name: str
    gstin: str | None
    address: str | None
    city: str | None
    state: str | None
    phone: str | None
    email: str | None
    contact_person: str | None
    payment_terms: str | None
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ── Medicines ─────────────────────────────────────────────────────────────────
class MedicineCreate(BaseModel):
    medicine_name: str = Field(min_length=1, max_length=255)
    generic_name: str | None = None
    brand_name: str = Field(min_length=1, max_length=255)
    manufacturer: str = Field(min_length=1, max_length=255)
    category_id: UUID | None = None
    medicine_type: MedicineType = MedicineType.tablet
    strength: str = ""
    unit: MedicineUnit = MedicineUnit.tablet
    hsn_code: str | None = None
    gst_percent: float = 12.0
    selling_price: float = 0.0
    purchase_price: float = 0.0
    mrp: float = 0.0
    minimum_stock: float = 10.0
    maximum_stock: float = 500.0
    reorder_level: float = 20.0
    storage_condition: str | None = None
    is_schedule_drug: bool = False
    requires_prescription: bool = False
    barcode_value: str | None = None
    qr_code: str | None = None
    description: str | None = None
    is_active: bool = True


class MedicineUpdate(BaseModel):
    medicine_name: str | None = None
    generic_name: str | None = None
    brand_name: str | None = None
    manufacturer: str | None = None
    category_id: UUID | None = None
    medicine_type: MedicineType | None = None
    strength: str | None = None
    unit: MedicineUnit | None = None
    hsn_code: str | None = None
    gst_percent: float | None = None
    selling_price: float | None = None
    purchase_price: float | None = None
    mrp: float | None = None
    minimum_stock: float | None = None
    maximum_stock: float | None = None
    reorder_level: float | None = None
    storage_condition: str | None = None
    is_schedule_drug: bool | None = None
    requires_prescription: bool | None = None
    barcode_value: str | None = None
    qr_code: str | None = None
    description: str | None = None
    is_active: bool | None = None


class MedicineResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    category_id: UUID | None
    category_name: str | None = None
    medicine_name: str
    generic_name: str | None
    brand_name: str
    manufacturer: str
    medicine_type: MedicineType
    strength: str
    unit: MedicineUnit
    hsn_code: str | None
    gst_percent: float
    selling_price: float
    purchase_price: float
    mrp: float
    minimum_stock: float
    maximum_stock: float
    reorder_level: float
    storage_condition: str | None
    is_schedule_drug: bool
    requires_prescription: bool
    barcode_value: str | None
    qr_code: str | None
    description: str | None
    is_active: bool
    current_stock: float = 0.0
    available_stock: float = 0.0
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class BatchResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    medicine_id: UUID
    medicine_name: str | None = None
    supplier_id: UUID | None
    batch_number: str
    manufacture_date: date | None
    expiry_date: date
    purchase_price: float
    selling_price: float
    mrp: float
    initial_quantity: float
    available_quantity: float
    barcode_value: str | None
    barcode_type: str | None
    scanner_reference: str | None
    is_active: bool
    is_expired: bool = False
    expiring_soon: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class InventoryRow(BaseModel):
    medicine_id: UUID
    medicine_name: str
    brand_name: str
    generic_name: str | None
    category_name: str | None
    manufacturer: str
    strength: str
    medicine_type: MedicineType
    current_stock: float
    reserved_stock: float
    available_stock: float
    minimum_stock: float
    nearest_expiry: date | None
    batch_count: int
    status: str
    selling_price: float
    requires_prescription: bool


# ── Purchases ─────────────────────────────────────────────────────────────────
class PurchaseItemCreate(BaseModel):
    medicine_id: UUID
    batch_number: str = Field(min_length=1, max_length=64)
    manufacture_date: date | None = None
    expiry_date: date
    purchase_price: float
    mrp: float
    selling_price: float
    quantity: float = Field(gt=0)
    free_quantity: float = 0.0
    gst_percent: float = 12.0
    discount_percent: float = 0.0


class PurchaseCreate(BaseModel):
    supplier_id: UUID
    invoice_number: str = Field(min_length=1, max_length=64)
    invoice_date: date
    received_date: date
    payment_status: PurchasePaymentStatus = PurchasePaymentStatus.unpaid
    notes: str | None = None
    items: list[PurchaseItemCreate] = Field(min_length=1)


class PurchaseItemResponse(BaseModel):
    id: UUID
    medicine_id: UUID
    medicine_name: str | None = None
    batch_id: UUID | None
    batch_number: str
    manufacture_date: date | None
    expiry_date: date
    purchase_price: float
    mrp: float
    selling_price: float
    quantity: float
    free_quantity: float
    gst_percent: float
    discount_percent: float
    discount_amount: float
    gst_amount: float
    net_amount: float

    class Config:
        from_attributes = True


class PurchaseResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    supplier_id: UUID
    supplier_name: str | None = None
    invoice_number: str
    invoice_date: date
    received_date: date
    payment_status: PurchasePaymentStatus
    subtotal: float
    discount_amount: float
    gst_amount: float
    net_amount: float
    notes: str | None
    created_by_name: str
    created_at: datetime
    items: list[PurchaseItemResponse] = []

    class Config:
        from_attributes = True


# ── Sales (walk-in counter) ───────────────────────────────────────────────────
class SaleItemCreate(BaseModel):
    medicine_id: UUID
    batch_id: UUID | None = None  # if None, FIFO allocate
    quantity: float = Field(gt=0)
    unit_price: float | None = None
    discount_amount: float = 0.0


class SaleCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=255)
    customer_phone: str = Field(min_length=7, max_length=32)
    patient_id: UUID | None = None
    prescription_id: UUID | None = None
    pharmacy_rx_request_id: UUID | None = None
    doctor_name: str | None = None
    sale_type: PharmacySaleType = PharmacySaleType.walk_in
    sale_date: date | None = None
    discount_amount: float = 0.0
    payment_method: str = "cash"
    amount_paid: float | None = None
    notes: str | None = None
    items: list[SaleItemCreate] = Field(min_length=1)


class SaleItemResponse(BaseModel):
    id: UUID
    medicine_id: UUID
    medicine_name: str | None = None
    batch_id: UUID
    batch_number: str
    quantity: float
    unit_price: float
    purchase_price: float
    gst_percent: float
    discount_amount: float
    gst_amount: float
    net_amount: float
    returned_quantity: float

    class Config:
        from_attributes = True


class SaleResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    invoice_number: str
    sale_type: PharmacySaleType
    status: PharmacySaleStatus
    customer_name: str
    customer_phone: str
    patient_id: UUID | None
    prescription_id: UUID | None
    doctor_name: str | None
    sale_date: date
    subtotal: float
    discount_amount: float
    gst_amount: float
    net_amount: float
    amount_paid: float
    payment_status: PharmacyPaymentStatus
    payment_method: str
    notes: str | None
    billing_charge_id: UUID | None
    created_by_name: str
    created_at: datetime
    items: list[SaleItemResponse] = []
    expiry_warnings: list[str] = []

    class Config:
        from_attributes = True


# ── Adjustments / Returns ─────────────────────────────────────────────────────
class AdjustmentCreate(BaseModel):
    medicine_id: UUID
    batch_id: UUID
    adjustment_type: StockAdjustmentType
    quantity: float = Field(gt=0)
    reason: str = Field(min_length=3)


class AdjustmentResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    medicine_id: UUID
    medicine_name: str | None = None
    batch_id: UUID
    batch_number: str | None = None
    adjustment_type: StockAdjustmentType
    quantity: float
    reason: str
    approved_by_name: str
    created_by_name: str
    created_at: datetime

    class Config:
        from_attributes = True


class CustomerReturnCreate(BaseModel):
    sale_id: UUID
    sale_item_id: UUID
    quantity: float = Field(gt=0)
    reason: str = Field(min_length=3)


class PurchaseReturnCreate(BaseModel):
    purchase_id: UUID
    purchase_item_id: UUID
    quantity: float = Field(gt=0)
    reason: str = Field(min_length=3)


class ReturnResponse(BaseModel):
    id: UUID
    return_type: str
    medicine_id: UUID
    batch_id: UUID
    quantity: float
    refund_amount: float
    reason: str
    created_by_name: str
    created_at: datetime

    class Config:
        from_attributes = True


# ── Rx requests ───────────────────────────────────────────────────────────────
class RxRequestItemCreate(BaseModel):
    medicine_id: UUID | None = None
    medicine_name: str
    quantity: float = 1.0
    dosage: str | None = None


class RxRequestCreate(BaseModel):
    prescription_id: UUID | None = None
    patient_id: UUID | None = None
    doctor_id: UUID | None = None
    patient_name: str = ""
    patient_phone: str = ""
    doctor_name: str = ""
    notes: str | None = None
    items: list[RxRequestItemCreate] = Field(min_length=1)


class RxRequestItemResponse(BaseModel):
    id: UUID
    medicine_id: UUID | None
    medicine_name: str
    quantity: float
    dosage: str | None
    dispensed_quantity: float
    status: str

    class Config:
        from_attributes = True


class RxRequestResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    prescription_id: UUID | None
    patient_id: UUID | None
    doctor_id: UUID | None
    patient_name: str
    patient_phone: str
    doctor_name: str
    status: PharmacyRxRequestStatus
    notes: str | None
    created_at: datetime
    items: list[RxRequestItemResponse] = []

    class Config:
        from_attributes = True


# ── Dashboard / Reports ───────────────────────────────────────────────────────
class PharmacyDashboard(BaseModel):
    today_sales_amount: float = 0.0
    today_sales_count: int = 0
    today_purchases_amount: float = 0.0
    today_profit: float = 0.0
    low_stock_count: int = 0
    out_of_stock_count: int = 0
    expired_count: int = 0
    expiring_soon_count: int = 0
    pending_bills_count: int = 0
    pending_rx_count: int = 0
    top_selling: list[dict] = []
    latest_purchases: list[dict] = []
    recent_sales: list[dict] = []
    stock_alerts: list[dict] = []
    monthly_sales: list[dict] = []


class ReportQuery(BaseModel):
    report: Literal[
        "current_stock",
        "low_stock",
        "out_of_stock",
        "expiring_30",
        "expiring_60",
        "purchase",
        "sales",
        "top_selling",
        "slow_moving",
        "supplier_purchases",
        "profit",
        "daily_sales",
        "monthly_sales",
        "category_sales",
        "inventory_valuation",
        "doctor_prescription_sales",
    ]
    from_date: date | None = None
    to_date: date | None = None


class SearchHit(BaseModel):
    """Unified search for name / barcode / batch / QR (scanner-ready)."""

    medicine_id: UUID
    medicine_name: str
    brand_name: str
    strength: str
    batch_id: UUID | None = None
    batch_number: str | None = None
    available_quantity: float = 0.0
    expiry_date: date | None = None
    selling_price: float = 0.0
    match_field: str = "name"
