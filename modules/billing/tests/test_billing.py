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
        source_type=BillingSourceType.other,
        source_id=None,
        description="Dr Consultation",
        charge_amount=500.0,
    )
    c2 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        source_id=None,
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

    # Emergency override clears successfully when an actor holding emergency:approve gives a reason
    from modules.doctors.entities.doctor import HospitalUser

    clinician = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=uuid4(),
        name="Emergency Consultant",
        phone="555-0199",
        email="er.consultant@apollocity.com",
        password_hash="x",
    )
    db_session.add(clinician)
    db_session.commit()
    cleared_state = assert_service_financially_cleared(
        db_session,
        hospital.id,
        BillingSourceType.laboratory,
        order_id,
        is_emergency_override=True,
        actor={
            "user_id": str(clinician.id),
            "role": "hospital_staff",
            "hospital_uuid": str(hospital.id),
            "name": clinician.name,
            "permissions": {"emergency": {"can_view": True, "can_approve": True}},
        },
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
        source_type=BillingSourceType.other,
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


# ── Billing Redesign & Regression Tests (Audit Phase) ──────────────────────────

def test_selective_invoice_line_integrity(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """Test A — Selective invoice line integrity:
    Create charges A (₹100), B (₹200), C (₹300), D (₹400).
    Invoice only A and C.
    Assert line_count == 2, invoice contains A and C, excludes B and D, grand_total == ₹400.
    """
    cA = _make_charge(db_session, hospital, patient, 100.0, "Consultation Charge A")
    cB = _make_charge(db_session, hospital, patient, 200.0, "CBC Charge B")
    cC = _make_charge(db_session, hospital, patient, 300.0, "Medicine Charge C")
    cD = _make_charge(db_session, hospital, patient, 400.0, "X-Ray Charge D")

    res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cA.id), str(cC.id)],
            "notes": "Selective Invoice Test",
        },
    )
    assert res.status_code == 201, res.text
    inv_data = res.json()
    assert inv_data["subtotal"] == 400.0
    assert inv_data["grand_total"] == 400.0
    assert len(inv_data["lines"]) == 2

    line_descriptions = [line["description"] for line in inv_data["lines"]]
    assert "Consultation Charge A" in line_descriptions
    assert "Medicine Charge C" in line_descriptions
    assert "CBC Charge B" not in line_descriptions
    assert "X-Ray Charge D" not in line_descriptions

    # Print preview must also contain only A and C
    print_res = billing_client.get(f"/api/billing/invoices/{inv_data['id']}/print", headers=admin_headers)
    assert print_res.status_code == 200
    html = print_res.text
    assert "Consultation Charge A" in html
    assert "Medicine Charge C" in html
    assert "CBC Charge B" not in html
    assert "X-Ray Charge D" not in html


def test_duplicate_invoice_guard(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """Test B — Duplicate invoice guard:
    Attempting to create another active invoice containing an already-invoiced charge fails.
    """
    cA = _make_charge(db_session, hospital, patient, 150.0, "Consultation Single")

    # First invoice succeeds
    res1 = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cA.id)],
        },
    )
    assert res1.status_code == 201, res1.text

    # Second invoice with same charge MUST fail
    res2 = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cA.id)],
        },
    )
    assert res2.status_code == 400, "Duplicate invoicing of active charge must be rejected"
    assert "already on an active invoice" in res2.text or "already invoiced" in res2.text


def test_historical_immutability(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """Test C — Historical immutability:
    1. Generate invoice for charge A (₹500).
    2. Add new unrelated patient charge B (₹15,000).
    3. Re-fetch invoice #1 and verify lines and grand_total remain unchanged at ₹500.
    """
    cA = _make_charge(db_session, hospital, patient, 500.0, "Ultrasound Scan")
    res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cA.id)],
        },
    )
    assert res.status_code == 201
    inv_id = res.json()["id"]
    inv_num = res.json()["invoice_number"]

    # Now post a huge new clinical charge to the patient
    _make_charge(db_session, hospital, patient, 15000.0, "Major Surgery OT Charge")

    # Re-fetch invoice #1 via API
    get_res = billing_client.get(f"/api/billing/invoices/{inv_id}", headers=admin_headers)
    assert get_res.status_code == 200
    inv_fetched = get_res.json()
    assert inv_fetched["subtotal"] == 500.0
    assert inv_fetched["grand_total"] == 500.0
    assert len(inv_fetched["lines"]) == 1
    assert inv_fetched["lines"][0]["description"] == "Ultrasound Scan"


def test_printed_invoice_excludes_global_outstanding(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """Test D — Critical Fix: Printed invoice excludes patient global ledger outstanding.
    Selected charge = ₹1,250.
    Unbilled charge = ₹15,000.
    Patient ledger outstanding = ₹16,250.
    Invoice document must display Grand Total ₹1,250 and NOT display Ledger Outstanding ₹16,250.
    """
    c_sel = _make_charge(db_session, hospital, patient, 1250.0, "Physiotherapy Session")
    c_unsel = _make_charge(db_session, hospital, patient, 15000.0, "ICU Bed Charges - 3 Days")

    res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(c_sel.id)],
        },
    )
    assert res.status_code == 201
    inv_id = res.json()["id"]

    print_res = billing_client.get(f"/api/billing/invoices/{inv_id}/print", headers=admin_headers)
    assert print_res.status_code == 200
    html = print_res.text

    assert "Grand Total" in html
    assert "₹1,250.00" in html
    assert "Rupees One Thousand Two Hundred Fifty Only" in html
    # Crucial check: Ledger Outstanding must NOT be present
    assert "Ledger Outstanding" not in html
    assert "16,250" not in html


def test_deposit_receipt_print_endpoint(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """Test E — Advance Deposit Receipt printing endpoint.
    Creates a deposit and retrieves the printable Advance Receipt HTML.
    Verifies deposit number, patient, amount, amount in words, and hospital header.
    """
    dep_res = billing_client.post(
        "/api/billing/deposits",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 5000.0,
            "deposit_type": "admission",
            "payment_method": "card",
            "reference_number": "POS-CARD-9988",
            "notes": "Emergency OT advance",
        },
    )
    assert dep_res.status_code == 201, dep_res.text
    dep_data = dep_res.json()
    dep_id = dep_data["id"]

    # Print endpoint GET /api/billing/deposits/{id}/print
    print_res = billing_client.get(f"/api/billing/deposits/{dep_id}/print", headers=admin_headers)
    assert print_res.status_code == 200
    html = print_res.text
    assert "ADVANCE / DEPOSIT RECEIPT" in html
    assert dep_data["deposit_number"] in html
    assert "₹5,000.00" in html
    assert "Rupees Five Thousand Only" in html
    assert "Emergency OT advance" in html


def test_multiline_invoice_rendering_and_pagination(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """Test F — Multi-line invoice pagination / A4 table rendering (25 lines).
    Verifies sequential numbering, table headers, A4 css, and totals.
    """
    charge_ids = []
    for i in range(25):
        c = _make_charge(db_session, hospital, patient, 100.0 + i, f"Lab Item Component #{i+1:02d}")
        charge_ids.append(str(c.id))

    res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": charge_ids,
        },
    )
    assert res.status_code == 201
    inv_id = res.json()["id"]

    print_res = billing_client.get(f"/api/billing/invoices/{inv_id}/print", headers=admin_headers)
    assert print_res.status_code == 200
    html = print_res.text

    assert "@page" in html
    assert "size: A4 portrait" in html
    assert "table.items-table" in html
    assert "Lab Item Component #01" in html
    assert "Lab Item Component #25" in html


@pytest.mark.parametrize("line_count", [5, 20, 50, 100])
def test_invoice_document_long_table_line_counts(
    line_count: int,
    billing_client: TestClient,
    admin_headers: dict,
    db_session: Session,
    hospital: Hospital,
    patient: Patient,
):
    """Print renderer retains every snapshot row and repeatable headers at A4 lengths."""
    charges = [
        _make_charge(db_session, hospital, patient, 100.0 + index, f"Pagination service line {index + 1:03d}")
        for index in range(line_count)
    ]
    created = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={"patient_id": str(patient.id), "charge_ids": [str(charge.id) for charge in charges]},
    )
    assert created.status_code == 201, created.text
    rendered = billing_client.get(
        f"/api/billing/invoices/{created.json()['id']}/print", headers=admin_headers
    )
    assert rendered.status_code == 200
    html = rendered.text
    assert "display: table-header-group" in html
    assert "break-inside: avoid" in html
    assert f"Pagination service line 001" in html
    assert f"Pagination service line {line_count:03d}" in html
    assert "Net Bill / Grand Total" in html


# ── True Itemized Invoice vs Payment Receipt Flow Tests ─────────────────────────

def test_1_itemized_charge_selection(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """TEST 1: Selective charge invoicing.
    Charges: Consultation ₹500, CBC ₹400, X-Ray ₹800, Medicine ₹350.
    Select: Consultation + Medicine.
    Expected: Invoice shows Consultation and Medicine with Grand Total ₹850.
    CBC and X-Ray must NOT appear.
    """
    cA = _make_charge(db_session, hospital, patient, 500.0, "Consultation")
    cB = _make_charge(db_session, hospital, patient, 400.0, "CBC")
    cC = _make_charge(db_session, hospital, patient, 800.0, "X-Ray")
    cD = _make_charge(db_session, hospital, patient, 350.0, "Medicine")

    res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cA.id), str(cD.id)],
        },
    )
    assert res.status_code == 201, res.text
    inv_data = res.json()
    assert inv_data["subtotal"] == 850.0
    assert inv_data["grand_total"] == 850.0
    assert len(inv_data["lines"]) == 2

    descriptions = [line["description"] for line in inv_data["lines"]]
    assert "Consultation" in descriptions
    assert "Medicine" in descriptions
    assert "CBC" not in descriptions
    assert "X-Ray" not in descriptions

    print_res = billing_client.get(f"/api/billing/invoices/{inv_data['id']}/print", headers=admin_headers)
    assert print_res.status_code == 200
    html = print_res.text
    assert "TAX INVOICE" in html
    assert "Consultation" in html
    assert "Medicine" in html
    assert "CBC" not in html
    assert "X-Ray" not in html
    assert "₹850.00" in html


def test_2_invoice_vs_receipt_separation(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """TEST 2: Invoice vs Receipt separation.
    Generate Invoice for ₹850.
    Pay ₹850 Cash.
    Assert receipt has amount ₹850, mode Cash, and no fake 'Hospital Payment Settlement' table row.
    Assert invoice exists independently.
    """
    cA = _make_charge(db_session, hospital, patient, 500.0, "Consultation")
    cB = _make_charge(db_session, hospital, patient, 350.0, "Medicine")

    inv_res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cA.id), str(cB.id)],
        },
    )
    assert inv_res.status_code == 201
    inv = inv_res.json()
    inv_id = inv["id"]

    pay_res = billing_client.post(
        "/api/billing/payments",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 850.0,
            "payment_method": "cash",
            "linked_invoice_id": inv_id,
        },
    )
    assert pay_res.status_code == 201, pay_res.text
    pay = pay_res.json()
    rcpt_id = pay["receipt_id"]

    rcpt_print = billing_client.get(f"/api/billing/receipts/{rcpt_id}/print", headers=admin_headers)
    assert rcpt_print.status_code == 200
    rcpt_html_text = rcpt_print.text
    assert "PAYMENT RECEIPT" in rcpt_html_text
    assert "₹850.00" in rcpt_html_text
    assert "Cash" in rcpt_html_text
    assert "Rupees Eight Hundred Fifty Only" in rcpt_html_text
    assert "Payment Acknowledgment Summary" in rcpt_html_text
    assert "Hospital Payment Settlement" not in rcpt_html_text
    assert inv["invoice_number"] in rcpt_html_text


def test_3_payment_does_not_replace_or_mutate_invoice(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """TEST 3: Payment does not mutate invoice lines or totals.
    After payment:
    GET /invoices/{id} must still return the exact itemized services.
    GET /receipts/{id} must show payment acknowledgment.
    """
    cA = _make_charge(db_session, hospital, patient, 600.0, "Specialist Consultation")
    inv_res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cA.id)],
        },
    )
    inv_id = inv_res.json()["id"]

    # Record payment settling the charge
    pay_res = billing_client.post(
        "/api/billing/payments",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 600.0,
            "payment_method": "cash",
            "linked_invoice_id": inv_id,
        },
    )
    rcpt_id = pay_res.json()["receipt_id"]

    # Invoice query
    inv_check = billing_client.get(f"/api/billing/invoices/{inv_id}", headers=admin_headers)
    assert inv_check.status_code == 200
    inv_body = inv_check.json()
    assert inv_body["status"] == "paid"
    assert inv_body["grand_total"] == 600.0
    assert len(inv_body["lines"]) == 1
    assert inv_body["lines"][0]["description"] == "Specialist Consultation"

    # Receipt query
    rcpt_check = billing_client.get(f"/api/billing/receipts/{rcpt_id}", headers=admin_headers)
    assert rcpt_check.status_code == 200
    rcpt_body = rcpt_check.json()
    assert rcpt_body["amount"] == 600.0
    assert rcpt_body["payment_method"] == "cash"


def test_4_invoice_reprint_stability(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """TEST 4: Reprint stability.
    Reprinting the invoice after payment has completed shows unchanged line items.
    """
    cA = _make_charge(db_session, hospital, patient, 750.0, "Cardiology Echo Test")
    inv_res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cA.id)],
        },
    )
    inv_id = inv_res.json()["id"]

    billing_client.post(
        "/api/billing/payments",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 750.0,
            "payment_method": "upi",
            "reference_number": "UPI-REPRINT-9988",
            "linked_invoice_id": inv_id,
        },
    )

    reprint_res = billing_client.get(f"/api/billing/invoices/{inv_id}/print", headers=admin_headers)
    assert reprint_res.status_code == 200
    html = reprint_res.text
    assert "Cardiology Echo Test" in html
    assert "₹750.00" in html
    assert "PAID" in html


def test_5_remaining_unbilled_charges(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    """TEST 5: Charges A, B, C, D exist.
    Invoice #1 selects A + C.
    B and D remain unbilled and can be invoiced in Invoice #2.
    """
    cA = _make_charge(db_session, hospital, patient, 100.0, "Item A")
    cB = _make_charge(db_session, hospital, patient, 200.0, "Item B")
    cC = _make_charge(db_session, hospital, patient, 300.0, "Item C")
    cD = _make_charge(db_session, hospital, patient, 400.0, "Item D")

    # Invoice 1
    inv1_res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cA.id), str(cC.id)],
        },
    )
    assert inv1_res.status_code == 201

    # Ledger check: pending charges for invoice should now contain B and D
    ledger_res = billing_client.get(f"/api/billing/patients/{patient.id}/ledger", headers=admin_headers)
    assert ledger_res.status_code == 200

    # Invoice 2 for B and D succeeds
    inv2_res = billing_client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "charge_ids": [str(cB.id), str(cD.id)],
        },
    )
    assert inv2_res.status_code == 201
    inv2_data = inv2_res.json()
    assert inv2_data["grand_total"] == 600.0
    descriptions = [line["description"] for line in inv2_data["lines"]]
    assert "Item B" in descriptions
    assert "Item D" in descriptions
    assert "Item A" not in descriptions
    assert "Item C" not in descriptions


def test_itemized_paid_bill_and_receipt_allocations(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    charges = [
        _make_charge(db_session, hospital, patient, amount, name)
        for name, amount in [
            ("Consultation Fee - Dr. Dr. Anil Nair", 1000), ("CBC", 600), ("X-Ray", 1200),
            ("Medicines", 700), ("Bed Charge", 1500),
        ]
    ]
    invoice = billing_client.post("/api/billing/invoices", headers=admin_headers, json={
        "patient_id": str(patient.id), "charge_ids": [str(c.id) for c in charges],
    }).json()
    payment = billing_client.post("/api/billing/payments", headers=admin_headers, json={
        "patient_id": str(patient.id), "amount": 5000, "payment_method": "cash",
        "linked_invoice_id": invoice["id"],
        "allocations": [{"charge_id": str(c.id), "amount": amount} for c, amount in zip(charges, [1000, 600, 1200, 700, 1500])],
    })
    assert payment.status_code == 201, payment.text
    bill = billing_client.get(f"/api/billing/invoices/{invoice['id']}/print", headers=admin_headers).text
    for name in ("Consultation", "CBC", "X-Ray", "Medicines", "Bed Charge"):
        assert name in bill
    assert "Invoice Total" in bill and "₹5,000.00" in bill
    assert "Amount Paid" in bill and "Balance Due" in bill and "₹0.00" in bill
    assert "Cash" in bill and payment.json()["receipt_number"] in bill
    assert "GSTIN:" not in bill and "PAN:" not in bill and "State Code:" not in bill
    assert "Dr. Anil Nair" in bill and "Dr. Dr." not in bill
    receipt = billing_client.get(f"/api/billing/receipts/{payment.json()['receipt_id']}/print", headers=admin_headers).text
    assert "PAYMENT RECEIPT" in receipt and "Paid Against" in receipt
    assert "Dr. Anil Nair" in receipt and "Dr. Dr." not in receipt
    for name in ("Consultation", "CBC", "X-Ray", "Medicines", "Bed Charge"):
        assert name in receipt
    assert "Official acknowledgement of payment." in receipt
    assert "refer to the corresponding tax invoice" not in receipt
    bill_download = billing_client.get(f"/api/billing/invoices/{invoice['id']}/pdf?download=true", headers=admin_headers)
    receipt_download = billing_client.get(f"/api/billing/receipts/{payment.json()['receipt_id']}/pdf?download=true", headers=admin_headers)
    assert bill_download.status_code == receipt_download.status_code == 200
    assert "ITEMIZED BILL" in bill_download.text and "PAYMENT RECEIPT" not in bill_download.text
    assert "PAYMENT RECEIPT" in receipt_download.text and "Paid Against" in receipt_download.text


def test_reference_5500_receipt_keeps_payment_allocations(
    billing_client: TestClient,
    admin_headers: dict,
    db_session: Session,
    hospital: Hospital,
    patient: Patient,
):
    consultation = _make_charge(db_session, hospital, patient, 2000, "Consultation Fee - Dr. Dr. Anil Nair")
    radiology = _make_charge(db_session, hospital, patient, 3500, "Radiology RAD0075 — CT Brain")
    invoice_response = billing_client.post("/api/billing/invoices", headers=admin_headers, json={
        "patient_id": str(patient.id), "charge_ids": [str(consultation.id), str(radiology.id)],
    })
    assert invoice_response.status_code == 201, invoice_response.text
    invoice = invoice_response.json()
    payment_response = billing_client.post("/api/billing/payments", headers=admin_headers, json={
        "patient_id": str(patient.id), "amount": 5500, "payment_method": "cash",
        "linked_invoice_id": invoice["id"],
        "allocations": [
            {"charge_id": str(consultation.id), "amount": 2000},
            {"charge_id": str(radiology.id), "amount": 3500},
        ],
    })
    assert payment_response.status_code == 201, payment_response.text
    receipt = billing_client.get(
        f"/api/billing/receipts/{payment_response.json()['receipt_id']}/print", headers=admin_headers
    ).text
    assert "₹5,500.00" in receipt
    assert "Consultation Fee - Dr. Anil Nair" in receipt
    assert "Radiology RAD0075 — CT Brain" in receipt
    assert "₹2,000.00" in receipt and "₹3,500.00" in receipt
    assert "Dr. Dr." not in receipt
    bill = billing_client.get(f"/api/billing/invoices/{invoice['id']}/print", headers=admin_headers).text
    assert "Consultation Fee - Dr. Anil Nair" in bill
    assert "Radiology RAD0075 — CT Brain" in bill
    assert "₹5,500.00" in bill


def test_partial_payment_is_reflected_on_bill_and_receipt(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    first = _make_charge(db_session, hospital, patient, 1000, "Charge A")
    second = _make_charge(db_session, hospital, patient, 1000, "Charge B")
    invoice = billing_client.post("/api/billing/invoices", headers=admin_headers, json={
        "patient_id": str(patient.id), "charge_ids": [str(first.id), str(second.id)],
    }).json()
    payment = billing_client.post("/api/billing/payments", headers=admin_headers, json={
        "patient_id": str(patient.id), "amount": 500, "payment_method": "cash",
        "linked_invoice_id": invoice["id"],
        "allocations": [{"charge_id": str(first.id), "amount": 500}],
    })
    assert payment.status_code == 201, payment.text
    bill = billing_client.get(f"/api/billing/invoices/{invoice['id']}/print", headers=admin_headers).text
    assert "Invoice Total" in bill and "₹2,000.00" in bill
    assert "Amount Paid" in bill and "₹500.00" in bill
    assert "Balance Due" in bill and "₹1,500.00" in bill
    receipt = billing_client.get(f"/api/billing/receipts/{payment.json()['receipt_id']}/print", headers=admin_headers).text
    assert "Charge A" in receipt and "₹500.00" in receipt
    assert "Charge B" not in receipt


def test_multiple_payments_aggregate_on_original_invoice(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    charges = [_make_charge(db_session, hospital, patient, amount, name) for name, amount in [("Item A", 3000), ("Item B", 2000)]]
    invoice = billing_client.post("/api/billing/invoices", headers=admin_headers, json={
        "patient_id": str(patient.id), "charge_ids": [str(c.id) for c in charges],
    }).json()
    receipt_ids = []
    for amount, charge, allocated in [(2000, charges[0], 2000), (3000, charges[0], 1000)]:
        response = billing_client.post("/api/billing/payments", headers=admin_headers, json={
            "patient_id": str(patient.id), "amount": amount, "payment_method": "cash",
            "linked_invoice_id": invoice["id"],
            "allocations": [{"charge_id": str(charge.id), "amount": allocated}],
        })
        assert response.status_code == 201, response.text
        receipt_ids.append(response.json()["receipt_id"])
    bill = billing_client.get(f"/api/billing/invoices/{invoice['id']}/print", headers=admin_headers).text
    assert "Invoice Total" in bill and "₹5,000.00" in bill
    assert "Amount Paid" in bill and "₹5,000.00" in bill
    assert "Balance Due" in bill and "₹0.00" in bill
    assert all(billing_client.get(f"/api/billing/receipts/{rid}", headers=admin_headers).status_code == 200 for rid in receipt_ids)


def test_direct_selected_payment_creates_exact_itemized_invoice(
    billing_client: TestClient, admin_headers: dict,
    db_session: Session, hospital: Hospital, patient: Patient,
):
    selected = _make_charge(db_session, hospital, patient, 300, "Selected Service")
    unselected = _make_charge(db_session, hospital, patient, 700, "Unselected Service")
    response = billing_client.post("/api/billing/payments", headers=admin_headers, json={
        "patient_id": str(patient.id), "amount": 300, "payment_method": "cash",
        "allocations": [{"charge_id": str(selected.id), "amount": 300}],
    })
    assert response.status_code == 201, response.text
    receipt = billing_client.get(f"/api/billing/receipts/{response.json()['receipt_id']}", headers=admin_headers).json()
    assert receipt["linked_invoice_id"]
    invoice = billing_client.get(f"/api/billing/invoices/{receipt['linked_invoice_id']}", headers=admin_headers).json()
    assert invoice["grand_total"] == 300
    assert [line["description"] for line in invoice["lines"]] == ["Selected Service"]
    assert unselected.id not in [line["charge_id"] for line in invoice["lines"]]
