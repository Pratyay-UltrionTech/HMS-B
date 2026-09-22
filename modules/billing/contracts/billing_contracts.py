"""Pydantic V2 schemas for Billing domain requests and responses.

Conforms to UltrionTech-Backend-Template modules/billing/contracts/ specification.
Includes Financial Accounts, Charges, Payments, Selective Allocations, Deposits, Refunds, and Invoices.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from modules.billing.entities.billing_entities import (
    BillingChargeStatus,
    BillingInvoiceStatus,
    BillingPaymentMethod,
    BillingReceiptStatus,
    BillingSourceType,
    DepositStatus,
    FinancialAccountStatus,
    FinancialAccountType,
    RefundStatus,
)


# ── Financial Accounts (Episodes) ───────────────────────────────────────────

class FinancialAccountCreate(BaseModel):
    patient_id: UUID
    account_type: FinancialAccountType = FinancialAccountType.general
    admission_id: UUID | None = None
    appointment_id: UUID | None = None
    notes: str | None = None


class FinancialAccountResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    account_number: str
    account_type: FinancialAccountType
    status: FinancialAccountStatus
    admission_id: UUID | None = None
    appointment_id: UUID | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    notes: str | None = None
    created_by_name: str = ""
    patient_name: str | None = None
    patient_uhid: str | None = None
    total_charges: float = 0.0
    total_paid: float = 0.0
    outstanding: float = 0.0

    model_config = ConfigDict(from_attributes=True)


# ── Charges ─────────────────────────────────────────────────────────────────

class BillingChargeCreate(BaseModel):
    patient_id: UUID
    account_id: UUID | None = None
    source_type: BillingSourceType = BillingSourceType.other
    source_id: UUID | None = None
    description: str = Field(min_length=1, max_length=512)
    quantity: float = Field(default=1.0, gt=0)
    unit_price: float = Field(default=0.0, ge=0)
    charge_amount: float = Field(ge=0)
    discount_amount: float = Field(default=0, ge=0)
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    discount_reason: str | None = None
    tax_amount: float = Field(default=0, ge=0)
    gst_rate: float | None = Field(default=0, ge=0)
    hsn_sac_code: str | None = None
    notes: str | None = None


class BillingChargeUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=512)
    quantity: float | None = Field(default=None, gt=0)
    unit_price: float | None = Field(default=None, ge=0)
    discount_amount: float | None = Field(default=None, ge=0)
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    discount_reason: str | None = None
    tax_amount: float | None = Field(default=None, ge=0)
    notes: str | None = None
    status: BillingChargeStatus | None = None


class BillingChargeResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    account_id: UUID | None = None
    source_type: BillingSourceType
    source_id: UUID | None = None
    description: str
    quantity: float = 1.0
    unit_price: float = 0.0
    charge_amount: float
    discount_amount: float
    discount_percent: float | None = None
    discount_reason: str | None = None
    tax_amount: float = 0.0
    gst_rate: float | None = 0.0
    hsn_sac_code: str | None = None
    net_amount: float
    amount_paid: float = 0
    status: BillingChargeStatus
    notes: str | None = None
    created_by_name: str = ""
    created_at: datetime
    updated_at: datetime | None = None
    patient_name: str | None = None
    patient_uhid: str | None = None
    account_number: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ── Payment Allocations ─────────────────────────────────────────────────────

class ChargeAllocationItem(BaseModel):
    charge_id: UUID
    amount: float = Field(gt=0)


class BillingPaymentAllocationResponse(BaseModel):
    id: UUID
    payment_id: UUID | None = None
    deposit_id: UUID | None = None
    charge_id: UUID
    allocated_amount: float
    created_by_name: str = ""
    created_at: datetime
    charge_description: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ── Payments ────────────────────────────────────────────────────────────────

class BillingPaymentCreate(BaseModel):
    patient_id: UUID
    account_id: UUID | None = None
    amount: float = Field(gt=0)
    payment_date: date | None = None
    payment_method: BillingPaymentMethod = BillingPaymentMethod.cash
    notes: str | None = None
    reference_number: str | None = Field(default=None, max_length=128)
    linked_invoice_id: UUID | None = None
    # Selective payment allocations (if specified, funds allocate strictly to these charges)
    allocations: list[ChargeAllocationItem] | None = None


class BillingPaymentResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    account_id: UUID | None = None
    amount: float
    payment_date: date
    payment_method: BillingPaymentMethod
    reference_number: str | None = None
    notes: str | None = None
    received_by_name: str = ""
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None
    receipt_id: UUID | None = None
    receipt_number: str | None = None
    allocations: list[BillingPaymentAllocationResponse] = []

    model_config = ConfigDict(from_attributes=True)


# ── Deposits (Advances) ─────────────────────────────────────────────────────

class BillingDepositCreate(BaseModel):
    patient_id: UUID
    account_id: UUID | None = None
    amount: float = Field(gt=0)
    deposit_date: date | None = None
    deposit_type: str = "admission"  # admission, surgical, general
    payment_method: BillingPaymentMethod = BillingPaymentMethod.cash
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = None


class BillingDepositAllocate(BaseModel):
    allocations: list[ChargeAllocationItem] = Field(min_length=1)


class BillingDepositResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    account_id: UUID | None = None
    deposit_number: str
    deposit_date: date
    deposit_type: str
    payment_method: BillingPaymentMethod
    original_amount: float
    available_amount: float
    status: DepositStatus
    reference_number: str | None = None
    notes: str | None = None
    received_by_name: str = ""
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ── Refunds ─────────────────────────────────────────────────────────────────

class BillingRefundCreate(BaseModel):
    patient_id: UUID
    account_id: UUID | None = None
    payment_id: UUID | None = None
    deposit_id: UUID | None = None
    amount: float = Field(gt=0)
    refund_date: date | None = None
    refund_method: BillingPaymentMethod = BillingPaymentMethod.cash
    reason: str = Field(min_length=1, max_length=512)


class BillingRefundResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    account_id: UUID | None = None
    payment_id: UUID | None = None
    deposit_id: UUID | None = None
    refund_number: str
    refund_date: date
    refund_method: BillingPaymentMethod
    amount: float
    reason: str
    status: RefundStatus
    approved_by_name: str = ""
    processed_by_name: str = ""
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ── Invoices ────────────────────────────────────────────────────────────────

class BillingInvoiceLineResponse(BaseModel):
    id: UUID
    charge_id: UUID | None = None
    source_type: str
    category_label: str | None = None
    description: str
    quantity: float = 1
    rate: float = 0
    amount: float = 0
    hsn_sac_code: str | None = None
    gst_rate: float | None = 0
    cgst_amount: float = 0
    sgst_amount: float = 0
    sort_order: int = 0

    model_config = ConfigDict(from_attributes=True)


class BillingInvoiceCreate(BaseModel):
    patient_id: UUID
    account_id: UUID | None = None
    charge_ids: list[UUID] = Field(min_length=1)
    invoice_date: date | None = None
    tax_amount: float = Field(default=0, ge=0)
    cgst_amount: float = Field(default=0, ge=0)
    sgst_amount: float = Field(default=0, ge=0)
    igst_amount: float = Field(default=0, ge=0)
    notes: str | None = None


class BillingInvoiceResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    account_id: UUID | None = None
    invoice_number: str
    invoice_date: date
    subtotal: float
    discount_amount: float
    taxable_amount: float = 0
    tax_amount: float
    cgst_amount: float = 0
    sgst_amount: float = 0
    igst_amount: float = 0
    grand_total: float
    status: BillingInvoiceStatus
    notes: str | None = None
    created_by_name: str = ""
    created_at: datetime
    updated_at: datetime | None = None
    patient_name: str | None = None
    patient_uhid: str | None = None
    lines: list[BillingInvoiceLineResponse] = []

    model_config = ConfigDict(from_attributes=True)


# ── Receipts ────────────────────────────────────────────────────────────────

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
    deposit_id: UUID | None = None
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

    model_config = ConfigDict(from_attributes=True)


# ── Ledger & Dashboard ──────────────────────────────────────────────────────

class LedgerEntry(BaseModel):
    id: str
    entry_type: str  # charge, payment, deposit, refund, deposit_allocation
    occurred_at: datetime | None = None
    description: str
    source_type: str | None = None
    debit: float = 0
    credit: float = 0
    running_balance: float = 0
    status: str | None = None
    ref_id: UUID | None = None
    reference_number: str | None = None


class PatientLedgerResponse(BaseModel):
    patient_id: UUID
    patient_name: str | None = None
    patient_uhid: str | None = None
    total_charges: float
    total_paid: float
    total_deposits_available: float = 0.0
    outstanding: float
    net_patient_balance: float = 0.0  # outstanding minus available deposits
    charge_count: int = 0
    payment_count: int = 0
    charges: list[BillingChargeResponse] = []
    payments: list[BillingPaymentResponse] = []
    deposits: list[BillingDepositResponse] = []
    refunds: list[BillingRefundResponse] = []
    invoices: list[BillingInvoiceResponse] = []
    receipts: list[BillingReceiptResponse] = []
    entries: list[LedgerEntry] = []


class PatientFinancialSummary(BaseModel):
    patient_id: UUID
    total_charges: float = 0
    total_paid: float = 0
    total_deposits_available: float = 0
    outstanding: float = 0
    net_patient_balance: float = 0
    recent_entries: list[LedgerEntry] = []


class BillingDashboardResponse(BaseModel):
    todays_charges: float
    todays_collections: float
    todays_deposits: float = 0
    todays_refunds: float = 0
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
