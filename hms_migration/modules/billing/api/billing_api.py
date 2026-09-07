"""FastAPI router for Billing, Charges, Payments, Invoices, Receipts, and Ledgers.

Conforms to UltrionTech-Backend-Template modules/billing/api/ specification.
Provides complete route parity for all 21 billing endpoints under /billing.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.billing.actions.billing_actions import (
    CancelChargeAction,
    CancelInvoiceAction,
    CancelReceiptAction,
    CreateChargeAction,
    CreateInvoiceAction,
    CreatePaymentAction,
    CreateReceiptAction,
    GetBillingDashboardAction,
    GetInvoiceAction,
    GetPatientLedgerAction,
    GetPatientSummaryAction,
    GetReceiptAction,
    ListChargesAction,
    ListInvoicesAction,
    ListPaymentsAction,
    ListReceiptsAction,
    PrintInvoiceAction,
    PrintReceiptAction,
    UpdateChargeAction,
)
from hms_migration.modules.billing.contracts.billing_contracts import (
    BillingChargeCreate,
    BillingChargeResponse,
    BillingChargeUpdate,
    BillingDashboardResponse,
    BillingInvoiceCreate,
    BillingInvoiceResponse,
    BillingPaymentCreate,
    BillingPaymentResponse,
    BillingReceiptCreate,
    BillingReceiptResponse,
    PatientFinancialSummary,
    PatientLedgerResponse,
)
from hms_migration.modules.billing.entities.billing_entities import (
    BillingChargeStatus,
    BillingInvoiceStatus,
    BillingPaymentMethod,
    BillingSourceType,
)
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/billing", tags=["billing"])


def _html_response(html: str, filename: str, download: bool) -> StreamingResponse:
    disposition = "attachment" if download else "inline"
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


# ── Dashboard ───────────────────────────────────────────────────────────────

@router.get("/dashboard", response_model=BillingDashboardResponse)
def get_billing_dashboard(
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingDashboardResponse:
    return GetBillingDashboardAction(db, hospital_id).execute()


# ── Charges ─────────────────────────────────────────────────────────────────

@router.get("/charges", response_model=list[BillingChargeResponse])
def list_charges(
    patient_id: UUID | None = Query(default=None),
    status_filter: BillingChargeStatus | None = Query(default=None, alias="status"),
    source_type: BillingSourceType | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    search: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[BillingChargeResponse]:
    return ListChargesAction(db, hospital_id).execute(
        patient_id=patient_id,
        status=status_filter,
        source_type=source_type,
        from_date=from_date,
        to_date=to_date,
        search=search,
    )


@router.post("/charges", response_model=BillingChargeResponse, status_code=status.HTTP_201_CREATED)
def create_charge(
    payload: BillingChargeCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingChargeResponse:
    return CreateChargeAction(db, hospital_id).execute(payload, user)


@router.put("/charges/{charge_id}", response_model=BillingChargeResponse)
def update_charge(
    charge_id: UUID,
    payload: BillingChargeUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingChargeResponse:
    return UpdateChargeAction(db, hospital_id).execute(charge_id, payload, user)


@router.post("/charges/{charge_id}/cancel", response_model=BillingChargeResponse)
def cancel_charge(
    charge_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingChargeResponse:
    return CancelChargeAction(db, hospital_id).execute(charge_id, user)


# ── Payments ────────────────────────────────────────────────────────────────

@router.get("/payments", response_model=list[BillingPaymentResponse])
def list_payments(
    patient_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    payment_method: BillingPaymentMethod | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[BillingPaymentResponse]:
    return ListPaymentsAction(db, hospital_id).execute(
        patient_id=patient_id,
        from_date=from_date,
        to_date=to_date,
        payment_method=payment_method,
    )


@router.post("/payments", response_model=BillingPaymentResponse, status_code=status.HTTP_201_CREATED)
def record_payment(
    payload: BillingPaymentCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingPaymentResponse:
    return CreatePaymentAction(db, hospital_id).execute(payload, user)


# ── Invoices ────────────────────────────────────────────────────────────────

@router.get("/invoices", response_model=list[BillingInvoiceResponse])
def list_invoices(
    patient_id: UUID | None = Query(default=None),
    status_filter: BillingInvoiceStatus | None = Query(default=None, alias="status"),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    search: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[BillingInvoiceResponse]:
    return ListInvoicesAction(db, hospital_id).execute(
        patient_id=patient_id,
        status=status_filter,
        from_date=from_date,
        to_date=to_date,
        search=search,
    )


@router.get("/invoices/{invoice_id}", response_model=BillingInvoiceResponse)
def get_invoice(
    invoice_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingInvoiceResponse:
    return GetInvoiceAction(db, hospital_id).execute(invoice_id)


@router.post("/invoices", response_model=BillingInvoiceResponse, status_code=status.HTTP_201_CREATED)
def create_invoice(
    payload: BillingInvoiceCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingInvoiceResponse:
    return CreateInvoiceAction(db, hospital_id).execute(payload, user)


@router.post("/invoices/{invoice_id}/cancel", response_model=BillingInvoiceResponse)
def cancel_invoice(
    invoice_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingInvoiceResponse:
    return CancelInvoiceAction(db, hospital_id).execute(invoice_id, user)


@router.get("/invoices/{invoice_id}/print")
def print_invoice(
    invoice_id: UUID,
    download: bool = Query(default=False),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    html = PrintInvoiceAction(db, hospital_id).execute(invoice_id, auto_print=not download)
    return _html_response(html, f"{invoice_id}.html", download)


@router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(
    invoice_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    html = PrintInvoiceAction(db, hospital_id).execute(invoice_id, auto_print=False)
    return _html_response(html, f"{invoice_id}.html", download=True)


# ── Receipts ────────────────────────────────────────────────────────────────

@router.get("/receipts", response_model=list[BillingReceiptResponse])
def list_receipts(
    patient_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[BillingReceiptResponse]:
    return ListReceiptsAction(db, hospital_id).execute(
        patient_id=patient_id, from_date=from_date, to_date=to_date
    )


@router.get("/receipts/{receipt_id}", response_model=BillingReceiptResponse)
def get_receipt(
    receipt_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingReceiptResponse:
    return GetReceiptAction(db, hospital_id).execute(receipt_id)


@router.post("/receipts", response_model=BillingReceiptResponse, status_code=status.HTTP_201_CREATED)
def create_receipt(
    payload: BillingReceiptCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingReceiptResponse:
    return CreateReceiptAction(db, hospital_id).execute(payload, user)


@router.post("/receipts/{receipt_id}/cancel", response_model=BillingReceiptResponse)
def cancel_receipt(
    receipt_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> BillingReceiptResponse:
    return CancelReceiptAction(db, hospital_id).execute(receipt_id, user)


@router.get("/receipts/{receipt_id}/print")
def print_receipt(
    receipt_id: UUID,
    download: bool = Query(default=False),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    html = PrintReceiptAction(db, hospital_id).execute(receipt_id, auto_print=not download)
    return _html_response(html, f"{receipt_id}.html", download)


@router.get("/receipts/{receipt_id}/pdf")
def download_receipt_pdf(
    receipt_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> StreamingResponse:
    html = PrintReceiptAction(db, hospital_id).execute(receipt_id, auto_print=False)
    return _html_response(html, f"{receipt_id}.html", download=True)


# ── Patient Ledger & Summary ────────────────────────────────────────────────

@router.get("/patients/{patient_id}/ledger", response_model=PatientLedgerResponse)
def get_patient_ledger(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PatientLedgerResponse:
    return GetPatientLedgerAction(db, hospital_id).execute(patient_id)


@router.get("/patients/{patient_id}/summary", response_model=PatientFinancialSummary)
def get_patient_financial_summary(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PatientFinancialSummary:
    return GetPatientSummaryAction(db, hospital_id).execute(patient_id)
