"""
Comprehensive tests for target-native Laboratory module.

Tests catalogue CRUD, panel grouping, order workflows, sample collection,
result observation entry, cross-hospital tenant isolation, and billing integration.
"""

from datetime import date
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.shared.auth import (
    get_hospital_context,
    require_hospital_user,
)
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from hms_migration.modules.clinical_records.entities.clinical_record import MedicalRecord
from hms_migration.modules.laboratory.api.laboratory_api import router as laboratory_router
from hms_migration.modules.laboratory.entities.lab_entities import (
    LabItemStatus,
    LabOrder,
    LabOrderStatus,
    LabSampleType,
    LabTestCatalog,
    LabTestPanel,
)
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.tenancy.entities.hospital import Hospital


@pytest.fixture
def lab_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def lab_client(lab_db):
    app = FastAPI()
    app.include_router(laboratory_router, prefix="/api")

    test_hospital_id = uuid4()
    hospital = Hospital(
        id=test_hospital_id,
        hospital_id="HOSP-LAB-01",
        name="Apollo Diagnostic Center",
        address="789 Health Ave",
        phone="9988776655",
        email="lab@apollo.com",
        password_hash="hashed_pw",
    )
    lab_db.add(hospital)
    lab_db.commit()

    test_user = {
        "id": str(uuid4()),
        "sub": "lab_tech",
        "name": "Alex Pathologist",
        "role": "lab_technician",
        "staff_role_name": "Lab Technician",
        "hospital_id": str(test_hospital_id),
    }

    app.dependency_overrides[get_transitional_sync_session] = lambda: lab_db
    app.dependency_overrides[get_hospital_context] = lambda: test_hospital_id
    app.dependency_overrides[require_hospital_user] = lambda: test_user

    client = TestClient(app)
    client.test_hospital_id = test_hospital_id
    client.test_user = test_user
    return client


def test_lab_catalogue_crud(lab_client):
    # 1. Create test
    create_resp = lab_client.post(
        "/api/laboratory/tests",
        json={
            "test_code": "CBC_NEW",
            "test_name": "Complete Blood Count New",
            "department": "Hematology",
            "price": 400.0,
            "sample_type": "blood",
            "tat_hours": 8,
            "description": "Routine blood exam",
        },
    )
    assert create_resp.status_code == 201
    test_data = create_resp.json()
    assert test_data["test_code"] == "CBC_NEW"
    test_id = test_data["id"]

    # 2. List tests
    list_resp = lab_client.get("/api/laboratory/tests?department=Hematology")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # 3. Update test
    update_resp = lab_client.put(
        f"/api/laboratory/tests/{test_id}",
        json={"price": 450.0, "tat_hours": 6},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["price"] == 450.0
    assert update_resp.json()["tat_hours"] == 6

    # 4. Delete (deactivate) test
    del_resp = lab_client.delete(f"/api/laboratory/tests/{test_id}")
    assert del_resp.status_code == 204

    active_list = lab_client.get("/api/laboratory/tests?is_active=true").json()
    assert len(active_list) == 0


def test_seed_standard_catalogue_and_panels(lab_client):
    seed_resp = lab_client.post("/api/laboratory/catalogue/seed-standard")
    assert seed_resp.status_code == 200
    data = seed_resp.json()
    assert data["tests_added"] > 10
    assert data["panels_added"] >= 5

    # Idempotent second call
    second_seed = lab_client.post("/api/laboratory/catalogue/seed-standard").json()
    assert second_seed["tests_added"] == 0
    assert second_seed["tests_already_existed"] == data["tests_added"]

    # List panels
    panels = lab_client.get("/api/laboratory/panels").json()
    assert len(panels) >= 5
    cbc_panel = next((p for p in panels if p["panel_code"] == "CBC"), None)
    assert cbc_panel is not None
    assert len(cbc_panel["tests"]) > 0


def test_order_creation_sample_collection_results_and_billing(lab_client, lab_db):
    # 1. Create Patient
    patient = Patient(
        id=uuid4(),
        hospital_id=lab_client.test_hospital_id,
        uhid="UHID-LAB-01",
        name="Rohit Sharma",
        age=36,
        gender="male",
        mobile="9876543210",
    )
    lab_db.add(patient)
    lab_db.commit()

    # 2. Create catalogue test
    t1 = LabTestCatalog(
        hospital_id=lab_client.test_hospital_id,
        test_code="GLUCOSE",
        test_name="Fasting Blood Glucose",
        department="Biochemistry",
        price=150.0,
        sample_type=LabSampleType.blood,
        tat_hours=4,
    )
    t2 = LabTestCatalog(
        hospital_id=lab_client.test_hospital_id,
        test_code="HBA1C_TEST",
        test_name="Glycated Hemoglobin",
        department="Biochemistry",
        price=450.0,
        sample_type=LabSampleType.blood,
        tat_hours=12,
    )
    lab_db.add_all([t1, t2])
    lab_db.commit()

    # 3. Create order
    order_resp = lab_client.post(
        "/api/laboratory/orders",
        json={
            "patient_id": str(patient.id),
            "test_ids": [str(t1.id), str(t2.id)],
            "clinical_notes": "Routine diabetes check",
        },
    )
    assert order_resp.status_code == 201
    order_data = order_resp.json()
    order_id = order_data["id"]
    assert order_data["status"] == "ordered"
    assert len(order_data["items"]) == 2

    # 4. Verify Billing Charge was automatically created
    charge = (
        lab_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == lab_client.test_hospital_id,
            BillingCharge.source_id == UUID(order_id),
            BillingCharge.source_type == BillingSourceType.laboratory,
        )
        .first()
    )
    assert charge is not None
    assert charge.charge_amount == 600.0  # 150 + 450
    assert charge.status == BillingChargeStatus.pending

    # 5. Collect Sample
    collect_resp = lab_client.post(
        f"/api/laboratory/orders/{order_id}/collect-sample",
        json={
            "collected_by": "Nurse Priya",
            "sample_type": "blood",
            "collection_remarks": "Fasting sample collected at 8am",
        },
    )
    assert collect_resp.status_code == 200
    assert collect_resp.json()["status"] == "sample_collected"
    assert collect_resp.json()["collected_by"] == "Nurse Priya"

    # 6. Update item status to processing
    item_id = order_data["items"][0]["id"]
    proc_resp = lab_client.put(
        f"/api/laboratory/orders/{order_id}/items/{item_id}/status",
        json={"status": "processing"},
    )
    assert proc_resp.status_code == 200
    assert proc_resp.json()["status"] == "in_progress"

    # 7. Enter lab results and mark completed
    results_resp = lab_client.post(
        f"/api/laboratory/orders/{order_id}/results",
        json={
            "results": [
                {
                    "parameter_name": "Fasting Blood Glucose",
                    "result_value": "95",
                    "unit": "mg/dL",
                    "reference_range": "70-100",
                    "remarks": "Normal",
                },
                {
                    "parameter_name": "HbA1c",
                    "result_value": "5.4",
                    "unit": "%",
                    "reference_range": "< 5.7",
                    "remarks": "Non-diabetic range",
                },
            ],
            "mark_completed": True,
        },
    )
    assert results_resp.status_code == 200
    completed_order = results_resp.json()
    assert completed_order["status"] == "completed"
    assert len(completed_order["results"]) == 2

    # 8. Verify Report HTML generation
    report_resp = lab_client.get(f"/api/laboratory/orders/{order_id}/report")
    assert report_resp.status_code == 200
    assert "text/html" in report_resp.headers["content-type"]
    assert "Fasting Blood Glucose" in report_resp.text
    assert "95" in report_resp.text


def test_order_cancellation_and_billing_cancellation(lab_client, lab_db):
    patient = Patient(
        id=uuid4(),
        hospital_id=lab_client.test_hospital_id,
        uhid="UHID-LAB-02",
        name="Kavita Rao",
        gender="female",
        mobile="9876543211",
    )
    lab_db.add(patient)
    lab_db.commit()

    t = LabTestCatalog(
        hospital_id=lab_client.test_hospital_id,
        test_code="LIPID_TEST",
        test_name="Lipid Profile",
        department="Biochemistry",
        price=500.0,
    )
    lab_db.add(t)
    lab_db.commit()

    order_resp = lab_client.post(
        "/api/laboratory/orders",
        json={"patient_id": str(patient.id), "test_ids": [str(t.id)]},
    )
    order_id = order_resp.json()["id"]

    # Cancel order
    cancel_resp = lab_client.post(f"/api/laboratory/orders/{order_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"

    # Verify billing charge cancelled
    charge = (
        lab_db.query(BillingCharge)
        .filter(
            BillingCharge.source_id == UUID(order_id),
            BillingCharge.source_type == BillingSourceType.laboratory,
        )
        .first()
    )
    assert charge.status == BillingChargeStatus.cancelled


def test_laboratory_tenant_isolation(lab_client, lab_db):
    # Create test in hospital B
    other_hospital = Hospital(
        id=uuid4(),
        hospital_id="HOSP-LAB-02",
        name="Other Hospital",
        address="456 Other St",
        phone="1122334455",
        email="other@hospital.com",
        password_hash="hashed_pw",
    )
    lab_db.add(other_hospital)
    lab_db.commit()

    other_test = LabTestCatalog(
        hospital_id=other_hospital.id,
        test_code="SECRET_TEST",
        test_name="Secret Test",
        department="Special",
        price=999.0,
    )
    lab_db.add(other_test)
    lab_db.commit()

    # Client is scoped to test_hospital_id
    res = lab_client.get("/api/laboratory/tests")
    assert res.status_code == 200
    codes = [t["test_code"] for t in res.json()]
    assert "SECRET_TEST" not in codes
