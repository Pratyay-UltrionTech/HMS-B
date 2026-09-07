"""Unit and integration tests for migrated Billing domain.

Conforms to UltrionTech-Backend-Template modules/billing/tests/ specification.
Tests charges, FIFO payment allocation, invoices, receipts, and patient ledgers.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db as legacy_get_db
from app.models import Hospital, Patient
from hms_migration.infrastructure.postgres import get_transitional_sync_session
from hms_migration.modules.billing.api.billing_api import router as billing_router
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingSourceType,
)
from hms_migration.modules.billing.services.billing_service import (
    allocate_payment_to_charges,
    compute_net,
    ensure_charge,
    patient_ledger_totals,
)
from hms_migration.shared.exceptions import register_exception_handlers
from tests.conftest import (
    admin_headers,
    auth_headers,
    auth_headers_b,
    db_session,
    hospital,
    hospital_b,
    patient,
    patient_b,
)


@pytest.fixture(scope="function")
def billing_app(db_session: Session) -> FastAPI:
    app = FastAPI(title="Billing Test App")
    register_exception_handlers(app)
    app.include_router(billing_router, prefix="/api")

    def _override_db():
        yield db_session

    app.dependency_overrides[legacy_get_db] = _override_db
    app.dependency_overrides[get_transitional_sync_session] = _override_db
    return app


@pytest.fixture(scope="function")
def billing_client(billing_app: FastAPI) -> TestClient:
    return TestClient(billing_app)


# ── Domain Math / Calculation Tests ─────────────────────────────────────────

def test_compute_net_calculations():
    """Verify net calculation with absolute and percentage discounts."""
    disc, net = compute_net(1000.0, discount_amount=150.0)
    assert disc == 150.0
    assert net == 850.0

    disc2, net2 = compute_net(1000.0, discount_percent=20.0)
    assert disc2 == 200.0
    assert net2 == 800.0

    # Discount capped at charge amount
    disc3, net3 = compute_net(500.0, discount_amount=800.0)
    assert disc3 == 500.0
    assert net3 == 0.0


def test_fifo_payment_allocation(db_session: Session, hospital: Hospital, patient: Patient):
    """Verify FIFO payment allocation across multiple charges."""
    c1 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid4(),
        description="Dr Consultation",
        charge_amount=500.0,
    )
    c2 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="CBC Blood Test",
        charge_amount=300.0,
    )
    db_session.commit()

    # Partial payment covering c1 and part of c2
    allocate_payment_to_charges(db_session, hospital.id, patient.id, 650.0)
    db_session.commit()
    db_session.refresh(c1)
    db_session.refresh(c2)

    assert c1.status == BillingChargeStatus.paid
    assert c1.amount_paid == 500.0
    assert c2.status == BillingChargeStatus.partially_paid
    assert c2.amount_paid == 150.0

    totals = patient_ledger_totals(db_session, hospital.id, patient.id)
    assert totals["total_charges"] == 800.0


# ── API Integration Tests ───────────────────────────────────────────────────

def test_api_charges_crud(billing_client: TestClient, auth_headers: dict, patient: Patient):
    """Test creating, listing, updating, and cancelling a billing charge."""
    # 1. Create charge
    res = billing_client.post(
        "/api/billing/charges",
        headers=auth_headers,
        json={
            "patient_id": str(patient.id),
            "source_type": "consultation",
            "description": "General OPD Consultation",
            "charge_amount": 600.0,
            "discount_amount": 100.0,
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["net_amount"] == 500.0
    assert data["status"] == "pending"
    charge_id = data["id"]

    # 2. List charges
    list_res = billing_client.get(
        f"/api/billing/charges?patient_id={patient.id}",
        headers=auth_headers,
    )
    assert list_res.status_code == 200
    charges = list_res.json()
    assert any(c["id"] == charge_id for c in charges)

    # 3. Cancel charge
    cancel_res = billing_client.post(
        f"/api/billing/charges/{charge_id}/cancel",
        headers=auth_headers,
    )
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"


def test_api_payment_and_invoice_workflow(
    billing_client: TestClient, auth_headers: dict, patient: Patient
):
    """Test full flow: charge -> invoice -> payment -> receipt -> ledger."""
    # 1. Create charge
    c_res = billing_client.post(
        "/api/billing/charges",
        headers=auth_headers,
        json={
            "patient_id": str(patient.id),
            "source_type": "admission",
            "description": "Room Admission Charge",
            "charge_amount": 1500.0,
        },
    )
    assert c_res.status_code == 201
    charge_id = c_res.json()["id"]

    # 2. Create invoice
    inv_res = billing_client.post(
        "/api/billing/invoices",
        headers=auth_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [charge_id],
            "tax_amount": 75.0,
            "notes": "Admission advance invoice",
        },
    )
    assert inv_res.status_code == 201
    inv_data = inv_res.json()
    assert inv_data["subtotal"] == 1500.0
    assert inv_data["grand_total"] == 1575.0
    assert inv_data["invoice_number"].startswith("INV-")
    invoice_id = inv_data["id"]

    # 3. Print invoice HTML
    html_res = billing_client.get(f"/api/billing/invoices/{invoice_id}/print", headers=auth_headers)
    assert html_res.status_code == 200
    assert "text/html" in html_res.headers["content-type"]
    assert "Room Admission Charge" in html_res.text

    # 4. Record payment
    pay_res = billing_client.post(
        "/api/billing/payments",
        headers=auth_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 1500.0,
            "payment_method": "upi",
            "reference_number": "UPI-TXN-123456",
            "linked_invoice_id": invoice_id,
        },
    )
    assert pay_res.status_code == 201
    pay_data = pay_res.json()
    assert pay_data["amount"] == 1500.0
    assert pay_data["receipt_number"] is not None

    # 5. Patient Ledger
    ledger_res = billing_client.get(
        f"/api/billing/patients/{patient.id}/ledger",
        headers=auth_headers,
    )
    assert ledger_res.status_code == 200
    ledger = ledger_res.json()
    assert ledger["total_charges"] >= 1500.0
    assert ledger["total_paid"] >= 1500.0
    assert len(ledger["entries"]) >= 2


def test_api_billing_cross_hospital_isolation(
    billing_client: TestClient,
    auth_headers: dict,
    auth_headers_b: dict,
    patient: Patient,
):
    """Assert hospital B cannot see or mutate charges created in hospital A."""
    # Create charge in hospital A
    res = billing_client.post(
        "/api/billing/charges",
        headers=auth_headers,
        json={
            "patient_id": str(patient.id),
            "source_type": "other",
            "description": "Confidential Hospital A Test",
            "charge_amount": 250.0,
        },
    )
    assert res.status_code == 201
    charge_id = res.json()["id"]

    # Hospital B lists charges -> must NOT include hospital A charge
    list_b = billing_client.get("/api/billing/charges", headers=auth_headers_b)
    assert list_b.status_code == 200
    ids_b = [c["id"] for c in list_b.json()]
    assert charge_id not in ids_b

    # Hospital B tries to cancel Hospital A charge -> 404
    cancel_b = billing_client.post(
        f"/api/billing/charges/{charge_id}/cancel",
        headers=auth_headers_b,
    )
    assert cancel_b.status_code == 404
