"""Pydantic V2 schemas for Billing domain requests and responses.

Conforms to UltrionTech-Backend-Template modules/billing/contracts/ specification.
Includes Financial Accounts, Charges, Payments, Selective Allocations, Deposits, Refunds, and Invoices.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    explicit_unscoped: bool = False

    @model_validator(mode="after")
    def validate_discount_reason(self) -> "BillingChargeCreate":
        has_discount = (self.discount_amount and self.discount_amount > 0) or (
            self.discount_percent is not None and self.discount_percent > 0
        )
        if has_discount:
            if not self.discount_reason or not self.discount_reason.strip():
                raise ValueError("Discount reason is mandatory when applying a discount")
            self.discount_reason = self.discount_reason.strip()
        return self


class BillingChargeUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=512)
    quantity: float | None = Field(default=None, gt=0)
    unit_price: float | None = Field(default=None, ge=0)
    discount_amount: float | None = Field(default=None, ge=0)
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    discount_reason: str | None = None
    tax_amount: float | None = Field(default=None, ge=0)
    notes: str | None = None
    # Settlement status is derived from allocations and cannot be edited; any
    # value is rejected with 400 (use POST /charges/{id}/cancel to cancel).
    status: BillingChargeStatus | None = None

    @model_validator(mode="after")
    def validate_discount_reason(self) -> "BillingChargeUpdate":
        has_discount = (self.discount_amount is not None and self.discount_amount > 0) or (
            self.discount_percent is not None and self.discount_percent > 0
        )
        if has_discount:
            if not self.discount_reason or not self.discount_reason.strip():
                raise ValueError("Discount reason is mandatory when applying a discount")
            self.discount_reason = self.discount_reason.strip()
        return self



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
    # Zero tender is allowed only when the line carries a discount
    # (discount-only / fully-waived line at settlement).
    amount: float = Field(ge=0)
    # Optional inline discount applied at payment settlement
    discount_amount: float | None = Field(default=None, ge=0)
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    discount_reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_allocation_line(self) -> "ChargeAllocationItem":
        has_discount = (self.discount_amount or 0) > 0 or (self.discount_percent or 0) > 0
        if (self.amount or 0) <= 0 and not has_discount:
            raise ValueError("allocation amount must be greater than 0 unless a discount is applied")
        if has_discount and not (self.discount_reason and self.discount_reason.strip()):
            raise ValueError("discount_reason is mandatory whenever a discount is applied")
        return self


class BillingPaymentAllocationResponse(BaseModel):
    id: UUID
    payment_id: UUID | None = None
    deposit_id: UUID | None = None
    charge_id: UUID
    # Negative for reversal rows (refund / cancellation / re-price release).
    allocated_amount: float
    reversal_of_id: UUID | None = None
    refund_id: UUID | None = None
    credit_deposit_id: UUID | None = None
    reversal_reason: str | None = None
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
    idempotency_key: str | None = Field(default=None, max_length=128)
    linked_invoice_id: UUID | None = None
    # Selective payment allocations (if specified, funds allocate strictly to these charges)
    allocations: list[ChargeAllocationItem] | None = None
    # Overall discount applied at payment settlement
    discount_amount: float | None = Field(default=None, ge=0)
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    discount_reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_payment_discount(self) -> "BillingPaymentCreate":
        disc_amt = self.discount_amount or 0.0
        disc_pct = self.discount_percent or 0.0
        if (disc_amt > 0 or disc_pct > 0) and not (self.discount_reason and self.discount_reason.strip()):
            raise ValueError("discount_reason is mandatory whenever a discount is applied at payment")
        return self


class BillingPaymentResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    account_id: UUID | None = None
    amount: float
    payment_date: date
    payment_method: BillingPaymentMethod
    reference_number: str | None = None
    idempotency_key: str | None = None
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

class BillingPaymentListItem(BaseModel):
    """Flat register row, without allocations, notes, or clinical detail."""
    id: UUID
    patient_id: UUID
    amount: float
    payment_date: date
    payment_method: BillingPaymentMethod
    reference_number: str | None = None
    received_by_name: str = ""
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None
    receipt_id: UUID | None = None
    receipt_number: str | None = None


class BillingPaymentPage(BaseModel):
    items: list[BillingPaymentListItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    total_collected: float


class BillingDepositCreate(BaseModel):
    patient_id: UUID
    account_id: UUID | None = None
    admission_id: UUID | None = None
    amount: float = Field(gt=0)
    deposit_date: date | None = None
    deposit_type: str = "admission"  # admission, surgical, general
    payment_method: BillingPaymentMethod = BillingPaymentMethod.cash
    reference_number: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    notes: str | None = None


class BillingDepositAllocate(BaseModel):
    allocations: list[ChargeAllocationItem] = Field(min_length=1)


class BillingDepositResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    account_id: UUID | None = None
    admission_id: UUID | None = None
    deposit_number: str
    deposit_date: date
    deposit_type: str
    payment_method: BillingPaymentMethod
    original_amount: float
    available_amount: float
    status: DepositStatus
    reference_number: str | None = None
    idempotency_key: str | None = None
    notes: str | None = None
    received_by_name: str = ""
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None

    allocated_amount: float | None = None

    model_config = ConfigDict(from_attributes=True)


class BillingDepositPage(BaseModel):
    items: list[BillingDepositResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
    total_deposited: float
    total_available: float
    total_allocated: float
    total_refunded: float = 0.0


# ── Refunds ─────────────────────────────────────────────────────────────────

class BillingRefundCreate(BaseModel):
    patient_id: UUID
    account_id: UUID | None = None
    # Exactly one source is required (validated in the service → 400).
    payment_id: UUID | None = None
    deposit_id: UUID | None = None
    amount: float = Field(gt=0)
    refund_date: date | None = None
    refund_method: BillingPaymentMethod = BillingPaymentMethod.cash
    reason: str = Field(min_length=1, max_length=512)
    # Payment refunds only: reverse allocations on these charges (default: newest first).
    charge_ids: list[UUID] | None = None


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
    subtotal: float = 0.0
    discount_amount: float = 0.0
    taxable_amount: float | None = 0.0
    tax_amount: float = 0.0
    cgst_amount: float | None = 0.0
    sgst_amount: float | None = 0.0
    igst_amount: float | None = 0.0
    grand_total: float = 0.0
    amount_paid: float = 0.0
    balance_due: float = 0.0
    status: BillingInvoiceStatus
    notes: str | None = None
    created_by_name: str | None = ""
    created_at: datetime
    updated_at: datetime | None = None
    patient_name: str | None = None
    patient_uhid: str | None = None
    reissue_eligible: bool = False
    reissue_reason: str | None = None
    lines: list[BillingInvoiceLineResponse] = []

    model_config = ConfigDict(from_attributes=True)


class BillingInvoiceReissueRequest(BaseModel):
    reason: str | None = None


class BillingBillPreparationResponse(BaseModel):
    account_id: UUID
    admission_id: UUID | None = None
    as_of: str
    total_charges: float
    total_paid: float
    outstanding: float
    charges: list[BillingChargeResponse] = []


class BillingInvoiceListItem(BaseModel):
    """Flat lightweight register row, without line items or clinical detail."""
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    account_id: UUID | None = None
    invoice_number: str
    invoice_date: date
    subtotal: float = 0.0
    discount_amount: float = 0.0
    taxable_amount: float | None = 0.0
    tax_amount: float = 0.0
    cgst_amount: float | None = 0.0
    sgst_amount: float | None = 0.0
    igst_amount: float | None = 0.0
    grand_total: float = 0.0
    amount_paid: float = 0.0
    balance_due: float = 0.0
    status: BillingInvoiceStatus
    created_by_name: str | None = ""
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None

    model_config = ConfigDict(from_attributes=True)


class BillingInvoicePage(BaseModel):
    items: list[BillingInvoiceListItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    total_amount: float


class BillingInvoiceReissueRequest(BaseModel):
    reason: str = "Superseded because billable charges changed after generation"
    notes: str | None = None


class BillingBillPreparationResponse(BaseModel):
    account_id: UUID
    admission_id: UUID | None = None
    as_of: str
    total_charges: float
    total_paid: float
    outstanding: float
    charges: list[BillingChargeResponse]


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


class PatientDuesResponse(BaseModel):
    patient_id: UUID
    patient_name: str | None = None
    patient_uhid: str | None = None
    outstanding: float = 0.0
    total_deposits_available: float = 0.0
    pending_charges: list[BillingChargeResponse] = []
    modal_invoices: list[BillingInvoiceResponse] = []


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
    recent_charges: list[BillingChargeResponse] = []
    recent_payments: list[BillingPaymentResponse] = []


# ── Canonical Insurance & Consolidated Statement Contracts ──────────────────

class StatementHospitalInfo(BaseModel):
    hospital_id: UUID
    name: str
    hospital_code: str
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    gstin: str | None = None
    tax_identifier: str | None = None  # backward-compatible alias for gstin
    registration_number: str | None = None


class StatementPatientInfo(BaseModel):
    patient_id: UUID
    name: str
    uhid: str  # MRN canonical identifier
    gender: str | None = None
    age: int | None = None
    date_of_birth: date | None = None
    mobile: str | None = None
    emergency_contact: str | None = None
    address: str | None = None


class StatementScopeInfo(BaseModel):
    scope: str = "entire_journey"  # "entire_journey" | "date_range" | "episode"
    date_from: date | None = None
    date_to: date | None = None
    source_filter: str | None = None
    account_number: str | None = None
    total_episodes: int = 1


class StatementItemizedCharge(BaseModel):
    id: UUID
    charge_date: datetime
    source_type: str
    source_id: UUID | None = None
    description: str
    quantity: float = 1.0
    unit_price: float = 0.0
    gross_amount: float = 0.0
    discount_amount: float = 0.0
    tax_amount: float = 0.0
    net_amount: float = 0.0
    amount_paid: float = 0.0
    outstanding: float = 0.0
    status: str
    reference_number: str | None = None
    account_id: UUID | None = None
    account_number: str | None = None


class StatementPayment(BaseModel):
    id: UUID
    payment_date: date
    amount: float
    payment_method: str
    reference_number: str | None = None
    receipt_number: str | None = None
    receipt_id: UUID | None = None
    received_by_name: str = ""
    account_number: str | None = None


class StatementDeposit(BaseModel):
    id: UUID
    deposit_number: str
    deposit_date: date
    deposit_type: str
    payment_method: str
    original_amount: float
    available_amount: float
    used_amount: float
    status: str
    reference_number: str | None = None


class StatementRefund(BaseModel):
    id: UUID
    refund_number: str
    refund_date: date
    refund_method: str
    amount: float
    reason: str
    status: str
    reference_number: str | None = None


class StatementFinancialSummary(BaseModel):
    gross_charges: float
    discount_total: float
    tax_total: float
    net_charges: float
    total_paid: float
    deposits_received: float
    deposits_applied: float
    deposits_available: float
    total_refunds: float
    outstanding: float
    net_patient_balance: float


class PatientBillingStatementResponse(BaseModel):
    statement_id: str
    generated_at: datetime
    hospital: StatementHospitalInfo
    patient: StatementPatientInfo
    scope: StatementScopeInfo
    charges: list[StatementItemizedCharge]
    payments: list[StatementPayment]
    receipts: list[BillingReceiptResponse]
    invoices: list[BillingInvoiceResponse]
    deposits: list[StatementDeposit]
    refunds: list[StatementRefund]
    summary: StatementFinancialSummary

