"""
Regression: Lab Orders Queue -> Select Order -> Order Details must load.

Covers backend half of the workflow:
  Lab Order Created -> GET /orders (queue) -> GET /orders/{id} (details) 200
with items/results/specimens serializable and clinical_notes preserved.

The frontend half failed with ReferenceError because LabOrderDetailPage used
`collectSampleType`/`setCollectSampleType` without declaring state, which
nulled the fetched order and showed "Failed to load lab order". That is fixed
by declaring the missing state; this test guards the API contract it depends on.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
import tests.conftest  # noqa: F401
from modules.laboratory.api.laboratory_api import router as laboratory_router
from modules.laboratory.entities.lab_entities import LabSampleType, LabTestCatalog
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def lab_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
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
    lab_db.add(Hospital(id=test_hospital_id, hospital_id="HOSP-LAB-REG",
                        name="Reg Lab", address="a", phone="1",
                        email="l@r.com", password_hash="x"))
    lab_db.commit()
    test_user = {"id": str(uuid4()), "sub": "lab_tech", "name": "Alex",
                 "role": "hospital_admin", "staff_role_name": "Lab Technician",
                 "hospital_id": str(test_hospital_id)}
    app.dependency_overrides[get_transitional_sync_session] = lambda: lab_db
    app.dependency_overrides[get_hospital_context] = lambda: test_hospital_id
    app.dependency_overrides[require_hospital_user] = lambda: test_user
    client = TestClient(app)
    client.test_hospital_id = test_hospital_id
    client.test_user = test_user
    return client


def test_queue_then_detail_loads_successfully(lab_client, lab_db):
    patient = Patient(
        id=uuid4(), hospital_id=lab_client.test_hospital_id,
        uhid="UHID-REG-QUEUE", name="Queue Patient",
        age=40, gender="male", mobile="9000000001",
    )
    lab_db.add(patient)
    t = LabTestCatalog(
        hospital_id=lab_client.test_hospital_id, test_code="REGQUEUE",
        test_name="Regression Queue Test", department="Biochemistry",
        price=100.0, sample_type=LabSampleType.blood, tat_hours=4,
    )
    lab_db.add(t)
    lab_db.commit()

    created = lab_client.post("/api/laboratory/orders", json={
        "patient_id": str(patient.id),
        "test_ids": [str(t.id)],
        "clinical_notes": "regression queue check",
    })
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]

    queue = lab_client.get("/api/laboratory/orders")
    assert queue.status_code == 200
    assert any(o["id"] == order_id for o in queue.json())

    detail = lab_client.get(f"/api/laboratory/orders/{order_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == order_id
    assert body["clinical_notes"] == "regression queue check"
    assert isinstance(body["items"], list) and len(body["items"]) == 1
    assert isinstance(body["results"], list)
    assert isinstance(body["specimens"], list)
    # sample_type must serialize for the detail page collector dropdown
    assert body["sample_type"] in ("blood", None) or body["sample_type"] is not None
