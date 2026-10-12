"""
Tests for Super Admin review, approval, idempotency, and rejection of hospital registration requests.
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
from modules.auth.entities.setup_token import AccountSetupToken
from modules.doctors.entities.doctor import HospitalUser
from modules.tenancy.api.tenancy_api import router as tenancy_router
from modules.tenancy.entities.hospital import Hospital, PlanType
from modules.tenancy.entities.hospital_registration_request import (
    HospitalRegistrationRequest,
    RegistrationStatus,
)
from shared.auth import require_super_admin


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
    app.dependency_overrides[require_super_admin] = lambda: {
        "sub": "superadmin@ultriontech.com",
        "role": "super_admin",
    }
    return TestClient(app)


def test_list_and_get_registration_requests(client, db_session):
    req = HospitalRegistrationRequest(
        request_number="REQ-2026-0001",
        hospital_name="City Central Hospital",
        contact_name="Dr. Banner",
        email="banner@citycentral.org",
        phone="9876543210",
        address="100 Hospital Way",
        status=RegistrationStatus.pending,
    )
    db_session.add(req)
    db_session.commit()

    # List requests
    res = client.get("/hospitals/registration-requests")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["hospital_name"] == "City Central Hospital"

    # Get single request
    res_single = client.get(f"/hospitals/registration-requests/{req.id}")
    assert res_single.status_code == 200
    assert res_single.json()["request_number"] == "REQ-2026-0001"


def test_approve_registration_request_success(client, db_session):
    req = HospitalRegistrationRequest(
        request_number="REQ-2026-0002",
        hospital_name="Apex Healthcare",
        contact_name="Dr. Strange",
        email="strange@apex.org",
        phone="9876543211",
        address="77 Bleecker St",
        status=RegistrationStatus.pending,
    )
    db_session.add(req)
    db_session.commit()

    approve_payload = {"plan": "premium"}
    res = client.post(f"/hospitals/registration-requests/{req.id}/approve", json=approve_payload)
    assert res.status_code == 200, res.text
    data = res.json()
    assert "hospital_id" in data
    assert data["hospital_name"] == "Apex Healthcare"
    assert "setup_token" in data
    assert len(data["setup_token"]) > 20

    # Verify Database state
    db_session.refresh(req)
    assert req.status == RegistrationStatus.approved
    assert req.reviewed_by == "superadmin@ultriontech.com"
    assert req.approved_hospital_id is not None

    # Verify Hospital row created
    hospital = db_session.query(Hospital).filter_by(id=req.approved_hospital_id).first()
    assert hospital is not None
    assert hospital.name == "Apex Healthcare"
    assert hospital.plan == PlanType.premium
    assert hospital.is_active is True

    # Verify HospitalUser admin created
    admin_user = db_session.query(HospitalUser).filter_by(hospital_id=hospital.id, email="strange@apex.org").first()
    assert admin_user is not None
    assert admin_user.name == "Dr. Strange"

    # Verify setup token persisted
    token_row = db_session.query(AccountSetupToken).filter_by(user_id=admin_user.id).first()
    assert token_row is not None
    assert token_row.token_type == "setup"
    assert token_row.used_at is None


def test_double_approval_blocked_and_idempotent(client, db_session):
    req = HospitalRegistrationRequest(
        request_number="REQ-2026-0003",
        hospital_name="St. Jude Hospital",
        contact_name="Dr. Watson",
        email="watson@stjude.org",
        phone="9876543212",
        address="221B Baker St",
        status=RegistrationStatus.pending,
    )
    db_session.add(req)
    db_session.commit()

    # First approval
    r1 = client.post(f"/hospitals/registration-requests/{req.id}/approve", json={"plan": "basic"})
    assert r1.status_code == 200

    # Second approval attempt
    r2 = client.post(f"/hospitals/registration-requests/{req.id}/approve", json={"plan": "basic"})
    assert r2.status_code == 409
    assert "already been approved" in r2.json()["detail"].lower()

    # Verify only 1 hospital was created
    count = db_session.query(Hospital).filter(Hospital.email == "watson@stjude.org").count()
    assert count == 1


def test_reject_registration_request(client, db_session):
    req = HospitalRegistrationRequest(
        request_number="REQ-2026-0004",
        hospital_name="Ineligible Clinic",
        contact_name="Bob",
        email="bob@clinic.com",
        phone="9876543213",
        address="Unknown",
        status=RegistrationStatus.pending,
    )
    db_session.add(req)
    db_session.commit()

    # Rejection without reason fails validation
    r_bad = client.post(f"/hospitals/registration-requests/{req.id}/reject", json={"rejection_reason": " "})
    assert r_bad.status_code == 422

    # Valid rejection
    r_ok = client.post(
        f"/hospitals/registration-requests/{req.id}/reject",
        json={"rejection_reason": "Facility does not meet minimum accreditation requirements."},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["status"] == "rejected"
    assert r_ok.json()["rejection_reason"] == "Facility does not meet minimum accreditation requirements."

    # Cannot approve a rejected request
    r_approve = client.post(f"/hospitals/registration-requests/{req.id}/approve", json={"plan": "basic"})
    assert r_approve.status_code == 409
    assert "cannot approve a rejected" in r_approve.json()["detail"].lower()
