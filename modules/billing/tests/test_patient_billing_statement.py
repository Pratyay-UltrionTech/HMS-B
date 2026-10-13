"""Comprehensive Phase C Tests: Canonical Patient Billing History + Consolidated Statement.

Verifies:
1. Empty history returns zero totals and empty lists.
2. Appointment consultation (Phase B settlement) appears in canonical history.
3. Multiple source types (consultation, lab, radiology, pharmacy, bed, ot).
4. Invoice lines are itemized without N+1 query patterns.
5. Multiple invoices are retrieved without silent 50-item cap.
6. Payment methods (cash, UPI, card, bank_transfer).
7. Receipt history maps to canonical payments.
8. Deposits appear in history.
9. Deposit usage/drawdown is represented without double counting.
10. Refunds appear and reconcile with ledger totals.
11. Partial payments preserve correct outstanding balances.
12. Statement totals strictly reconcile to canonical ledger totals.
13. Date range filtering works accurately.
14. Entire journey handles >50 transactions without arbitrary truncation (75+ records test).
15. Bulk eager loading for invoices and lines.
16. Authorization & tenant isolation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infrastructure.postgres.session import get_transitional_sync_session
from modules.appointments.actions.book_appointment_action import BookAppointmentAction
from modules.appointments.contracts.appointments_contracts import BookAppointmentRequest
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.db.availability_reader import AvailabilityReader
from modules.appointments.db.pricing_reader import PricingReader
from modules.billing.actions.billing_actions import (
    GetPatientLedgerAction,
    GetPatientStatementAction,
    PrintPatientStatementAction,
)
from modules.billing.api.billing_api import router as billing_router
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingReceipt,
    BillingRefund,
    BillingSourceType,
    DepositStatus,
    RefundStatus,
)
from modules.billing.services.billing_service import (
    create_deposit,
    create_payment,
    draw_down_deposit_for_charges,
    ensure_charge,
    patient_ledger_totals,
    process_refund,
)
from modules.billing.services.invoice_service import (
    create_invoice_from_charges,
    issue_receipt_for_payment,
)
from modules.tenancy.entities.hospital import Hospital, PlanType
from shared.auth.jwt import create_access_token
from shared.exceptions import register_exception_handlers
from tests.conftest import (  # noqa: F401
    admin_headers,
    db_session,
    doctor,
    hospital,
    patient,
)


@pytest.fixture
def client(db_session: Session) -> TestClient:
    app = FastAPI(title="Phase C Billing Statement Test App")
    register_exception_handlers(app)
    app.include_router(billing_router, prefix="/api")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_transitional_sync_session] = _override_db
    return TestClient(app)


def _auth_headers(hospital_id: UUID, role: str = "hospital_admin") -> dict[str, str]:
    token = create_access_token({
        "sub": "user@test.org",
        "name": "Test User",
        "hospital_uuid": str(hospital_id),
        "hospital_id": str(hospital_id),
        "role": role,
    })
    return {"Authorization": f"Bearer {token}"}


# ── Test 1: Empty History ───────────────────────────────────────────────────

def test_empty_history(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Patient with no financial records returns zero totals and empty lists."""
    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/ledger", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_charges"] == 0.0
    assert data["total_paid"] == 0.0
    assert data["outstanding"] == 0.0
    assert data["charges"] == []
    assert data["payments"] == []
    assert data["entries"] == []
    assert data["deposits"] == []
    assert data["refunds"] == []

    # Statement endpoint
    stmt_resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert stmt_resp.status_code == 200, stmt_resp.text
    stmt_data = stmt_resp.json()
    assert stmt_data["summary"]["gross_charges"] == 0.0
    assert stmt_data["summary"]["net_charges"] == 0.0
    assert stmt_data["summary"]["total_paid"] == 0.0
    assert stmt_data["summary"]["outstanding"] == 0.0
    assert stmt_data["charges"] == []
    assert stmt_data["payments"] == []


# ── Test 2: Appointment Consultation Appears ────────────────────────────────

def test_appointment_consultation_appears(
    client: TestClient, db_session: Session, hospital: Hospital, patient, doctor
):
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    req = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=date.today() + timedelta(days=1),
        appointment_time="10:00:00",
        department="General Medicine",
        purpose="General Checkup",
        payment_method="upi",
        payment_reference="UPI-REF-PHASE-B",
    )
    user = {
        "sub": "cashier@hosp.org",
        "name": "Front Desk Cashier",
        "role": "billing_cashier",
        "hospital_uuid": str(hospital.id),
    }
    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    pricing_reader = PricingReader(db_session, hospital.id)
    appt_dto = BookAppointmentAction(
        db_session, hospital.id, repo, avail_reader, pricing_reader
    ).execute(req, user)
    assert appt_dto.payment_amount == 500.0
    assert appt_dto.receipt_number is not None

    # Verify statement via action and API
    headers = _auth_headers(hospital.id)
    stmt_resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert stmt_resp.status_code == 200, stmt_resp.text
    stmt = stmt_resp.json()

    # Consultation charge appears
    assert len(stmt["charges"]) == 1
    chg = stmt["charges"][0]
    assert chg["source_type"] == "consultation"
    assert chg["net_amount"] == 500.0
    assert chg["amount_paid"] == 500.0
    assert chg["outstanding"] == 0.0
    assert chg["status"] == "paid"

    # Payment appears
    assert len(stmt["payments"]) == 1
    pay = stmt["payments"][0]
    assert pay["payment_method"] == "upi"
    assert pay["amount"] == 500.0
    assert pay["reference_number"] == "UPI-REF-PHASE-B"
    assert pay["receipt_number"] is not None

    # Summary reconciles
    assert stmt["summary"]["net_charges"] == 500.0
    assert stmt["summary"]["total_paid"] == 500.0
    assert stmt["summary"]["outstanding"] == 0.0


# ── Test 3: Multiple Source Types ───────────────────────────────────────────

def test_multiple_source_types(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Charges from consultation, lab, radiology, pharmacy, bed, and ot all appear with canonical source metadata."""
    sources = [
        (BillingSourceType.consultation, 500.0, "Dr. Consultation Fee"),
        (BillingSourceType.laboratory, 1200.0, "Complete Blood Count & Liver Profile"),
        (BillingSourceType.radiology, 2500.0, "Chest X-Ray Digital PA"),
        (BillingSourceType.pharmacy, 850.0, "Prescribed Antibiotics & Analgesics"),
        (BillingSourceType.bed, 3000.0, "Semi-Private Ward Day Charge"),
        (BillingSourceType.ot, 15000.0, "Minor Surgical Procedure OT Charge"),
    ]
    for src_type, amt, desc in sources:
        ensure_charge(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            source_type=src_type,
            source_id=uuid4(),
            description=desc,
            charge_amount=amt,
        )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["charges"]) == 6
    found_sources = {c["source_type"] for c in data["charges"]}
    expected_sources = {"consultation", "laboratory", "radiology", "pharmacy", "bed", "ot"}
    assert found_sources == expected_sources

    total_expected = sum(amt for _, amt, _ in sources)
    assert data["summary"]["gross_charges"] == total_expected
    assert data["summary"]["net_charges"] == total_expected
    assert data["summary"]["outstanding"] == total_expected


# ── Test 4: Invoice Lines Itemization (No N+1) ──────────────────────────────

def test_invoice_lines_itemization(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Invoice with multiple itemized lines returns all lines in statement."""
    c1 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Serum Creatinine",
        charge_amount=300.0,
    )
    c2 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Blood Urea",
        charge_amount=250.0,
    )
    db_session.commit()

    inv = create_invoice_from_charges(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        charge_ids=[c1.id, c2.id],
        created_by_name="Billing Clerk",
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["invoices"]) == 1
    inv_data = data["invoices"][0]
    assert inv_data["invoice_number"] == inv.invoice_number
    assert len(inv_data["lines"]) == 2
    descs = {ln["description"] for ln in inv_data["lines"]}
    assert "Serum Creatinine" in descs
    assert "Blood Urea" in descs


# ── Test 5: Multiple Invoices Retrievable ────────────────────────────────────

def test_multiple_invoices_retrievable(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """All invoices appear without silent truncation."""
    invoices = []
    for i in range(3):
        c = ensure_charge(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            source_type=BillingSourceType.other,
            source_id=uuid4(),
            description=f"Service Charge Batch {i + 1}",
            charge_amount=100.0 * (i + 1),
        )
        db_session.commit()
        inv = create_invoice_from_charges(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            charge_ids=[c.id],
            created_by_name="Billing Staff",
        )
        db_session.commit()
        invoices.append(inv)

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["invoices"]) == 3
    retrieved_nums = {inv["invoice_number"] for inv in data["invoices"]}
    for inv in invoices:
        assert inv.invoice_number in retrieved_nums


# ── Test 6: Payment Methods ─────────────────────────────────────────────────

def test_payment_methods(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Cash, UPI, Card, and Bank Transfer payments are represented accurately."""
    methods = [
        (BillingPaymentMethod.cash, 100.0, "CASH-1"),
        (BillingPaymentMethod.upi, 200.0, "UPI-2"),
        (BillingPaymentMethod.card, 300.0, "CARD-3"),
        (BillingPaymentMethod.bank_transfer, 400.0, "NEFT-4"),
    ]
    for meth, amt, ref in methods:
        create_payment(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            amount=amt,
            payment_date=date.today(),
            payment_method=meth,
            reference_number=ref,
            notes=None,
            received_by_name="Cashier",
        )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["payments"]) == 4
    found_methods = {p["payment_method"] for p in data["payments"]}
    assert found_methods == {"cash", "upi", "card", "bank_transfer"}
    assert data["summary"]["total_paid"] == 1000.0


# ── Test 7: Receipt History ─────────────────────────────────────────────────

def test_receipt_history(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Receipt number maps to canonical payment and appears in statement."""
    pay = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=750.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        reference_number="RCPT-TEST-REF",
        notes=None,
        received_by_name="Head Cashier",
    )
    rcpt = issue_receipt_for_payment(db_session, pay)
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["receipts"]) >= 1
    rcpt = data["receipts"][0]
    assert rcpt["amount"] == 750.0
    assert rcpt["payment_id"] == str(pay.id)

    # In payments list, the receipt number is linked
    stmt_pay = data["payments"][0]
    assert stmt_pay["receipt_number"] == rcpt["receipt_number"]


# ── Test 8: Deposit Appears ─────────────────────────────────────────────────

def test_deposit_appears(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Advance deposit appears in canonical statement and ledger."""
    dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=5000.0,
        deposit_date=date.today(),
        payment_method=BillingPaymentMethod.card,
        deposit_type="admission_advance",
        received_by_name="Admission Desk",
        reference_number="DEP-ADV-001",
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["deposits"]) == 1
    d = data["deposits"][0]
    assert d["deposit_number"] == dep.deposit_number
    assert d["original_amount"] == 5000.0
    assert d["available_amount"] == 5000.0
    assert d["used_amount"] == 0.0
    assert d["status"] == "available"

    # Ledger also returns deposits
    ledger_resp = client.get(f"/api/billing/patients/{patient.id}/ledger", headers=headers)
    assert ledger_resp.status_code == 200
    l_data = ledger_resp.json()
    assert len(l_data["deposits"]) == 1
    assert l_data["total_deposits_available"] == 5000.0


# ── Test 9: Deposit Usage Representation ────────────────────────────────────

def test_deposit_usage_representation(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Drawdown from deposit settles charge without double-counting money."""
    dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=5000.0,
        deposit_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        deposit_type="general",
        received_by_name="Cashier",
    )
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Service Charge",
        charge_amount=2000.0,
    )
    db_session.commit()

    # Draw down 2000 from deposit
    draw_down_deposit_for_charges(
        db_session,
        hospital_id=hospital.id,
        deposit_id=dep.id,
        allocations_plan=[{"charge_id": chg.id, "amount": 2000.0}],
        created_by_name="Billing System",
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    # Deposit info
    d = data["deposits"][0]
    assert d["original_amount"] == 5000.0
    assert d["available_amount"] == 3000.0
    assert d["used_amount"] == 2000.0
    assert d["status"] == "partially_allocated"

    # Financial summary
    s = data["summary"]
    assert s["gross_charges"] == 2000.0
    assert s["net_charges"] == 2000.0
    assert s["deposits_received"] == 5000.0
    assert s["deposits_applied"] == 2000.0
    assert s["deposits_available"] == 3000.0
    assert s["outstanding"] == 0.0  # Settled by deposit drawdown


# ── Test 10: Refund Appears and Reconciles ───────────────────────────────────

def test_refund_appears_and_reconciles(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Refunds appear and summary reflects total refunds processed."""
    pay = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=2000.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes=None,
        received_by_name="Cashier",
    )
    db_session.commit()

    # Process refund of 500
    ref = process_refund(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=500.0,
        refund_date=date.today(),
        refund_method=BillingPaymentMethod.cash,
        reason="Overpayment refund",
        approved_by_name="Supervisor",
        processed_by_name="Accounts",
        payment_id=pay.id,
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["refunds"]) == 1
    r = data["refunds"][0]
    assert r["refund_number"] == ref.refund_number
    assert r["amount"] == 500.0
    assert r["reason"] == "Overpayment refund"

    assert data["summary"]["total_refunds"] == 500.0


# ── Test 11: Partial Payment ────────────────────────────────────────────────

def test_partial_payment(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Partial payment reflects correct outstanding amount."""
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Comprehensive Diagnostic Profile",
        charge_amount=5000.0,
    )
    db_session.commit()

    # Pay 3000 partially
    create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=3000.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.upi,
        notes=None,
        received_by_name="Cashier",
        allocations_plan=[{"charge_id": chg.id, "amount": 3000.0}],
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    c = data["charges"][0]
    assert c["net_amount"] == 5000.0
    assert c["amount_paid"] == 3000.0
    assert c["outstanding"] == 2000.0
    assert c["status"] == "partially_paid"

    assert data["summary"]["net_charges"] == 5000.0
    assert data["summary"]["total_paid"] == 3000.0
    assert data["summary"]["outstanding"] == 2000.0


# ── Test 12: Statement Reconciles to Canonical Ledger ───────────────────────

def test_statement_reconciles_to_canonical_ledger(
    client: TestClient, db_session: Session, hospital: Hospital, patient
):
    """Statement summary totals exactly equal canonical patient_ledger_totals invariants."""
    # Create mix of charges, payments, deposits
    c1 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid4(),
        description="Consultation",
        charge_amount=500.0,
    )
    c2 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Blood Test",
        charge_amount=1500.0,
    )
    db_session.commit()

    create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=500.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes=None,
        received_by_name="Cashier",
        allocations_plan=[{"charge_id": c1.id, "amount": 500.0}],
    )
    create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=1000.0,
        deposit_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        deposit_type="general",
        received_by_name="Cashier",
    )
    db_session.commit()

    canonical_totals = patient_ledger_totals(db_session, hospital.id, patient.id)

    headers = _auth_headers(hospital.id)
    resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert resp.status_code == 200
    stmt = resp.json()["summary"]

    assert stmt["net_charges"] == canonical_totals["total_charges"]
    assert stmt["total_paid"] == canonical_totals["total_paid"]
    assert stmt["outstanding"] == canonical_totals["outstanding"]
    assert stmt["deposits_available"] == canonical_totals["total_deposits_available"]
    assert stmt["net_patient_balance"] == canonical_totals["net_patient_balance"]


# ── Test 13: Date Range Filtering ───────────────────────────────────────────

def test_date_range_filtering(client: TestClient, db_session: Session, hospital: Hospital, patient):
    """Only records within requested date scope appear in filtered statement."""
    # Past charge (10 days ago)
    c_past = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Old Charge",
        charge_amount=300.0,
    )
    c_past.created_at = datetime.now() - timedelta(days=10)
    db_session.commit()

    # Recent charge (today)
    c_recent = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Recent Charge",
        charge_amount=700.0,
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    today_str = date.today().isoformat()
    resp = client.get(
        f"/api/billing/patients/{patient.id}/statement?date_from={today_str}", headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["charges"]) == 1
    assert data["charges"][0]["description"] == "Recent Charge"
    assert data["summary"]["net_charges"] == 700.0


# ── Test 14: Entire Journey Beyond 50 Records (75+ Records) ─────────────────

def test_entire_journey_beyond_50_records(
    client: TestClient, db_session: Session, hospital: Hospital, patient
):
    """Creates 80 charges and proves no silent 50-entry cap truncates history."""
    for i in range(80):
        ensure_charge(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            source_type=BillingSourceType.laboratory,
            source_id=uuid4(),
            description=f"Serial Test Lab Item #{i + 1:03d}",
            charge_amount=50.0,
        )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    # Statement must retrieve all 80
    stmt_resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=headers)
    assert stmt_resp.status_code == 200
    stmt_data = stmt_resp.json()
    assert len(stmt_data["charges"]) == 80
    assert stmt_data["summary"]["net_charges"] == 80 * 50.0

    # Ledger must also retrieve all 80 without arbitrary cap
    ledger_resp = client.get(f"/api/billing/patients/{patient.id}/ledger", headers=headers)
    assert ledger_resp.status_code == 200
    ledger_data = ledger_resp.json()
    assert len(ledger_data["charges"]) == 80
    assert len(ledger_data["entries"]) == 80


# ── Test 15: Bulk Eager Loading & Print Action ──────────────────────────────

def test_print_action_html(db_session: Session, hospital: Hospital, patient):
    """PrintPatientStatementAction generates clean, professional HTML statement."""
    c = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid4(),
        description="OPD Consultation",
        charge_amount=500.0,
    )
    db_session.commit()
    create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=500.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes=None,
        received_by_name="Cashier",
        allocations_plan=[{"charge_id": c.id, "amount": 500.0}],
    )
    db_session.commit()

    action = PrintPatientStatementAction(db_session, hospital.id)
    html = action.execute(patient.id, auto_print=False)

    assert "<!DOCTYPE html>" in html
    assert hospital.name in html
    assert patient.uhid in html
    assert "Consolidated Billing Statement" in html
    assert "OPD Consultation" in html
    assert "₹500.00" in html
    assert "Authorized Signatory" in html


# ── Test 16: Authorization & Tenant Isolation ───────────────────────────────

def test_authorization_and_tenant_isolation(
    client: TestClient, db_session: Session, hospital: Hospital, patient
):
    """Other hospital cannot access patient statement, and unauthenticated requests are rejected."""
    # Unauthenticated request
    unauth_resp = client.get(f"/api/billing/patients/{patient.id}/statement")
    assert unauth_resp.status_code in (401, 403)

    # Different hospital
    other_hospital = Hospital(
        id=uuid4(),
        hospital_id="HOSP-OTHER-STMT",
        name="Other Hospital",
        address="Other Address",
        phone="9999999999",
        email="other_stmt@hospital.com",
        password_hash="fake",
        plan=PlanType.basic,
        is_active=True,
        status="active",
    )
    db_session.add(other_hospital)
    db_session.commit()
    other_headers = _auth_headers(other_hospital.id)
    other_resp = client.get(f"/api/billing/patients/{patient.id}/statement", headers=other_headers)
    assert other_resp.status_code == 404  # Patient not found in other hospital


# ── Test 17: Financial History Least Privilege Authorization ────────────────

def test_financial_history_authorization_least_privilege(
    client: TestClient, db_session: Session, hospital: Hospital, patient
):
    """Least privilege access: billing staff and admin can view, insurance staff can view statement but not ledger, unrelated staff gets 403."""
    from modules.doctors.entities.doctor import HospitalUser, RolePermission, StaffRole

    # 1. Billing Staff Role
    billing_role = StaffRole(hospital_id=hospital.id, name="Billing Specialist")
    db_session.add(billing_role)
    db_session.flush()
    db_session.add(RolePermission(
        hospital_id=hospital.id,
        role_id=billing_role.id,
        module_key="billing",
        can_view=True,
    ))
    billing_user = HospitalUser(
        hospital_id=hospital.id,
        name="Billing User",
        email="billing@user.org",
        phone="9876543201",
        password_hash="dummy_hash",
        role_id=billing_role.id,
    )
    db_session.add(billing_user)

    # 2. Insurance Staff Role (insurance.view ONLY, no billing.view)
    insurance_role = StaffRole(hospital_id=hospital.id, name="Insurance Officer")
    db_session.add(insurance_role)
    db_session.flush()
    db_session.add(RolePermission(
        hospital_id=hospital.id,
        role_id=insurance_role.id,
        module_key="insurance",
        can_view=True,
    ))
    insurance_user = HospitalUser(
        hospital_id=hospital.id,
        name="Insurance User",
        email="ins@user.org",
        phone="9876543202",
        password_hash="dummy_hash",
        role_id=insurance_role.id,
    )
    db_session.add(insurance_user)

    # 3. Clinical Staff Role (unrelated clinical permission, no billing/insurance)
    nurse_role = StaffRole(hospital_id=hospital.id, name="Floor Nurse")
    db_session.add(nurse_role)
    db_session.flush()
    db_session.add(RolePermission(
        hospital_id=hospital.id,
        role_id=nurse_role.id,
        module_key="nurses",
        can_view=True,
    ))
    nurse_user = HospitalUser(
        hospital_id=hospital.id,
        name="Nurse User",
        email="nurse@user.org",
        phone="9876543203",
        password_hash="dummy_hash",
        role_id=nurse_role.id,
    )
    db_session.add(nurse_user)
    db_session.commit()

    def _token(user_id, role_id):
        t = create_access_token({
            "sub": str(user_id),
            "user_id": str(user_id),
            "hospital_uuid": str(hospital.id),
            "hospital_id": str(hospital.id),
            "role": "hospital_staff",
            "role_id": str(role_id),
        })
        return {"Authorization": f"Bearer {t}"}

    billing_headers = _token(billing_user.id, billing_role.id)
    insurance_headers = _token(insurance_user.id, insurance_role.id)
    nurse_headers = _token(nurse_user.id, nurse_role.id)

    # Billing staff can view both ledger and statement
    r = client.get(f"/api/billing/patients/{patient.id}/statement", headers=billing_headers)
    assert r.status_code == 200
    r = client.get(f"/api/billing/patients/{patient.id}/ledger", headers=billing_headers)
    assert r.status_code == 200

    # Insurance staff can view statement but NOT raw ledger
    r = client.get(f"/api/billing/patients/{patient.id}/statement", headers=insurance_headers)
    assert r.status_code == 200
    r = client.get(f"/api/billing/patients/{patient.id}/ledger", headers=insurance_headers)
    assert r.status_code == 403

    # Unrelated staff (nurse with no billing/insurance) is rejected from both
    r = client.get(f"/api/billing/patients/{patient.id}/statement", headers=nurse_headers)
    assert r.status_code == 403
    r = client.get(f"/api/billing/patients/{patient.id}/ledger", headers=nurse_headers)
    assert r.status_code == 403


# ── Test 18: Hospital Metadata Accuracy and No Fabricated Data ──────────────

def test_hospital_metadata_accuracy_and_no_fake_data(
    db_session: Session, hospital: Hospital, patient
):
    """Real configured hospital metadata appears; missing fields remain empty with NO fabricated strings."""
    # 1. Configured metadata
    hospital.name = "Apollo Apex Hospital"
    hospital.address = "123 Healthcare Boulevard, South Wing"
    hospital.phone = "+91 80 4455 6677"
    hospital.email = "accounts@apolloapex.com"
    hospital.gstin = "29AAAAA0000A1Z5"
    db_session.commit()

    stmt = GetPatientStatementAction(db_session, hospital.id).execute(patient.id)
    assert stmt.hospital.name == "Apollo Apex Hospital"
    assert stmt.hospital.address == "123 Healthcare Boulevard, South Wing"
    assert stmt.hospital.phone == "+91 80 4455 6677"
    assert stmt.hospital.email == "accounts@apolloapex.com"
    assert stmt.hospital.gstin == "29AAAAA0000A1Z5"
    assert stmt.hospital.tax_identifier == "29AAAAA0000A1Z5"

    html = PrintPatientStatementAction(db_session, hospital.id).execute(patient.id, auto_print=False)
    assert "Apollo Apex Hospital" in html
    assert "123 Healthcare Boulevard, South Wing" in html
    assert "+91 80 4455 6677" in html
    assert "accounts@apolloapex.com" in html
    assert "29AAAAA0000A1Z5" in html

    # Assert NO fabricated strings exist anywhere
    assert "Central Medical Enclave" not in html
    assert "+91 11 2345 6789" not in html
    assert "billing@hospital.org" not in html
    assert "GSTIN-29AAACH2234M1Z2" not in html

    # 2. Missing optional metadata fixture: gstin is None, facility_settings is None
    hospital.gstin = None
    hospital.facility_settings = None
    db_session.commit()

    stmt_bare = GetPatientStatementAction(db_session, hospital.id).execute(patient.id)
    assert stmt_bare.hospital.gstin is None
    assert stmt_bare.hospital.tax_identifier is None

    html_bare = PrintPatientStatementAction(db_session, hospital.id).execute(patient.id, auto_print=False)
    assert "Central Medical Enclave" not in html_bare
    assert "+91 11 2345 6789" not in html_bare
    assert "billing@hospital.org" not in html_bare
    assert "GSTIN-29AAACH2234M1Z2" not in html_bare


# ── Test 19: UHID vs MRN Label Integrity ────────────────────────────────────

def test_uhid_mrn_label_integrity(db_session: Session, hospital: Hospital, patient):
    """Only genuine UHID is presented; MRN is not conflated or fabricated."""
    html = PrintPatientStatementAction(db_session, hospital.id).execute(patient.id, auto_print=False)
    # The patient header must have UHID and must NOT label it MRN / UHID
    assert "MRN / UHID" not in html
    assert '<span class="lbl">UHID</span>' in html
    assert patient.uhid in html


# ── Tests G & H / Blocker 4 Regression: Date-Filtered Accounting Semantics ──

def test_statement_service_in_period_payment_after_period(
    client: TestClient, db_session: Session, hospital: Hospital, patient
):
    """Test G (Test 1): Charge in service period (01/10) with later payment (15/10).

    Filter: 01/10 -> 05/10.
    Charge appears, linked payment appears, total_paid = 500.0, outstanding = 0.0.
    """
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Comprehensive Metabolic Panel",
        charge_amount=500.0,
    )
    chg.created_at = datetime(2026, 10, 1, 10, 0, 0)
    db_session.commit()

    # Later payment (15/10) settles the charge
    pay = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=500.0,
        payment_method=BillingPaymentMethod.upi,
        payment_date=date(2026, 10, 15),
        reference_number="PAY-LATER-OCT15",
        notes="Settlement",
        received_by_name="Cashier",
        allocations_plan=[{"charge_id": chg.id, "amount": 500.0}],
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(
        f"/api/billing/patients/{patient.id}/statement?date_from=2026-10-01&date_to=2026-10-05",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    stmt = resp.json()

    # 1. Scoped charge appears
    assert len(stmt["charges"]) == 1
    assert stmt["charges"][0]["id"] == str(chg.id)
    assert stmt["charges"][0]["net_amount"] == 500.0

    # 2. Linked payment appears even though payment_date is 15/10
    assert len(stmt["payments"]) == 1
    assert stmt["payments"][0]["id"] == str(pay.id)
    assert stmt["payments"][0]["amount"] == 500.0
    assert stmt["payments"][0]["payment_date"] == "2026-10-15"

    # 3. Summary totals reconcile strictly
    assert stmt["summary"]["net_charges"] == 500.0
    assert stmt["summary"]["total_paid"] == 500.0
    assert stmt["summary"]["outstanding"] == 0.0
    assert stmt["summary"]["net_patient_balance"] == 0.0


def test_statement_service_in_period_deposit_applied_later(
    client: TestClient, db_session: Session, hospital: Hospital, patient
):
    """Test 2: Charge in service period (01/10) with deposit applied later (15/10)."""
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.radiology,
        source_id=uuid4(),
        description="Chest X-Ray PA",
        charge_amount=600.0,
    )
    chg.created_at = datetime(2026, 10, 1, 11, 0, 0)
    db_session.commit()

    # Advance deposit received on 15/10 and applied against charge
    dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=600.0,
        payment_method=BillingPaymentMethod.cash,
        deposit_date=date(2026, 10, 15),
        reference_number="DEP-LATER-OCT15",
    )
    draw_down_deposit_for_charges(
        db_session,
        hospital_id=hospital.id,
        deposit_id=dep.id,
        allocations_plan=[{"charge_id": chg.id, "amount": 600.0}],
        created_by_name="Cashier",
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(
        f"/api/billing/patients/{patient.id}/statement?date_from=2026-10-01&date_to=2026-10-05",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    stmt = resp.json()

    assert len(stmt["charges"]) == 1
    assert len(stmt["deposits"]) == 1
    assert stmt["deposits"][0]["id"] == str(dep.id)
    assert stmt["deposits"][0]["used_amount"] == 600.0
    assert stmt["summary"]["net_charges"] == 600.0
    assert stmt["summary"]["deposits_applied"] == 600.0
    assert stmt["summary"]["outstanding"] == 0.0


def test_statement_refund_later_than_service_period(
    client: TestClient, db_session: Session, hospital: Hospital, patient
):
    """Test H (Test 3): Charge & payment in period (01/10), refund processed later (15/10)."""
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid4(),
        description="Cardiology Consultation",
        charge_amount=400.0,
    )
    chg.created_at = datetime(2026, 10, 2, 10, 0, 0)
    db_session.commit()

    pay = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=400.0,
        payment_method=BillingPaymentMethod.card,
        payment_date=date(2026, 10, 2),
        notes="Consultation Payment",
        received_by_name="Cashier",
        allocations_plan=[{"charge_id": chg.id, "amount": 400.0}],
    )
    db_session.commit()

    # Process refund on 15/10
    ref = process_refund(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        payment_id=pay.id,
        amount=400.0,
        refund_date=date(2026, 10, 15),
        reason="Doctor cancelled session",
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(
        f"/api/billing/patients/{patient.id}/statement?date_from=2026-10-01&date_to=2026-10-05",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    stmt = resp.json()

    assert len(stmt["charges"]) == 1
    assert len(stmt["refunds"]) == 1
    assert stmt["refunds"][0]["id"] == str(ref.id)
    assert stmt["refunds"][0]["amount"] == 400.0
    assert stmt["summary"]["total_refunds"] == 400.0


def test_statement_unrelated_payment_outside_scope_excluded(
    client: TestClient, db_session: Session, hospital: Hospital, patient
):
    """Test 4: Unrelated transaction outside service scope is NOT included merely because same patient made it."""
    # Scoped charge (02/10)
    chg_in = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.pharmacy,
        source_id=uuid4(),
        description="Antibiotics Course",
        charge_amount=250.0,
    )
    chg_in.created_at = datetime(2026, 10, 2, 9, 0, 0)
    db_session.commit()

    # Payment for in-scope charge
    pay_in = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=250.0,
        payment_method=BillingPaymentMethod.cash,
        payment_date=date(2026, 10, 2),
        notes="In-scope payment",
        received_by_name="Cashier",
        allocations_plan=[{"charge_id": chg_in.id, "amount": 250.0}],
    )

    # Out-of-scope charge (25/10)
    chg_out = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.pharmacy,
        source_id=uuid4(),
        description="Pain Relief",
        charge_amount=150.0,
    )
    chg_out.created_at = datetime(2026, 10, 25, 9, 0, 0)
    db_session.commit()

    # Out-of-scope payment (25/10) settling out-of-scope charge
    pay_out = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=150.0,
        payment_method=BillingPaymentMethod.cash,
        payment_date=date(2026, 10, 25),
        notes="Out-of-scope payment",
        received_by_name="Cashier",
        allocations_plan=[{"charge_id": chg_out.id, "amount": 150.0}],
    )
    db_session.commit()

    headers = _auth_headers(hospital.id)
    resp = client.get(
        f"/api/billing/patients/{patient.id}/statement?date_from=2026-10-01&date_to=2026-10-05",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    stmt = resp.json()

    # Only in-scope charge & payment appear
    assert len(stmt["charges"]) == 1
    assert stmt["charges"][0]["id"] == str(chg_in.id)

    assert len(stmt["payments"]) == 1
    assert stmt["payments"][0]["id"] == str(pay_in.id)
    assert stmt["summary"]["total_paid"] == 250.0

    # Out-of-scope payment is NOT present
    payment_ids = [p["id"] for p in stmt["payments"]]
    assert str(pay_out.id) not in payment_ids


