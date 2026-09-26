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

from infrastructure.postgres.session import get_transitional_sync_session
from modules.tenancy.entities.hospital import Hospital
from modules.patients.entities.patient import Patient
from modules.billing.api.billing_api import router as billing_router
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingSourceType,
)
from modules.billing.services.billing_service import (
    allocate_payment_to_charges,
    compute_net,
    ensure_charge,
    patient_ledger_totals,
)
from shared.exceptions import register_exception_handlers
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

def test_api_charges_crud(billing_client: TestClient, admin_headers: dict, patient: Patient):
    """Test creating, listing, updating, and cancelling a billing charge."""
    # 1. Create charge
    res = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
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
        headers=admin_headers,
    )
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1

    # 3. Update charge
    upd_res = billing_client.put(
        f"/api/billing/charges/{charge_id}",
        headers=admin_headers,
        json={"discount_amount": 200.0},
    )
    assert upd_res.status_code == 200
    assert upd_res.json()["net_amount"] == 400.0

    # 4. Cancel charge
    cancel_res = billing_client.post(
        f"/api/billing/charges/{charge_id}/cancel",
        headers=admin_headers,
    )
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"


def test_api_payment_and_invoice_workflow(
    billing_client: TestClient, admin_headers: dict, patient: Patient
):
    """Test full flow: charge -> invoice -> payment -> receipt -> ledger."""
    # 1. Create charge
    c_res = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
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
        headers=admin_headers,
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
    html_res = billing_client.get(f"/api/billing/invoices/{invoice_id}/print", headers=admin_headers)
    assert html_res.status_code == 200
    assert "text/html" in html_res.headers["content-type"]
    assert "Room Admission Charge" in html_res.text

    # 4. Record payment
    pay_res = billing_client.post(
        "/api/billing/payments",
        headers=admin_headers,
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
        headers=admin_headers,
    )
    assert ledger_res.status_code == 200
    ledger = ledger_res.json()
    assert ledger["total_charges"] >= 1500.0
    assert ledger["total_paid"] >= 1500.0
    assert len(ledger["entries"]) >= 2


def test_api_billing_cross_hospital_isolation(
    billing_client: TestClient,
    admin_headers: dict,
    hospital_b: Hospital,
    patient: Patient,
):
    """Assert hospital B cannot see or mutate charges created in hospital A."""
    from shared.auth.jwt import create_access_token
    token_b = create_access_token({
        "sub": "admin@fortisreg.com",
        "name": "Admin B",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital_b.id),
    })
    admin_headers_b = {"Authorization": f"Bearer {token_b}"}

    # Create charge in hospital A
    res = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
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

def test_api_charges_crud(billing_client: TestClient, admin_headers: dict, patient: Patient):
    """Test creating, listing, updating, and cancelling a billing charge with discount reason validation."""
    # 0. Test missing discount reason rejection
    bad_res = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "source_type": "consultation",
            "description": "General OPD Consultation",
            "charge_amount": 600.0,
            "discount_amount": 100.0,
        },
    )
    assert bad_res.status_code == 422
    assert "Discount reason is mandatory" in bad_res.text

    # 1. Create charge with valid discount reason
    res = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "source_type": "consultation",
            "description": "General OPD Consultation",
            "charge_amount": 600.0,
            "discount_amount": 100.0,
            "discount_reason": "Senior Citizen Concession",
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["net_amount"] == 500.0
    assert data["discount_reason"] == "Senior Citizen Concession"
    assert data["status"] == "pending"
    charge_id = data["id"]

    # 2. List charges
    list_res = billing_client.get(
        f"/api/billing/charges?patient_id={patient.id}",
        headers=admin_headers,
    )
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1

    # 3. Update charge with discount reason
    upd_res = billing_client.put(
        f"/api/billing/charges/{charge_id}",
        headers=admin_headers,
        json={"discount_amount": 200.0, "discount_reason": "Special Management Approval"},
    )
    assert upd_res.status_code == 200
    assert upd_res.json()["net_amount"] == 400.0
    assert upd_res.json()["discount_reason"] == "Special Management Approval"

    # 4. Cancel charge
    cancel_res = billing_client.post(
        f"/api/billing/charges/{charge_id}/cancel",
        headers=admin_headers,
    )
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"



def test_api_payment_and_invoice_workflow(
    billing_client: TestClient, admin_headers: dict, patient: Patient
):
    """Test full flow: charge -> invoice -> payment -> receipt -> ledger."""
    # 1. Create charge
    c_res = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
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
        headers=admin_headers,
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
    html_res = billing_client.get(f"/api/billing/invoices/{invoice_id}/print", headers=admin_headers)
    assert html_res.status_code == 200
    assert "text/html" in html_res.headers["content-type"]
    assert "Room Admission Charge" in html_res.text

    # 4. Record payment
    pay_res = billing_client.post(
        "/api/billing/payments",
        headers=admin_headers,
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
        headers=admin_headers,
    )
    assert ledger_res.status_code == 200
    ledger = ledger_res.json()
    assert ledger["total_charges"] >= 1500.0
    assert ledger["total_paid"] >= 1500.0
    assert len(ledger["entries"]) >= 2


def test_api_billing_cross_hospital_isolation(
    billing_client: TestClient,
    admin_headers: dict,
    hospital_b: Hospital,
    patient: Patient,
):
    """Assert hospital B cannot see or mutate charges created in hospital A."""
    from shared.auth.jwt import create_access_token
    token_b = create_access_token({
        "sub": "admin@fortisreg.com",
        "name": "Admin B",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital_b.id),
    })
    admin_headers_b = {"Authorization": f"Bearer {token_b}"}

    # Create charge in hospital A
    res = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
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
    list_b = billing_client.get("/api/billing/charges", headers=admin_headers_b)
    assert list_b.status_code == 200
    ids_b = [c["id"] for c in list_b.json()]
    assert charge_id not in ids_b

    # Hospital B tries to cancel Hospital A charge -> 404
    cancel_b = billing_client.post(
        f"/api/billing/charges/{charge_id}/cancel",
        headers=admin_headers_b,
    )
    assert cancel_b.status_code == 404


def test_selective_payment_and_allocation(
    billing_client: TestClient,
    admin_headers: dict,
    patient: Patient,
):
    """Verify selective payment allocates only to cashier-selected charges."""
    # 1. Create 3 distinct charges (Lab, OT, Bed)
    r1 = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "source_type": "laboratory",
            "description": "CBC Hemogram",
            "charge_amount": 500.0,
        },
    )
    assert r1.status_code == 201
    c1_id = r1.json()["id"]

    r2 = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "source_type": "ot",
            "description": "Appendectomy Surgery",
            "charge_amount": 25000.0,
        },
    )
    assert r2.status_code == 201
    c2_id = r2.json()["id"]

    r3 = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "source_type": "bed",
            "description": "Deluxe Room Bed Charges",
            "charge_amount": 3000.0,
        },
    )
    assert r3.status_code == 201
    c3_id = r3.json()["id"]

    # 2. Cashier selectively pays only Lab (₹500) and partial OT (₹10,000) = ₹10,500
    pay_res = billing_client.post(
        "/api/billing/payments",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 10500.0,
            "payment_method": "card",
            "allocations": [
                {"charge_id": c1_id, "amount": 500.0},
                {"charge_id": c2_id, "amount": 10000.0},
            ],
        },
    )
    assert pay_res.status_code == 201
    pay_data = pay_res.json()
    assert pay_data["amount"] == 10500.0
    assert len(pay_data["allocations"]) == 2

    # 3. Verify charge states:
    # c1 (Lab) -> paid
    # c2 (OT) -> partially_paid (10000 paid / 25000)
    # c3 (Bed) -> pending (0 paid / 3000)
    charges_res = billing_client.get(f"/api/billing/charges?patient_id={patient.id}", headers=admin_headers)
    assert charges_res.status_code == 200
    chg_map = {c["id"]: c for c in charges_res.json()}

    assert chg_map[c1_id]["status"] == "paid"
    assert chg_map[c1_id]["amount_paid"] == 500.0

    assert chg_map[c2_id]["status"] == "partially_paid"
    assert chg_map[c2_id]["amount_paid"] == 10000.0

    assert chg_map[c3_id]["status"] == "pending"
    assert chg_map[c3_id]["amount_paid"] == 0.0


def test_deposit_lifecycle_and_drawdown(
    billing_client: TestClient,
    admin_headers: dict,
    patient: Patient,
):
    """Verify advance deposit receipt, drawdown against charges, and running balance."""
    # 1. Patient deposits ₹20,000
    dep_res = billing_client.post(
        "/api/billing/deposits",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 20000.0,
            "deposit_type": "ipd_admission",
            "payment_method": "bank_transfer",
            "notes": "Admission advance deposit",
        },
    )
    assert dep_res.status_code == 201
    dep_data = dep_res.json()
    dep_id = dep_data["id"]
    assert dep_data["original_amount"] == 20000.0
    assert dep_data["available_amount"] == 20000.0
    assert dep_data["status"] == "available"

    # 2. Add an investigation charge ₹4,500
    c_res = billing_client.post(
        "/api/billing/charges",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "source_type": "radiology",
            "description": "MRI Brain with Contrast",
            "charge_amount": 4500.0,
        },
    )
    assert c_res.status_code == 201
    chg_id = c_res.json()["id"]

    # 3. Draw down deposit to pay for MRI
    alloc_res = billing_client.post(
        f"/api/billing/deposits/{dep_id}/allocate",
        headers=admin_headers,
        json={
            "allocations": [{"charge_id": chg_id, "amount": 4500.0}],
        },
    )
    assert alloc_res.status_code == 200
    alloc_data = alloc_res.json()
    assert alloc_data["available_amount"] == 15500.0
    assert alloc_data["status"] == "partially_allocated"

    # Verify charge is now marked paid
    chg_check = billing_client.get(f"/api/billing/charges?patient_id={patient.id}", headers=admin_headers)
    mri_chg = next(c for c in chg_check.json() if c["id"] == chg_id)
    assert mri_chg["status"] == "paid"
    assert mri_chg["amount_paid"] == 4500.0

    # 4. Refund remaining deposit ₹5,000
    ref_res = billing_client.post(
        "/api/billing/refunds",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "deposit_id": dep_id,
            "amount": 5000.0,
            "reason": "Patient requested partial advance refund prior to discharge",
        },
    )
    assert ref_res.status_code == 201
    ref_data = ref_res.json()
    assert ref_data["amount"] == 5000.0
    assert ref_data["status"] == "processed"

    # 5. Verify deposit available balance is now ₹10,500
    dep_check = billing_client.get(f"/api/billing/deposits?patient_id={patient.id}", headers=admin_headers)
    dep_final = next(d for d in dep_check.json() if d["id"] == dep_id)
    assert dep_final["available_amount"] == 10500.0


def test_emergency_clearance_override(
    db_session: Session,
    hospital: Hospital,
    patient: Patient,
):
    """Verify emergency STAT orders pass financial clearance check."""
    from modules.billing.services.service_financial_clearance import (
        assert_service_financially_cleared,
        check_service_financial_clearance,
    )
    from fastapi import HTTPException

    order_id = uuid4()
    # Create an unpaid charge
    ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=order_id,
        description="STAT Cardiac Troponin T",
        charge_amount=1200.0,
    )
    db_session.commit()

    # Normal non-emergency check raises HTTPException 402
    with pytest.raises(HTTPException) as exc:
        assert_service_financially_cleared(
            db_session,
            hospital.id,
            BillingSourceType.laboratory,
            order_id,
            is_emergency_override=False,
        )
    assert exc.value.status_code == 402

    # Emergency override without mandatory reason or authorized actor is rejected
    with pytest.raises(HTTPException) as exc_no_reason:
        assert_service_financially_cleared(
            db_session,
            hospital.id,
            BillingSourceType.laboratory,
            order_id,
            is_emergency_override=True,
        )
    assert exc_no_reason.value.status_code == 400

    # Emergency override clears successfully when authorized actor and reason provided
    cleared_state = assert_service_financially_cleared(
        db_session,
        hospital.id,
        BillingSourceType.laboratory,
        order_id,
        is_emergency_override=True,
        actor={"role": "doctor", "name": "Dr. House"},
        emergency_reason="Acute STEMI requiring emergency cardiac enzymes",
    )
    assert cleared_state.is_cleared is True
    assert "override" in cleared_state.reason.lower()


def test_receipt_number_self_heals_behind_legacy_rows(
    db_session: Session, hospital: Hospital, patient: Patient
):
    """Regression: atomic receipt counter behind legacy MAX+1 rows must not 500.

    Reproduces POST /api/billing/payments → UniqueViolation on
    uq_billing_receipt_number for RCPT-<year>-00001: a legacy receipt row
    exists while the sequence counter starts at 1. The generator must resync
    past existing rows instead of re-issuing the same number.
    """
    from datetime import date
    from modules.billing.entities.billing_entities import (
        BillingPayment as _Pay,
        BillingReceipt as _Rcpt,
    )
    from modules.billing.services.invoice_service import issue_receipt

    y = date.today().year
    legacy = _Rcpt(
        hospital_id=hospital.id,
        patient_id=patient.id,
        payment_id=uuid4(),
        receipt_number=f"RCPT-{y}-00001",
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        amount=100.0,
        status="issued",
        collected_by_name="Legacy",
    )
    db_session.add(legacy)
    db_session.commit()
    # No sequence_counters row exists yet — counter would start at 1.

    r1 = issue_receipt(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=200.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        collected_by_name="Test",
    )
    db_session.commit()
    assert r1.receipt_number == f"RCPT-{y}-00002"

    r2 = issue_receipt(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=300.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        collected_by_name="Test",
    )
    db_session.commit()
    assert r2.receipt_number == f"RCPT-{y}-00003"


def test_two_payments_get_distinct_receipts(
    billing_client: TestClient, admin_headers: dict, patient: Patient
):
    """Two consecutive POST /payments must yield distinct receipt numbers."""
    numbers = set()
    for _ in range(2):
        res = billing_client.post(
            "/api/billing/payments",
            headers=admin_headers,
            json={
                "patient_id": str(patient.id),
                "amount": 5000.0,
                "payment_method": "cash",
            },
        )
        assert res.status_code == 201, res.text
        numbers.add(res.json()["receipt_number"])
    assert len(numbers) == 2


# ── Settlement-discount regression tests ──────────────────────────────────

def _make_charge(db_session, hospital, patient, amount: float, desc: str = "Consult"):
    ch = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid4(),
        description=desc,
        charge_amount=amount,
    )
    db_session.commit()
    return ch


def test_settlement_item_discount_updates_charge_net(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """Per-item discount entered at Record Payment must update the charge net.

    Regression: allocate_specific_charges assigned compute_net()'s (disc, net)
    tuple straight into the Float column → flush blew up → 500.
    """
    ch = _make_charge(db_session, hospital, patient, 1000.0)
    res = billing_client.post(
        "/api/billing/payments",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 800.0,
            "payment_method": "cash",
            "allocations": [
                {
                    "charge_id": str(ch.id),
                    "amount": 800.0,
                    "discount_amount": 200.0,
                    "discount_reason": "Staff concession",
                }
            ],
        },
    )
    assert res.status_code == 201, res.text
    db_session.refresh(ch)
    assert ch.discount_amount == 200.0
    assert ch.discount_reason == "Staff concession"
    assert ch.net_amount == 800.0
    assert ch.amount_paid == 800.0
    assert ch.status == "paid"


def test_settlement_global_discount_distributes_across_charges(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """Top-level discount on POST /payments must not be silently dropped.

    A ₹200 global concession on two ₹1000 charges distributes ₹100 each.
    """
    c1 = _make_charge(db_session, hospital, patient, 1000.0, "Consult A")
    c2 = _make_charge(db_session, hospital, patient, 1000.0, "Consult B")
    res = billing_client.post(
        "/api/billing/payments",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 1800.0,
            "payment_method": "cash",
            "discount_amount": 200.0,
            "discount_reason": "Management concession",
        },
    )
    assert res.status_code == 201, res.text
    db_session.refresh(c1)
    db_session.refresh(c2)
    assert c1.net_amount == 900.0
    assert c2.net_amount == 900.0
    assert c1.discount_reason == "Management concession"
    assert c2.discount_reason == "Management concession"
    assert c1.status == "paid"
    assert c2.status == "paid"


def test_settlement_discount_only_line_without_tender(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """A fully-waived line (₹0 tender + discount) must be accepted.

    Regression: ChargeAllocationItem.amount gt=0 rejected discount-only lines
    with 422, making 100% concessions at settlement impossible.
    """
    c1 = _make_charge(db_session, hospital, patient, 500.0, "Consult A")
    c2 = _make_charge(db_session, hospital, patient, 1000.0, "Consult B")
    res = billing_client.post(
        "/api/billing/payments",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 500.0,
            "payment_method": "cash",
            "allocations": [
                {"charge_id": str(c1.id), "amount": 500.0},
                {
                    "charge_id": str(c2.id),
                    "amount": 0,
                    "discount_amount": 1000.0,
                    "discount_reason": "Charity waiver",
                },
            ],
        },
    )
    assert res.status_code == 201, res.text
    db_session.refresh(c2)
    assert c2.net_amount == 0.0
    assert c2.status == "paid"
