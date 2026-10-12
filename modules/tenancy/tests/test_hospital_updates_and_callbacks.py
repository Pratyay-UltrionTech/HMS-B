"""
Tests for Hospital PATCH updates, clinical deletion guards, and callback request handling.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.beds.entities.bed import Ward, Room  # noqa: F401
from modules.patients.entities.patient import Patient, PatientStatus
from modules.tenancy.api.tenancy_api import callback_router, router as tenancy_router
from modules.tenancy.entities.callback_request import CallbackRequest
from modules.tenancy.entities.hospital import Hospital, PlanType
from shared.auth import require_super_admin
from shared.auth.security import hash_password



@pytest.fixture
def db_session():
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
def super_admin_client(db_session):
    app = FastAPI()
    app.include_router(tenancy_router)
    app.include_router(callback_router)
    app.dependency_overrides[get_transitional_sync_session] = lambda: db_session
    app.dependency_overrides[require_super_admin] = lambda: {
        "sub": "admin@ultrion.com",
        "role": "super_admin",
    }
    return TestClient(app)


def test_update_hospital_metadata(super_admin_client, db_session):
    hosp_id = uuid4()
    hosp = Hospital(
        id=hosp_id,
        hospital_id="HMS-HOSP-UP1",
        name="Original Name Hospital",
        address="100 First St",
        phone="9876543210",
        email="up1@hospital.com",
        password_hash=hash_password("adminpass"),
        plan=PlanType.basic,
        is_active=True,
    )
    db_session.add(hosp)
    db_session.commit()

    # 1. Update name, plan, and icon_url
    res = super_admin_client.patch(
        f"/hospitals/{hosp_id}",
        json={
            "name": "Updated Apex Hospital",
            "plan": "premium",
            "icon_url": "https://cdn.example.com/logo.png",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "Updated Apex Hospital"
    assert data["plan"] == "premium"
    assert data["icon_url"] == "https://cdn.example.com/logo.png"

    # Verify DB persistence
    refreshed = db_session.query(Hospital).filter(Hospital.id == hosp_id).first()
    assert refreshed.name == "Updated Apex Hospital"
    assert refreshed.plan == PlanType.premium


def test_update_hospital_duplicate_email_conflict(super_admin_client, db_session):
    hosp1_id = uuid4()
    hosp1 = Hospital(
        id=hosp1_id,
        hospital_id="HMS-HOSP-E1",
        name="Alpha Hospital",
        address="1 Alpha Way",
        phone="9876543211",
        email="alpha@hospital.com",
        password_hash=hash_password("adminpass"),
        plan=PlanType.basic,
        is_active=True,
    )
    hosp2_id = uuid4()
    hosp2 = Hospital(
        id=hosp2_id,
        hospital_id="HMS-HOSP-E2",
        name="Beta Hospital",
        address="2 Beta Way",
        phone="9876543212",
        email="beta@hospital.com",
        password_hash=hash_password("adminpass"),
        plan=PlanType.basic,
        is_active=True,
    )
    db_session.add_all([hosp1, hosp2])
    db_session.commit()

    # Try updating hosp2's email to hosp1's email
    res = super_admin_client.patch(
        f"/hospitals/{hosp2_id}",
        json={"email": "alpha@hospital.com"},
    )
    assert res.status_code == 409
    assert "already exists" in res.json()["detail"].lower()


def test_delete_hospital_blocked_when_clinical_data_exists(super_admin_client, db_session):
    hosp_id = uuid4()
    hosp = Hospital(
        id=hosp_id,
        hospital_id="HMS-HOSP-DEL1",
        name="Active Clinical Hospital",
        address="10 Clinical Way",
        phone="9876543213",
        email="clinical@hospital.com",
        password_hash=hash_password("adminpass"),
        plan=PlanType.basic,
        is_active=True,
    )
    db_session.add(hosp)
    db_session.flush()

    # Add a patient for this hospital
    patient = Patient(
        hospital_id=hosp_id,
        uhid="UHID-DEL-001",
        first_name="Jane",
        last_name="Doe",
        name="Jane Doe",
        gender="female",
        mobile="9876543214",
        status=PatientStatus.active,
    )
    db_session.add(patient)
    db_session.commit()


    # Deletion attempt must be blocked by clinical guard
    res = super_admin_client.delete(f"/hospitals/{hosp_id}")
    assert res.status_code == 400
    assert "cannot delete hospital with existing clinical records" in res.json()["detail"].lower()

    # Verify hospital still exists
    assert db_session.query(Hospital).filter(Hospital.id == hosp_id).first() is not None


def test_delete_hospital_allowed_when_empty(super_admin_client, db_session):
    hosp_id = uuid4()
    hosp = Hospital(
        id=hosp_id,
        hospital_id="HMS-HOSP-EMPTY",
        name="Empty Hospital",
        address="10 Empty Way",
        phone="9876543215",
        email="empty@hospital.com",
        password_hash=hash_password("adminpass"),
        plan=PlanType.basic,
        is_active=True,
    )
    db_session.add(hosp)
    db_session.commit()

    res = super_admin_client.delete(f"/hospitals/{hosp_id}")
    assert res.status_code == 204
    assert db_session.query(Hospital).filter(Hospital.id == hosp_id).first() is None


def test_callback_request_submission(super_admin_client, db_session):
    payload = {
        "name": "Dr. Ramesh Verma",
        "phone": "9876543210",
        "email": "dr.verma@citycare.com",
        "location": "Mumbai Central",
        "comments": "Interested in 100-bed hospital management deployment.",
    }
    # Test via /hospitals/callback-requests
    res = super_admin_client.post("/hospitals/callback-requests", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "Dr. Ramesh Verma"
    assert data["phone"] == "9876543210"
    assert data["status"] == "pending"

    # Test via /callback-requests
    payload2 = {
        "name": "Sita Sharma",
        "phone": "9123456789",
        "email": "sita@sharma.com",
    }
    res2 = super_admin_client.post("/callback-requests", json=payload2)
    assert res2.status_code == 201
    assert res2.json()["name"] == "Sita Sharma"

    # Check database records
    count = db_session.query(CallbackRequest).count()
    assert count == 2
