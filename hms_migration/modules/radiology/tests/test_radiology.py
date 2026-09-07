"""
Unit and integration tests for Radiology domain.
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

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from hms_migration.modules.clinical_records.entities.clinical_record import MedicalRecord
from hms_migration.modules.doctors.entities.doctor import HospitalUser, StaffRole
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.radiology.api.radiology_api import router as radiology_router
from hms_migration.modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
)
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def rad_db():
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
def rad_client(rad_db):
    app = FastAPI()
    app.include_router(radiology_router, prefix="/api")

    hospital_id = uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id="HOSP-RAD-01",
        name="Apex Diagnostics Hospital",
        address="123 Health Ave",
        phone="9876543210",
        email="apex@hospital.com",
        password_hash="hashed_pw",
        is_active=True,
    )
    rad_db.add(hospital)

    role = StaffRole(
        id=uuid4(),
        hospital_id=hospital_id,
        name="doctor",
        description="Doctor role",
        is_active=True,
    )
    rad_db.add(role)

    user_id = uuid4()
    user = HospitalUser(
        id=user_id,
        hospital_id=hospital_id,
        role_id=role.id,
        name="Dr. Rad Staff",
        phone="9876543210",
        email="radstaff@apex.com",
        password_hash="fakehash",
        is_active=True,
    )
    rad_db.add(user)
    rad_db.commit()

    def override_get_db():
        yield rad_db

    def override_get_hospital_context():
        return hospital_id

    def override_require_hospital_user():
        return {
            "sub": str(user_id),
            "hospital_id": str(hospital_id),
            "name": "Dr. Rad Staff",
            "role": "doctor",
            "staff_role_name": "Senior Radiologist",
        }

    app.dependency_overrides[get_transitional_sync_session] = override_get_db
    app.dependency_overrides[get_hospital_context] = override_get_hospital_context
    app.dependency_overrides[require_hospital_user] = override_require_hospital_user

    client = TestClient(app)
    client.test_hospital_id = hospital_id
    client.test_user_id = user_id
    return client


def test_radiology_catalogue_seed_and_crud(rad_client, rad_db):
    # 1. Seed standard catalogue
    resp = rad_client.post("/api/radiology/catalogue/seed-standard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["added"] >= 15
    assert data["template_pack"] == "standard"

    # Idempotent re-seed
    resp2 = rad_client.post("/api/radiology/catalogue/seed-standard")
    assert resp2.status_code == 200
    assert resp2.json()["added"] == 0
    assert resp2.json()["already_existed"] == data["added"]

    # 2. List scans
    scans_resp = rad_client.get("/api/radiology/scans?active_only=true")
    assert scans_resp.status_code == 200
    scans = scans_resp.json()
    assert len(scans) >= 15

    # Filter by search
    xray_resp = rad_client.get("/api/radiology/scans?search=Chest")
    assert xray_resp.status_code == 200
    assert any(s["scan_code"] == "XRCHEST" for s in xray_resp.json())

    # 3. Create custom scan
    new_scan = {
        "scan_code": "PET-CT-01",
        "scan_name": "Full Body PET-CT",
        "category": "Nuclear Medicine",
        "department": "Radiology",
        "price": 18000.0,
        "duration_minutes": 60,
        "description": "Oncological staging scan",
        "is_active": True,
    }
    c_resp = rad_client.post("/api/radiology/scans", json=new_scan)
    assert c_resp.status_code == 201
    created = c_resp.json()
    assert created["scan_code"] == "PET-CT-01"

    # Duplicate code conflict
    c_dup = rad_client.post("/api/radiology/scans", json=new_scan)
    assert c_dup.status_code == 409

    # 4. Update scan
    up_resp = rad_client.put(
        f"/api/radiology/scans/{created['id']}",
        json={"price": 19500.0, "description": "Updated staging scan"},
    )
    assert up_resp.status_code == 200
    assert up_resp.json()["price"] == 19500.0

    # 5. Delete scan
    del_resp = rad_client.delete(f"/api/radiology/scans/{created['id']}")
    assert del_resp.status_code == 204


def test_radiology_order_full_lifecycle(rad_client, rad_db):
    h_id = rad_client.test_hospital_id

    # Create patient
    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-RAD-001",
        name="Vikram Seth",
        mobile="9877777777",
        age=52,
        gender="male",
    )
    rad_db.add(patient)

    # Seed scan
    scan = RadiologyScanCatalog(
        id=uuid4(),
        hospital_id=h_id,
        scan_code="XRCHEST",
        scan_name="X-Ray Chest",
        category="X-Ray",
        price=500.0,
        duration_minutes=15,
        is_active=True,
    )
    rad_db.add(scan)
    rad_db.commit()

    # 1. Create order
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(rad_client.test_user_id),
        "scan_ids": [str(scan.id)],
        "clinical_notes": "Persistent cough and shortness of breath",
    }
    o_resp = rad_client.post("/api/radiology/orders", json=payload)
    assert o_resp.status_code == 201
    orders = o_resp.json()
    assert len(orders) == 1
    order = orders[0]
    order_id = order["id"]
    assert order["status"] == "ordered"
    assert order["scan_code"] == "XRCHEST"

    # Verify billing charge auto-created on target billing!
    charge = (
        rad_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.source_id == UUID(order_id),
            BillingCharge.source_type == BillingSourceType.radiology,
        )
        .first()
    )
    assert charge is not None
    assert charge.charge_amount == 500.0
    assert charge.status == BillingChargeStatus.pending

    # 2. Schedule order
    sch_resp = rad_client.post(
        f"/api/radiology/orders/{order_id}/schedule",
        json={
            "scheduled_at": datetime.now(timezone.utc).isoformat(),
            "machine": "Siemens X-Ray Room 2",
            "technician_name": "Ravi Kumar",
        },
    )
    assert sch_resp.status_code == 200
    assert sch_resp.json()["status"] == "scheduled"
    assert sch_resp.json()["machine"] == "Siemens X-Ray Room 2"

    # 3. Start scan
    st_resp = rad_client.post(f"/api/radiology/orders/{order_id}/start")
    assert st_resp.status_code == 200
    assert st_resp.json()["status"] == "in_progress"
    assert st_resp.json()["started_at"] is not None

    # 4. Complete scan
    cs_resp = rad_client.post(f"/api/radiology/orders/{order_id}/complete-scan")
    assert cs_resp.status_code == 200
    assert cs_resp.json()["status"] == "completed"

    # 5. Upload report with scan image
    sample_img = "data:image/png;base64," + base64.b64encode(b"dummy_png_bytes").decode("ascii")
    sample_pdf = "data:application/pdf;base64," + base64.b64encode(b"dummy_pdf_bytes").decode("ascii")
    rep_resp = rad_client.post(
        f"/api/radiology/orders/{order_id}/report",
        json={
            "findings": "Normal broncho-vascular markings. No focal consolidation.",
            "impression": "Normal chest radiograph.",
            "remarks": "Clinical correlation recommended.",
            "image_file_name": "chest_xray.png",
            "image_file_data": sample_img,
            "report_file_name": "chest_report.pdf",
            "report_file_data": sample_pdf,
        },
    )
    assert rep_resp.status_code == 200
    rep_data = rep_resp.json()
    assert rep_data["findings"] == "Normal broncho-vascular markings. No focal consolidation."
    assert rep_data["has_image_file"] is True
    assert rep_data["has_report_file"] is True

    # Check medical records sync
    med_rec = (
        rad_db.query(MedicalRecord)
        .filter(
            MedicalRecord.hospital_id == h_id,
            MedicalRecord.radiology_order_id == UUID(order_id),
        )
        .first()
    )
    assert med_rec is not None
    assert "Normal chest radiograph" in med_rec.notes

    # 6. HTML Report viewing
    html_resp = rad_client.get(f"/api/radiology/orders/{order_id}/report-view")
    assert html_resp.status_code == 200
    assert "text/html" in html_resp.headers["content-type"]
    assert "Normal chest radiograph" in html_resp.text

    # 7. File streaming
    file_resp = rad_client.get(f"/api/radiology/orders/{order_id}/file/image")
    assert file_resp.status_code == 200
    assert file_resp.content == b"dummy_png_bytes"


def test_radiology_cancellation_and_billing_sync(rad_client, rad_db):
    h_id = rad_client.test_hospital_id

    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-RAD-002",
        name="Pooja Sharma",
        mobile="9822222222",
    )
    rad_db.add(patient)

    scan = RadiologyScanCatalog(
        id=uuid4(),
        hospital_id=h_id,
        scan_code="USGABD",
        scan_name="Ultrasound Abdomen",
        category="Ultrasound",
        price=900.0,
        is_active=True,
    )
    rad_db.add(scan)
    rad_db.commit()

    # Create order
    o_resp = rad_client.post(
        "/api/radiology/orders",
        json={
            "patient_id": str(patient.id),
            "scan_ids": [str(scan.id)],
        },
    )
    assert o_resp.status_code == 201
    order_id = o_resp.json()[0]["id"]

    # Charge was created
    charge = (
        rad_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.source_id == UUID(order_id),
        )
        .first()
    )
    assert charge.status == BillingChargeStatus.pending

    # Cancel order
    c_resp = rad_client.post(f"/api/radiology/orders/{order_id}/cancel")
    assert c_resp.status_code == 200
    assert c_resp.json()["status"] == "cancelled"

    # Verify charge was cancelled
    rad_db.refresh(charge)
    assert charge.status == BillingChargeStatus.cancelled


def test_radiology_dashboard_and_tenant_isolation(rad_client, rad_db):
    h_id = rad_client.test_hospital_id

    # Check dashboard initially
    dash = rad_client.get("/api/radiology/dashboard")
    assert dash.status_code == 200
    assert "todays_orders" in dash.json()

    # Create order for another hospital
    other_hosp_id = uuid4()
    other_patient = Patient(
        id=uuid4(),
        hospital_id=other_hosp_id,
        uhid="UHID-OTHER-01",
        name="Other Patient",
        mobile="9800000000",
    )
    other_order = RadiologyOrder(
        id=uuid4(),
        hospital_id=other_hosp_id,
        order_no="RAD-OTHER-01",
        patient_id=other_patient.id,
        scan_code="CTBRAIN",
        scan_name="CT Brain",
        price=3500.0,
        status=RadiologyOrderStatus.ordered,
    )
    rad_db.add(other_patient)
    rad_db.add(other_order)
    rad_db.commit()

    # Other hospital order should not appear in rad_client orders
    orders_resp = rad_client.get("/api/radiology/orders")
    assert orders_resp.status_code == 200
    assert not any(o["order_no"] == "RAD-OTHER-01" for o in orders_resp.json())

    # Direct lookup of other hospital order should be 404
    lookup_resp = rad_client.get(f"/api/radiology/orders/{other_order.id}")
    assert lookup_resp.status_code == 404
