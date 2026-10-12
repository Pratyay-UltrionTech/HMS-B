"""
Unit and integration tests for public hospital registration requests.
Tests:
- POST /api/hospital-registration-requests (happy path, validation, duplicate prevention)
- Invariant: Public submission does NOT create Hospital or admin user credentials
"""

from __future__ import annotations

import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.tenancy.api.tenancy_api import router as tenancy_router
from modules.tenancy.entities.hospital import Hospital, PlanType
from modules.tenancy.entities.hospital_registration_request import (
    HospitalRegistrationRequest,
    RegistrationStatus,
)


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
def client(db_session):
    app = FastAPI()
    app.include_router(tenancy_router)
    app.dependency_overrides[get_transitional_sync_session] = lambda: db_session
    return TestClient(app)


def test_public_registration_success(client, db_session):
    payload = {
        "hospital_name": "Metro Health Center",
        "contact_name": "Dr. Sarah Jenkins",
        "email": "sarah.jenkins@metrohealth.org",
        "phone": "9876543210",
        "address": "123 Medical Park",
        "city": "Bengaluru",
        "state": "Karnataka",
        "postal_code": "560001",
        "comments": "We are a 100-bed multispecialty center looking to onboard.",
    }
    response = client.post("/hospitals/registration-requests", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    assert "id" in data
    assert data["hospital_name"] == "Metro Health Center"
    assert data["status"] == "pending"
    assert data["request_number"].startswith("REQ-")
    assert "submitted" in data["message"].lower()

    # Verify persisted in database
    persisted = db_session.query(HospitalRegistrationRequest).filter_by(id=uuid.UUID(data["id"])).first()
    assert persisted is not None
    assert persisted.status == RegistrationStatus.pending
    assert persisted.email == "sarah.jenkins@metrohealth.org"

    # Verify no Hospital tenant was created
    hospital_count = db_session.query(Hospital).count()
    assert hospital_count == 0


def test_public_registration_validation(client):
    # Missing required hospital_name
    bad_payload = {
        "contact_name": "Dr. Sarah Jenkins",
        "email": "sarah@metrohealth.org",
        "phone": "9876543210",
        "address": "123 Medical Park",
    }
    response = client.post("/hospitals/registration-requests", json=bad_payload)
    assert response.status_code == 422

    # Invalid email
    bad_email_payload = {
        "hospital_name": "Metro Health",
        "contact_name": "Dr. Sarah",
        "email": "invalid-email",
        "phone": "9876543210",
        "address": "123 Park",
    }
    response = client.post("/hospitals/registration-requests", json=bad_email_payload)
    assert response.status_code == 422

    # Phone not 10 digits
    bad_phone_payload = {
        "hospital_name": "Metro Health",
        "contact_name": "Dr. Sarah",
        "email": "sarah@metrohealth.org",
        "phone": "123",
        "address": "123 Park",
    }
    response = client.post("/hospitals/registration-requests", json=bad_phone_payload)
    assert response.status_code == 422


def test_public_registration_duplicate_pending(client):
    payload = {
        "hospital_name": "Apollo City Center",
        "contact_name": "Dr. Sharma",
        "email": "admin@apollocity.com",
        "phone": "9876543210",
        "address": "45 MG Road",
    }
    r1 = client.post("/hospitals/registration-requests", json=payload)
    assert r1.status_code == 201

    # Second submission with same email
    r2 = client.post("/hospitals/registration-requests", json=payload)
    assert r2.status_code == 409
    assert "pending registration request" in r2.json()["detail"].lower()


def test_public_registration_conflict_with_active_hospital(client, db_session):
    # Pre-existing active hospital
    active_hospital = Hospital(
        hospital_id="HMS-ACTIVE-01",
        name="Existing Hospital",
        address="789 Ring Road",
        phone="9876543210",
        email="existing@hospital.com",
        password_hash="hashedpw",
        plan=PlanType.basic,
        is_active=True,
    )
    db_session.add(active_hospital)
    db_session.commit()

    payload = {
        "hospital_name": "Another Hospital",
        "contact_name": "Dr. Existing",
        "email": "existing@hospital.com",
        "phone": "9876543210",
        "address": "789 Ring Road",
    }
    response = client.post("/hospitals/registration-requests", json=payload)
    assert response.status_code == 409
    assert "already registered" in response.json()["detail"].lower()
