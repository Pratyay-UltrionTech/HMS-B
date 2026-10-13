"""
Comprehensive tests for Billing Receipt Historical Immutability (Closure Issue 1).

Covers:
- Test A: New receipt historical immutability across subsequent patient charges.
- Test B: Printing/viewing does not mutate stored database snapshot columns.
- Test C: Legacy NULL snapshot receipt remains NULL when printed, and displays current info labeled distinctly.
- Test D: Snapshot calculation failure rolls back transaction; no incomplete receipt is committed.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infrastructure.postgres.session import get_transitional_sync_session
from modules.billing.actions.billing_actions import PrintReceiptAction
from modules.billing.api.billing_api import router as billing_router
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingPayment,
    BillingPaymentAllocation,
    BillingPaymentMethod,
    BillingReceipt,
    BillingReceiptStatus,
    BillingSourceType,
)
from modules.billing.services.billing_service import patient_ledger_totals
from modules.billing.services.invoice_service import issue_receipt, receipt_html
from modules.billing.services.receipt_financial_context import (
    build_receipt_financial_context,
)
from shared.exceptions import register_exception_handlers
from shared.exceptions.base import FinancialIntegrityError
from tests.conftest import (
    admin_headers,
    auth_headers,
    db_session,
    hospital,
    patient,
)


@pytest.fixture(scope="function")
def billing_app(db_session: Session) -> FastAPI:
    app = FastAPI(title="Billing Immutability Test App")
    register_exception_handlers(app)
    app.include_router(billing_router, prefix="/api")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_transitional_sync_session] = _override_db
    return app


@pytest.fixture(scope="function")
def billing_client(billing_app: FastAPI) -> TestClient:
    return TestClient(billing_app)


def test_a_new_receipt_historical_immutability(db_session: Session, hospital, patient):
    """Test A: New receipt stores snapshot at issuance; subsequent charges never alter historical snapshot."""
    # 1. Charge of 500
    chg1 = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        description="Initial Consultation",
        quantity=Decimal("1.0"),
        unit_price=Decimal("500.00"),
        charge_amount=Decimal("500.00"),
        net_amount=Decimal("500.00"),
        amount_paid=Decimal("500.00"),
        status=BillingChargeStatus.paid,
    )
    db_session.add(chg1)
    db_session.flush()

    # 2. Payment of 500
    pmt1 = BillingPayment(
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=Decimal("500.00"),
        payment_method=BillingPaymentMethod.cash,
        payment_date=date.today(),
        reference_number="CASH-500",
        received_by_name="Cashier",
    )
    db_session.add(pmt1)
    db_session.flush()

    alloc1 = BillingPaymentAllocation(
        hospital_id=hospital.id,
        payment_id=pmt1.id,
        charge_id=chg1.id,
        allocated_amount=Decimal("500.00"),
    )
    db_session.add(alloc1)
    db_session.flush()

    # 3. Issue receipt
    rcpt = issue_receipt(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=500.00,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        payment_id=pmt1.id,
        collected_by_name="Cashier",
    )
    db_session.commit()

    assert rcpt.account_balance_after_snapshot is not None
    assert float(rcpt.account_balance_after_snapshot) == 0.0
    assert float(rcpt.patient_outstanding_after_snapshot) == 0.0

    # 4. Subsequent new charge posted for patient (₹2,000)
    chg2 = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        description="Later Procedure",
        quantity=Decimal("1.0"),
        unit_price=Decimal("2000.00"),
        charge_amount=Decimal("2000.00"),
        net_amount=Decimal("2000.00"),
        amount_paid=Decimal("0.00"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(chg2)
    db_session.commit()

    # Dynamic live ledger now reflects the new charge (2000.0)
    live_ledger = patient_ledger_totals(db_session, hospital.id, patient.id)
    assert float(live_ledger["outstanding"]) == 2000.0

    # 5. Reload & reprint original receipt: stored snapshot must remain ₹0.00
    db_session.expire_all()
    reloaded_rcpt = db_session.query(BillingReceipt).filter(BillingReceipt.id == rcpt.id).one()
    assert float(reloaded_rcpt.account_balance_after_snapshot) == 0.0

    ctx = build_receipt_financial_context(db_session, hospital.id, reloaded_rcpt)
    assert ctx.account_balance_after_payment == 0.0
    assert ctx.patient_overall_outstanding_after_payment == 0.0

    html = receipt_html(reloaded_rcpt, hospital, patient, financial_context=ctx)
    assert "₹0.00" in html
    assert "2,000.00" not in html


def test_b_printing_does_not_mutate(billing_client: TestClient, db_session: Session, hospital, patient, admin_headers):
    """Test B: Calling receipt detail, print API, and HTML renderer multiple times does not mutate database snapshot fields."""
    pmt = BillingPayment(
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=Decimal("350.00"),
        payment_method=BillingPaymentMethod.upi,
        payment_date=date.today(),
        reference_number="UPI-350",
    )
    db_session.add(pmt)
    db_session.flush()

    rcpt = issue_receipt(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=350.00,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.upi,
        payment_id=pmt.id,
    )
    db_session.commit()

    # Record all snapshot column values
    snapshot_before = {
        "account_id": rcpt.account_id_snapshot,
        "amount_allocated": rcpt.amount_allocated_snapshot,
        "balance_before": rcpt.account_balance_before_snapshot,
        "balance_after": rcpt.account_balance_after_snapshot,
        "patient_outstanding": rcpt.patient_outstanding_after_snapshot,
        "available_deposit": rcpt.available_deposit_after_snapshot,
        "net_patient_balance": rcpt.net_patient_balance_after_snapshot,
    }

    # Call receipt detail API multiple times
    for _ in range(3):
        res = billing_client.get(f"/api/billing/receipts/{rcpt.id}", headers=admin_headers)
        assert res.status_code == 200

    # Call receipt print API multiple times
    for _ in range(3):
        res = billing_client.get(f"/api/billing/receipts/{rcpt.id}/print", headers=admin_headers)
        assert res.status_code == 200

    # Call HTML renderer directly multiple times
    for _ in range(3):
        PrintReceiptAction(db_session, hospital.id).execute(rcpt.id, auto_print=False)

    # Verify database snapshot values are completely identical
    db_session.expire_all()
    fresh = db_session.query(BillingReceipt).filter(BillingReceipt.id == rcpt.id).one()
    snapshot_after = {
        "account_id": fresh.account_id_snapshot,
        "amount_allocated": fresh.amount_allocated_snapshot,
        "balance_before": fresh.account_balance_before_snapshot,
        "balance_after": fresh.account_balance_after_snapshot,
        "patient_outstanding": fresh.patient_outstanding_after_snapshot,
        "available_deposit": fresh.available_deposit_after_snapshot,
        "net_patient_balance": fresh.net_patient_balance_after_snapshot,
    }
    assert snapshot_after == snapshot_before


def test_c_legacy_null_snapshot_remains_null_when_printed(
    billing_client: TestClient, db_session: Session, hospital, patient, admin_headers
):
    """Test C: Legacy receipts with NULL snapshot fields remain NULL when printed, and display current state labeled clearly."""
    # 1. Create a legacy receipt directly with NULL snapshot fields
    legacy_rcpt = BillingReceipt(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        receipt_number="RCPT-LEGACY-001",
        payment_date=date(2025, 1, 15),
        payment_method=BillingPaymentMethod.cash,
        amount=Decimal("1500.00"),
        status=BillingReceiptStatus.issued,
        collected_by_name="Old Cashier",
        account_id_snapshot=None,
        amount_allocated_snapshot=None,
        account_balance_before_snapshot=None,
        account_balance_after_snapshot=None,
        patient_outstanding_after_snapshot=None,
        available_deposit_after_snapshot=None,
        net_patient_balance_after_snapshot=None,
    )
    db_session.add(legacy_rcpt)
    db_session.commit()

    # 2. Add an outstanding charge so current balance is non-zero
    chg = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        description="Current Charge",
        quantity=Decimal("1.0"),
        unit_price=Decimal("750.00"),
        charge_amount=Decimal("750.00"),
        net_amount=Decimal("750.00"),
        amount_paid=Decimal("0.00"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(chg)
    db_session.commit()

    # 3. Print legacy receipt via action and via API
    html = PrintReceiptAction(db_session, hospital.id).execute(legacy_rcpt.id, auto_print=False)

    # Verify context is labeled as CURRENT account balance and includes disclaimer
    assert "Current Account Balance" in html
    assert "Historical balance snapshot unavailable for this legacy receipt" in html

    # Also test via billing print endpoint
    res = billing_client.get(f"/api/billing/receipts/{legacy_rcpt.id}/print", headers=admin_headers)
    assert res.status_code == 200
    assert "Current Account Balance" in res.text

    # Verify DB snapshot columns were NOT backfilled and remain NULL
    db_session.expire_all()
    fresh = db_session.query(BillingReceipt).filter(BillingReceipt.id == legacy_rcpt.id).one()
    assert fresh.account_balance_after_snapshot is None
    assert fresh.amount_allocated_snapshot is None
    assert fresh.patient_outstanding_after_snapshot is None


def test_d_snapshot_calculation_failure_rolls_back_transaction(db_session: Session, hospital, patient):
    """Test D: If snapshot calculation fails during receipt creation, no incomplete receipt is committed."""
    initial_receipt_count = db_session.query(BillingReceipt).filter(BillingReceipt.hospital_id == hospital.id).count()

    pmt = BillingPayment(
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=Decimal("800.00"),
        payment_method=BillingPaymentMethod.cash,
        payment_date=date.today(),
    )
    db_session.add(pmt)
    db_session.flush()

    # Inject a failure into compute_receipt_snapshots
    with patch(
        "modules.billing.services.receipt_financial_context.compute_receipt_snapshots",
        side_effect=RuntimeError("Simulated snapshot corruption / calculation failure"),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            issue_receipt(
                db_session,
                hospital_id=hospital.id,
                patient_id=patient.id,
                amount=800.00,
                payment_date=date.today(),
                payment_method=BillingPaymentMethod.cash,
                payment_id=pmt.id,
            )
        assert "Simulated snapshot corruption" in str(exc_info.value)
        db_session.rollback()

    # Verify no receipt was committed
    final_receipt_count = db_session.query(BillingReceipt).filter(BillingReceipt.hospital_id == hospital.id).count()
    assert final_receipt_count == initial_receipt_count
