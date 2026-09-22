"""
Integration tests for Billing-Gated Radiology Workflow.

Enforces:
Doctor Prescription -> Billing Charge (Pending) -> Financial Clearance Required ->
Actionable Radiology Worklist -> Accession -> Scheduling -> Scanning -> Report Sign-off.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
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
from modules.laboratory.services.lab_prescription_service import (
    create_investigation_requests_for_prescription,
)
from modules.masters.entities.organization_entities import Department  # noqa: F401
from modules.patients.entities.patient import Patient
from modules.radiology.api.radiology_api import router as radiology_router
from modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
    RadPrescriptionRequest,
    RadPrescriptionRequestStatus,
)
from modules.tenancy.entities.hospital import Hospital
import tests.conftest  # noqa: F401
from shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def rad_gated_db():
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
def rad_gated_client(rad_gated_db):
    app = FastAPI()
    app.include_router(radiology_router, prefix="/api")

    hospital_id = uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id="HOSP-RAD-GATE",
        name="Metro Radiology Center",
        address="200 Imaging Blvd",
        phone="9876543210",
        email="rad@hospital.com",
        password_hash="fakehash",
        is_active=True,
    )
    rad_gated_db.add(hospital)

    role = StaffRole(
        id=uuid4(),
        hospital_id=hospital_id,
        name="hospital_admin",
        description="Admin",
        is_active=True,
    )
    rad_gated_db.add(role)

    user_id = uuid4()
    user = HospitalUser(
        id=user_id,
        hospital_id=hospital_id,
        role_id=role.id,
        name="Dr. Neil Radiologist",
        phone="9876543210",
        email="neil@hospital.com",
        password_hash="fakehash",
        is_active=True,
    )
    rad_gated_db.add(user)
    rad_gated_db.commit()

    app.dependency_overrides[get_transitional_sync_session] = lambda: rad_gated_db
    app.dependency_overrides[get_hospital_context] = lambda: hospital_id
    app.dependency_overrides[require_hospital_user] = lambda: {
        "sub": str(user_id),
        "hospital_id": str(hospital_id),
        "name": "Dr. Neil Radiologist",
        "role": "hospital_admin",
        "staff_role_name": "Chief Radiologist",
    }

    client = TestClient(app)
    client.test_hospital_id = hospital_id
    client.test_user_id = user_id
    return client


def test_billing_gated_radiology_full_lifecycle(rad_gated_client, rad_gated_db):
    h_id = rad_gated_client.test_hospital_id
    user_id = rad_gated_client.test_user_id

    # 1. Patient & Scan
    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-RAD-GATE-01",
        name="Sunil Gavaskar",
        mobile="9876511111",
        age=58,
        gender="male",
    )
    rad_gated_db.add(patient)

    scan = RadiologyScanCatalog(
        id=uuid4(),
        hospital_id=h_id,
        scan_code="XRCHEST",
        scan_name="X-Ray Chest PA View",
        category="X-Ray",
        department="Radiology",
        price=500.0,
        duration_minutes=15,
        is_active=True,
    )
    rad_gated_db.add(scan)
    rad_gated_db.commit()

    # 2. Prescribe scan -> creates RadPrescriptionRequest + BillingCharge (pending)
    rx_id = uuid4()
    _, rad_req = create_investigation_requests_for_prescription(
        db=rad_gated_db,
        hospital_id=h_id,
        prescription_id=rx_id,
        patient_id=patient.id,
        doctor_id=user_id,
        appointment_id=None,
        test_ids=[],
        panel_ids=[],
        scan_ids=[scan.id],
        clinical_notes="Routine health checkup",
    )
    rad_gated_db.commit()

    assert rad_req is not None
    assert rad_req.status == RadPrescriptionRequestStatus.pending

    charges = (
        rad_gated_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.source_id == rad_req.id,
            BillingCharge.source_type == BillingSourceType.radiology,
        )
        .all()
    )
    assert len(charges) == 1
    charge = charges[0]
    assert charge.charge_amount == 500.0
    assert charge.status == BillingChargeStatus.pending

    # 3. Queue Isolation: Unpaid request must not be in released worklist
    list_resp = rad_gated_client.get("/api/radiology/prescription-requests?status=pending")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 0

    pt_list_resp = rad_gated_client.get(f"/api/radiology/prescription-requests?patient_id={patient.id}")
    assert pt_list_resp.status_code == 200
    pt_reqs = pt_list_resp.json()
    assert len(pt_reqs) == 1
    assert pt_reqs[0]["is_financially_cleared"] is False
    assert pt_reqs[0]["payment_status"] == "pending"

    # 4. Accession attempt when unpaid MUST be blocked with HTTP 402
    accession_resp = rad_gated_client.post(
        "/api/radiology/orders",
        json={
            "patient_id": str(patient.id),
            "doctor_id": str(user_id),
            "prescription_request_id": str(rad_req.id),
        },
    )
    assert accession_resp.status_code == 402
    err_detail = accession_resp.json()["detail"]
    assert (
        err_detail.get("code") == "FINANCIAL_CLEARANCE_REQUIRED"
        or "clearance" in str(err_detail).lower()
        or "payment" in str(err_detail).lower()
    )

    # 5. Financial Clearance
    charge.status = BillingChargeStatus.paid
    rad_gated_db.commit()

    # Now pending queue has the released request
    list_resp2 = rad_gated_client.get("/api/radiology/prescription-requests?status=pending")
    assert list_resp2.status_code == 200
    assert len(list_resp2.json()) == 1
    assert list_resp2.json()[0]["is_financially_cleared"] is True

    # 6. Accession succeeds
    accession_resp2 = rad_gated_client.post(
        "/api/radiology/orders",
        json={
            "patient_id": str(patient.id),
            "doctor_id": str(user_id),
            "prescription_request_id": str(rad_req.id),
        },
    )
    assert accession_resp2.status_code == 201
    orders = accession_resp2.json()
    assert len(orders) == 1
    order = orders[0]
    order_id = UUID(order["id"])
    assert order["status"] == "ordered"

    # 7. Zero Duplicate Charges: Existing charge re-pointed to order_id
    all_rad_charges = (
        rad_gated_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.patient_id == patient.id,
            BillingCharge.source_type == BillingSourceType.radiology,
        )
        .all()
    )
    assert len(all_rad_charges) == 1
    assert all_rad_charges[0].source_id == order_id
    assert all_rad_charges[0].status == BillingChargeStatus.paid

    # 8. Operational flow: Schedule -> Start -> Complete -> Upload Report
    sch_resp = rad_gated_client.post(
        f"/api/radiology/orders/{order_id}/schedule",
        json={
            "scheduled_at": datetime.now(timezone.utc).isoformat(),
            "machine": "X-Ray Room 1",
            "technician_name": "Kishore",
        },
    )
    assert sch_resp.status_code == 200
    assert sch_resp.json()["status"] == "scheduled"

    st_resp = rad_gated_client.post(f"/api/radiology/orders/{order_id}/start")
    assert st_resp.status_code == 200
    assert st_resp.json()["status"] == "in_progress"

    cs_resp = rad_gated_client.post(f"/api/radiology/orders/{order_id}/complete-scan")
    assert cs_resp.status_code == 200
    assert cs_resp.json()["status"] == "completed"

    sample_img = "data:image/png;base64," + base64.b64encode(b"img_bytes").decode("ascii")
    rep_resp = rad_gated_client.post(
        f"/api/radiology/orders/{order_id}/report",
        json={
            "findings": "Clear lung fields bilaterally.",
            "impression": "Normal chest radiograph.",
            "image_file_name": "chest.png",
            "image_file_data": sample_img,
        },
    )
    assert rep_resp.status_code == 200
    assert rep_resp.json()["status"] == "completed"
    assert rep_resp.json()["findings"] == "Clear lung fields bilaterally."


def test_billing_gated_radiology_stat_bypass(rad_gated_client, rad_gated_db):
    """STAT Emergency radiology scan bypasses financial clearance."""
    h_id = rad_gated_client.test_hospital_id
    user_id = rad_gated_client.test_user_id

    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-RAD-STAT-01",
        name="Emergency Trauma Patient",
        mobile="9876522222",
        age=29,
        gender="female",
    )
    rad_gated_db.add(patient)

    scan = RadiologyScanCatalog(
        id=uuid4(),
        hospital_id=h_id,
        scan_code="CTHEAD",
        scan_name="CT Head Non-Contrast",
        category="CT",
        department="Radiology",
        price=2500.0,
        duration_minutes=20,
        is_active=True,
    )
    rad_gated_db.add(scan)
    rad_gated_db.commit()

    rx_id = uuid4()
    _, rad_req = create_investigation_requests_for_prescription(
        db=rad_gated_db,
        hospital_id=h_id,
        prescription_id=rx_id,
        patient_id=patient.id,
        doctor_id=user_id,
        appointment_id=None,
        test_ids=[],
        panel_ids=[],
        scan_ids=[scan.id],
        clinical_notes="Acute head injury STAT emergency",
    )
    rad_gated_db.commit()

    # Appears in actionable queue immediately due to STAT override
    list_resp = rad_gated_client.get("/api/radiology/prescription-requests?status=pending")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # Accession succeeds immediately without payment
    accession_resp = rad_gated_client.post(
        "/api/radiology/orders",
        json={
            "patient_id": str(patient.id),
            "doctor_id": str(user_id),
            "prescription_request_id": str(rad_req.id),
            "clinical_notes": "STAT accession",
        },
    )
    assert accession_resp.status_code == 201
    assert accession_resp.json()[0]["status"] == "ordered"


def test_radiology_prescription_cancel_cancels_billing_charge(rad_gated_client, rad_gated_db):
    """Cancelling a radiology prescription request cancels its billing charge."""
    h_id = rad_gated_client.test_hospital_id
    user_id = rad_gated_client.test_user_id

    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-RAD-CNC-01",
        name="Meena Rao",
        mobile="9876533333",
        age=42,
        gender="female",
    )
    rad_gated_db.add(patient)

    scan = RadiologyScanCatalog(
        id=uuid4(),
        hospital_id=h_id,
        scan_code="USGABD",
        scan_name="USG Abdomen & Pelvis",
        category="Ultrasound",
        department="Radiology",
        price=1200.0,
        duration_minutes=30,
        is_active=True,
    )
    rad_gated_db.add(scan)
    rad_gated_db.commit()

    rx_id = uuid4()
    _, rad_req = create_investigation_requests_for_prescription(
        db=rad_gated_db,
        hospital_id=h_id,
        prescription_id=rx_id,
        patient_id=patient.id,
        doctor_id=user_id,
        appointment_id=None,
        test_ids=[],
        panel_ids=[],
        scan_ids=[scan.id],
        clinical_notes="Abdominal pain",
    )
    rad_gated_db.commit()

    charge = (
        rad_gated_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.source_id == rad_req.id,
            BillingCharge.source_type == BillingSourceType.radiology,
        )
        .first()
    )
    assert charge is not None
    assert charge.status == BillingChargeStatus.pending

    # Cancel the prescription request
    cancel_resp = rad_gated_client.post(
        f"/api/radiology/prescription-requests/{rad_req.id}/cancel",
        json={"reason": "Patient requested discharge"},
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"

    rad_gated_db.refresh(charge)
    assert charge.status == BillingChargeStatus.cancelled
