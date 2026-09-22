"""
Integration tests for Billing-Gated Laboratory Workflow.

Enforces:
Doctor Prescription -> Billing Charge (Pending) -> Financial Clearance Required ->
Actionable Lab Worklist -> Accession -> Sample Collection -> Results.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.laboratory.api.laboratory_api import router as laboratory_router
from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestStatus,
    LabSampleType,
    LabTestCatalog,
)
from modules.laboratory.services.lab_prescription_service import (
    create_investigation_requests_for_prescription,
)
from modules.masters.entities.organization_entities import Department  # noqa: F401
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
import tests.conftest  # noqa: F401
from shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def lab_gated_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def lab_gated_client(lab_gated_db):
    app = FastAPI()
    app.include_router(laboratory_router, prefix="/api")

    hospital_id = uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id="HOSP-LAB-GATE",
        name="Metro Hospital",
        address="100 Medical Blvd",
        phone="9876543210",
        email="metro@hospital.com",
        password_hash="fakehash",
        is_active=True,
    )
    lab_gated_db.add(hospital)

    role = StaffRole(
        id=uuid4(),
        hospital_id=hospital_id,
        name="hospital_admin",
        description="Admin",
        is_active=True,
    )
    lab_gated_db.add(role)

    user_id = uuid4()
    user = HospitalUser(
        id=user_id,
        hospital_id=hospital_id,
        role_id=role.id,
        name="Dr. Sarah Pathologist",
        phone="9876543210",
        email="sarah@hospital.com",
        password_hash="fakehash",
        is_active=True,
    )
    lab_gated_db.add(user)
    lab_gated_db.commit()

    app.dependency_overrides[get_transitional_sync_session] = lambda: lab_gated_db
    app.dependency_overrides[get_hospital_context] = lambda: hospital_id
    app.dependency_overrides[require_hospital_user] = lambda: {
        "sub": str(user_id),
        "hospital_id": str(hospital_id),
        "name": "Dr. Sarah Pathologist",
        "role": "hospital_admin",
        "staff_role_name": "Hospital Administrator",
    }

    client = TestClient(app)
    client.test_hospital_id = hospital_id
    client.test_user_id = user_id
    return client


def test_billing_gated_lab_workflow_full_lifecycle(lab_gated_client, lab_gated_db):
    h_id = lab_gated_client.test_hospital_id
    user_id = lab_gated_client.test_user_id

    # 1. Create Patient & Lab Test
    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-LAB-GATE-01",
        name="Anita Desai",
        mobile="9876500001",
        age=34,
        gender="female",
    )
    lab_gated_db.add(patient)

    test = LabTestCatalog(
        id=uuid4(),
        hospital_id=h_id,
        test_code="CBC",
        test_name="Complete Blood Count",
        department="Hematology",
        price=350.0,
        sample_type=LabSampleType.blood,
        tat_hours=6,
        is_active=True,
    )
    lab_gated_db.add(test)
    lab_gated_db.commit()

    # 2. Doctor prescribes CBC -> creates LabPrescriptionRequest + BillingCharge (pending)
    rx_id = uuid4()
    lab_req, rad_req = create_investigation_requests_for_prescription(
        db=lab_gated_db,
        hospital_id=h_id,
        prescription_id=rx_id,
        patient_id=patient.id,
        doctor_id=user_id,
        appointment_id=None,
        test_ids=[test.id],
        panel_ids=[],
        scan_ids=[],
        clinical_notes="Routine pre-op workup",
    )
    lab_gated_db.commit()

    assert lab_req is not None
    assert lab_req.status == LabPrescriptionRequestStatus.pending

    # Verify billing charge created on target ledger
    charges = (
        lab_gated_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.source_id == lab_req.id,
            BillingCharge.source_type == BillingSourceType.laboratory,
        )
        .all()
    )
    assert len(charges) == 1
    charge = charges[0]
    assert charge.charge_amount == 350.0
    assert charge.status == BillingChargeStatus.pending

    # 3. Queue Isolation: Unpaid request must NOT be in pending actionable dashboard list
    dash_resp = lab_gated_client.get("/api/laboratory/dashboard")
    assert dash_resp.status_code == 200
    dash_data = dash_resp.json()
    assert dash_data["pending_doctor_requests"] == 0
    assert len(dash_data["pending_requests"]) == 0

    # Default pending list query (for laboratory queue) excludes unreleased requests
    list_resp = lab_gated_client.get("/api/laboratory/prescription-requests?status=pending")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 0

    # Patient-specific history displays it with clear financial status
    pt_list_resp = lab_gated_client.get(f"/api/laboratory/prescription-requests?patient_id={patient.id}")
    assert pt_list_resp.status_code == 200
    pt_reqs = pt_list_resp.json()
    assert len(pt_reqs) == 1
    assert pt_reqs[0]["is_financially_cleared"] is False
    assert pt_reqs[0]["payment_status"] == "pending"

    # 4. Accession attempt when unpaid MUST be blocked with HTTP 402
    accession_resp = lab_gated_client.post(
        "/api/laboratory/orders",
        json={
            "patient_id": str(patient.id),
            "doctor_id": str(user_id),
            "prescription_request_id": str(lab_req.id),
        },
    )
    assert accession_resp.status_code == 402
    err_detail = accession_resp.json()["detail"]
    assert (
        err_detail.get("code") == "FINANCIAL_CLEARANCE_REQUIRED"
        or "clearance" in str(err_detail).lower()
        or "payment" in str(err_detail).lower()
    )

    # Verify no LabOrder was created
    order_count = (
        lab_gated_db.query(LabOrder)
        .filter(LabOrder.hospital_id == h_id, LabOrder.prescription_request_id == lab_req.id)
        .count()
    )
    assert order_count == 0

    # 5. Financial Clearance (e.g. Patient pays at billing counter)
    charge.status = BillingChargeStatus.paid
    lab_gated_db.commit()

    # Now actionable queue includes the request
    dash_resp2 = lab_gated_client.get("/api/laboratory/dashboard")
    assert dash_resp2.status_code == 200
    assert dash_resp2.json()["pending_doctor_requests"] == 1
    assert len(dash_resp2.json()["pending_requests"]) == 1

    list_resp2 = lab_gated_client.get("/api/laboratory/prescription-requests?status=pending")
    assert list_resp2.status_code == 200
    assert len(list_resp2.json()) == 1
    assert list_resp2.json()[0]["is_financially_cleared"] is True

    # 6. Accession succeeds after financial clearance
    accession_resp2 = lab_gated_client.post(
        "/api/laboratory/orders",
        json={
            "patient_id": str(patient.id),
            "doctor_id": str(user_id),
            "prescription_request_id": str(lab_req.id),
        },
    )
    assert accession_resp2.status_code == 201
    order_data = accession_resp2.json()
    order_id = UUID(order_data["id"])
    assert order_data["status"] == "ordered"
    assert order_data["is_financially_cleared"] is True

    # 7. Zero Duplicate Charges: Ensure the charge was re-pointed to order.id
    all_lab_charges = (
        lab_gated_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.patient_id == patient.id,
            BillingCharge.source_type == BillingSourceType.laboratory,
        )
        .all()
    )
    # Exactly ONE charge for this patient's CBC!
    assert len(all_lab_charges) == 1
    assert all_lab_charges[0].source_id == order_id
    assert all_lab_charges[0].status == BillingChargeStatus.paid

    # 8. Downstream operational actions proceed smoothly
    collect_resp = lab_gated_client.post(
        f"/api/laboratory/orders/{order_id}/collect-sample",
        json={
            "collected_by": "Nurse Priya",
            "sample_type": "blood",
            "collection_remarks": "Fasting blood sample",
        },
    )
    assert collect_resp.status_code == 200
    assert collect_resp.json()["status"] == "sample_collected"


def test_billing_gated_stat_emergency_bypass(lab_gated_client, lab_gated_db):
    """Emergency / STAT doctor orders must bypass financial clearance."""
    h_id = lab_gated_client.test_hospital_id
    user_id = lab_gated_client.test_user_id

    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-LAB-STAT-01",
        name="Emergency Patient",
        mobile="9876500002",
        age=45,
        gender="male",
    )
    lab_gated_db.add(patient)

    test = LabTestCatalog(
        id=uuid4(),
        hospital_id=h_id,
        test_code="TROP-I",
        test_name="Troponin I",
        department="Biochemistry",
        price=800.0,
        sample_type=LabSampleType.blood,
        tat_hours=1,
        is_active=True,
    )
    lab_gated_db.add(test)
    lab_gated_db.commit()

    # Prescribe with STAT in notes
    rx_id = uuid4()
    lab_req, _ = create_investigation_requests_for_prescription(
        db=lab_gated_db,
        hospital_id=h_id,
        prescription_id=rx_id,
        patient_id=patient.id,
        doctor_id=user_id,
        appointment_id=None,
        test_ids=[test.id],
        panel_ids=[],
        scan_ids=[],
        clinical_notes="Acute chest pain STAT emergency",
    )
    lab_gated_db.commit()

    # STAT request appears in actionable queue even before payment
    dash_resp = lab_gated_client.get("/api/laboratory/dashboard")
    assert dash_resp.status_code == 200
    assert dash_resp.json()["pending_doctor_requests"] == 1

    # Accession succeeds immediately (emergency bypass)
    accession_resp = lab_gated_client.post(
        "/api/laboratory/orders",
        json={
            "patient_id": str(patient.id),
            "doctor_id": str(user_id),
            "prescription_request_id": str(lab_req.id),
            "clinical_notes": "STAT accession",
        },
    )
    assert accession_resp.status_code == 201
    assert accession_resp.json()["status"] == "ordered"


def test_prescription_request_cancel_cancels_billing_charge(lab_gated_client, lab_gated_db):
    """Cancelling an unfulfilled prescription request must cancel its pending billing charge."""
    h_id = lab_gated_client.test_hospital_id
    user_id = lab_gated_client.test_user_id

    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-LAB-CNC-01",
        name="Ramesh Patel",
        mobile="9876500003",
        age=60,
        gender="male",
    )
    lab_gated_db.add(patient)

    test = LabTestCatalog(
        id=uuid4(),
        hospital_id=h_id,
        test_code="LFT",
        test_name="Liver Function Test",
        department="Biochemistry",
        price=600.0,
        sample_type=LabSampleType.blood,
        tat_hours=6,
        is_active=True,
    )
    lab_gated_db.add(test)
    lab_gated_db.commit()

    rx_id = uuid4()
    lab_req, _ = create_investigation_requests_for_prescription(
        db=lab_gated_db,
        hospital_id=h_id,
        prescription_id=rx_id,
        patient_id=patient.id,
        doctor_id=user_id,
        appointment_id=None,
        test_ids=[test.id],
        panel_ids=[],
        scan_ids=[],
        clinical_notes="Followup",
    )
    lab_gated_db.commit()

    charge = (
        lab_gated_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.source_id == lab_req.id,
            BillingCharge.source_type == BillingSourceType.laboratory,
        )
        .first()
    )
    assert charge is not None
    assert charge.status == BillingChargeStatus.pending

    # Cancel the prescription request
    cancel_resp = lab_gated_client.post(
        f"/api/laboratory/prescription-requests/{lab_req.id}/cancel",
        json={"reason": "Patient declined test"},
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"

    # Verify billing charge is now cancelled
    lab_gated_db.refresh(charge)
    assert charge.status == BillingChargeStatus.cancelled
