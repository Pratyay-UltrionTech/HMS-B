from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import (
    BillingChargeStatus,
    BillingInvoiceStatus,
    BillingPaymentMethod,
    BillingReceiptStatus,
    BillingSourceType,
)


class BillingChargeCreate(BaseModel):
    patient_id: UUID
    source_type: BillingSourceType = BillingSourceType.other
    source_id: UUID | None = None
    description: str = Field(min_length=1, max_length=512)
    charge_amount: float = Field(ge=0)
    discount_amount: float = Field(default=0, ge=0)
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    notes: str | None = None


class BillingChargeUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=512)
    discount_amount: float | None = Field(default=None, ge=0)
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    notes: str | None = None
    status: BillingChargeStatus | None = None


class BillingChargeResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    source_type: BillingSourceType
    source_id: UUID | None = None
    description: str
    charge_amount: float
    discount_amount: float
    discount_percent: float | None = None
    net_amount: float
    amount_paid: float = 0
    status: BillingChargeStatus
    notes: str | None = None
    created_by_name: str = ""
    created_at: datetime
    updated_at: datetime | None = None
    patient_name: str | None = None
    patient_uhid: str | None = None

    model_config = {"from_attributes": True}


class BillingPaymentCreate(BaseModel):
    patient_id: UUID
    amount: float = Field(gt=0)
    payment_date: date | None = None
    payment_method: BillingPaymentMethod = BillingPaymentMethod.cash
    notes: str | None = None
    reference_number: str | None = Field(default=None, max_length=128)
    linked_invoice_id: UUID | None = None


class BillingPaymentResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    amount: float
    payment_date: date
    payment_method: BillingPaymentMethod
    notes: str | None = None
    received_by_name: str = ""
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None
    receipt_id: UUID | None = None
    receipt_number: str | None = None

    model_config = {"from_attributes": True}


class BillingInvoiceLineResponse(BaseModel):
    id: UUID
    charge_id: UUID | None = None
    source_type: str
    category_label: str | None = None
    description: str
    quantity: float = 1
    rate: float = 0
    amount: float = 0
    sort_order: int = 0

    model_config = {"from_attributes": True}


class BillingInvoiceCreate(BaseModel):
    patient_id: UUID
    charge_ids: list[UUID] = Field(min_length=1)
    invoice_date: date | None = None
    tax_amount: float = Field(default=0, ge=0)
    notes: str | None = None


class BillingInvoiceResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    invoice_number: str
    invoice_date: date
    subtotal: float
    discount_amount: float
    tax_amount: float
    grand_total: float
    status: BillingInvoiceStatus
    notes: str | None = None
    created_by_name: str = ""
    created_at: datetime
    updated_at: datetime | None = None
    patient_name: str | None = None
    patient_uhid: str | None = None
    lines: list[BillingInvoiceLineResponse] = []

    model_config = {"from_attributes": True}


class BillingReceiptCreate(BaseModel):
    patient_id: UUID
    amount: float = Field(gt=0)
    payment_date: date | None = None
    payment_method: BillingPaymentMethod = BillingPaymentMethod.cash
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = None
    linked_invoice_id: UUID | None = None
    create_payment: bool = True


class BillingReceiptResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    payment_id: UUID | None = None
    linked_invoice_id: UUID | None = None
    receipt_number: str
    payment_date: date
    payment_method: BillingPaymentMethod
    amount: float
    reference_number: str | None = None
    notes: str | None = None
    status: BillingReceiptStatus
    collected_by_name: str = ""
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None

    model_config = {"from_attributes": True}


class LedgerEntry(BaseModel):
    id: str
    entry_type: str
    occurred_at: datetime | None = None
    description: str
    source_type: str | None = None
    debit: float = 0
    credit: float = 0
    status: str | None = None
    ref_id: UUID | None = None


class PatientLedgerResponse(BaseModel):
    patient_id: UUID
    patient_name: str | None = None
    patient_uhid: str | None = None
    total_charges: float
    total_paid: float
    outstanding: float
    charge_count: int = 0
    payment_count: int = 0
    charges: list[BillingChargeResponse] = []
    payments: list[BillingPaymentResponse] = []
    invoices: list[BillingInvoiceResponse] = []
    receipts: list[BillingReceiptResponse] = []
    entries: list[LedgerEntry] = []


class PatientFinancialSummary(BaseModel):
    patient_id: UUID
    total_charges: float = 0
    total_paid: float = 0
    outstanding: float = 0
    recent_entries: list[LedgerEntry] = []


class BillingDashboardResponse(BaseModel):
    todays_charges: float
    todays_collections: float
    outstanding_total: float
    pending_charges_count: int
    todays_ot_revenue: float = 0
    todays_ipd_revenue: float = 0
    outstanding_by_category: dict[str, float] = {}
    today_invoice_count: int = 0
    today_receipt_count: int = 0
    total_invoiced: float = 0
    total_collected: float = 0
    recent_charges: list[BillingChargeResponse] = []
    recent_payments: list[BillingPaymentResponse] = []
