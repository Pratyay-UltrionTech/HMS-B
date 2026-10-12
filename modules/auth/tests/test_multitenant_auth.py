"""
Tests for multi-tenant login qualification and password setup/reset tokens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.auth.api.auth_api import router as auth_router
from modules.beds.entities.bed import Ward, Room  # noqa: F401
from modules.auth.entities.setup_token import AccountSetupToken
from modules.doctors.entities.doctor import HospitalUser, HospitalUserRole, StaffRole
from modules.tenancy.entities.hospital import Hospital, PlanType
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
def client(db_session):
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_transitional_sync_session] = lambda: db_session
    return TestClient(app)


def test_multitenant_login_ambiguity_and_qualification(client, db_session):
    # 1. Hospital A with doctor
    hosp_a_id = uuid4()
    hosp_a = Hospital(
        id=hosp_a_id,
        hospital_id="HMS-HOSP-A",
        name="Metro Hospital North",
        address="10 North Way",
        phone="9876543201",
        email="contact@hospital-a.com",
        password_hash=hash_password("adminpassA"),
        plan=PlanType.basic,
        is_active=True,
    )
    role_a = StaffRole(hospital_id=hosp_a_id, name="Doctor", is_active=True)
    db_session.add_all([hosp_a, role_a])
    db_session.flush()

    user_a = HospitalUser(
        hospital_id=hosp_a_id,
        role_id=role_a.id,
        name="Dr. Smith",
        email="dr.smith@example.com",
        phone="9876543201",
        password_hash=hash_password("PasswordA123!"),
        is_active=True,
        token_version=1,
    )
    db_session.add(user_a)
    db_session.flush()
    db_session.add(HospitalUserRole(hospital_id=hosp_a_id, user_id=user_a.id, role_id=role_a.id, is_primary=True))

    # 2. Hospital B with doctor with same email
    hosp_b_id = uuid4()
    hosp_b = Hospital(
        id=hosp_b_id,
        hospital_id="HMS-HOSP-B",
        name="Metro Hospital South",
        address="20 South Way",
        phone="9876543202",
        email="contact@hospital-b.com",
        password_hash=hash_password("adminpassB"),
        plan=PlanType.basic,
        is_active=True,
    )
    role_b = StaffRole(hospital_id=hosp_b_id, name="Doctor", is_active=True)
    db_session.add_all([hosp_b, role_b])
    db_session.flush()

    user_b = HospitalUser(
        hospital_id=hosp_b_id,
        role_id=role_b.id,
        name="Dr. Smith",
        email="dr.smith@example.com",
        phone="9876543202",
        password_hash=hash_password("PasswordB456!"),
        is_active=True,
        token_version=1,
    )
    db_session.add(user_b)
    db_session.flush()
    db_session.add(HospitalUserRole(hospital_id=hosp_b_id, user_id=user_b.id, role_id=role_b.id, is_primary=True))
    db_session.commit()

    # 3. Security check: Wrong password MUST NOT leak any hospital memberships
    res_wrong = client.post(
        "/auth/login",
        json={"email": "dr.smith@example.com", "password": "WrongPassword999!"},
    )
    assert res_wrong.status_code == 401
    assert "matching_hospitals" not in res_wrong.json()

    # 4. Security check: Different passwords across hospitals:
    # Entering PasswordA123! without hospital_id resolves Hospital A directly and MUST NOT reveal Hospital B
    res_direct_a = client.post(
        "/auth/login",
        json={"email": "dr.smith@example.com", "password": "PasswordA123!"},
    )
    assert res_direct_a.status_code == 200
    assert res_direct_a.json()["hospital_id"] == "HMS-HOSP-A"

    # Entering PasswordB456! without hospital_id resolves Hospital B directly and MUST NOT reveal Hospital A
    res_direct_b = client.post(
        "/auth/login",
        json={"email": "dr.smith@example.com", "password": "PasswordB456!"},
    )
    assert res_direct_b.status_code == 200
    assert res_direct_b.json()["hospital_id"] == "HMS-HOSP-B"

    # 5. When accounts share the SAME password across multiple hospitals:
    user_a.password_hash = hash_password("SharedPassword789!")
    user_b.password_hash = hash_password("SharedPassword789!")
    db_session.commit()

    res_ambiguous = client.post(
        "/auth/login",
        json={"email": "dr.smith@example.com", "password": "SharedPassword789!"},
    )
    assert res_ambiguous.status_code == 409
    data = res_ambiguous.json()
    assert "multiple hospital accounts" in data["detail"].lower()
    assert "matching_hospitals" in data
    assert len(data["matching_hospitals"]) == 2
    # Ensure role is NOT leaked in disambiguation response
    for m in data["matching_hospitals"]:
        assert "hospital_id" in m
        assert "hospital_name" in m
        assert "role" not in m

    # 6. Disambiguation resolved by providing hospital_id
    res_resolved_a = client.post(
        "/auth/login",
        json={
            "email": "dr.smith@example.com",
            "password": "SharedPassword789!",
            "hospital_id": "HMS-HOSP-A",
        },
    )
    assert res_resolved_a.status_code == 200
    assert res_resolved_a.json()["hospital_id"] == "HMS-HOSP-A"



def test_single_tenant_login_without_hospital_id_works_transparently(client, db_session):
    hosp_id = uuid4()
    hosp = Hospital(
        id=hosp_id,
        hospital_id="HMS-HOSP-SINGLE",
        name="Single Hospital",
        address="1 Single Ave",
        phone="9876543203",
        email="contact@single.com",
        password_hash=hash_password("adminpass"),
        plan=PlanType.basic,
        is_active=True,
    )
    role = StaffRole(hospital_id=hosp_id, name="Nurse", is_active=True)
    db_session.add_all([hosp, role])
    db_session.flush()

    user = HospitalUser(
        hospital_id=hosp_id,
        role_id=role.id,
        name="Nurse Joy",
        email="joy@single.com",
        phone="9876543203",
        password_hash=hash_password("JoyNurse123!"),
        is_active=True,
        token_version=1,
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(HospitalUserRole(hospital_id=hosp_id, user_id=user.id, role_id=role.id, is_primary=True))
    db_session.commit()

    # Login without hospital_id resolves single tenant automatically
    res = client.post(
        "/auth/login",
        json={"email": "joy@single.com", "password": "JoyNurse123!"},
    )
    assert res.status_code == 200
    assert res.json()["hospital_id"] == "HMS-HOSP-SINGLE"


def test_password_setup_token_flow(client, db_session):
    hosp_id = uuid4()
    hosp = Hospital(
        id=hosp_id,
        hospital_id="HMS-HOSP-SETUP",
        name="Setup Hospital",
        address="1 Setup Rd",
        phone="9876543204",
        email="admin@setup.com",
        password_hash=hash_password("initialunusedhash"),
        plan=PlanType.basic,
        is_active=True,
    )
    role = StaffRole(hospital_id=hosp_id, name="Hospital Admin", is_active=True)
    db_session.add_all([hosp, role])
    db_session.flush()

    admin_user = HospitalUser(
        hospital_id=hosp_id,
        role_id=role.id,
        name="Setup Admin",
        email="admin@setup.com",
        phone="9876543204",
        password_hash=hash_password("initialunusedhash"),
        is_active=True,
        token_version=1,
    )
    db_session.add(admin_user)
    db_session.flush()
    db_session.add(HospitalUserRole(hospital_id=hosp_id, user_id=admin_user.id, role_id=role.id, is_primary=True))

    raw_token = "secure-setup-token-xyz-123456789012345"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    setup_token = AccountSetupToken(
        token_hash=token_hash,
        user_id=admin_user.id,
        hospital_id=hosp_id,
        email="admin@setup.com",
        token_type="setup",
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add(setup_token)
    db_session.commit()

    # 1. Setting password with token succeeds
    res_setup = client.post(
        "/auth/setup-password",
        json={"token": raw_token, "password": "NewStrongPassword123!"},
    )
    assert res_setup.status_code == 200
    assert "successfully" in res_setup.json()["message"].lower()

    # 2. Reusing the token fails
    res_reuse = client.post(
        "/auth/setup-password",
        json={"token": raw_token, "password": "AnotherPassword123!"},
    )
    assert res_reuse.status_code == 400
    assert "invalid or already used" in res_reuse.json()["detail"].lower()

    # 3. Can now authenticate with the new password
    res_login = client.post(
        "/auth/login",
        json={"email": "admin@setup.com", "password": "NewStrongPassword123!"},
    )
    assert res_login.status_code == 200
    assert res_login.json()["hospital_id"] == "HMS-HOSP-SETUP"


def test_expired_setup_token_rejected(client, db_session):
    hosp_id = uuid4()
    hosp = Hospital(
        id=hosp_id,
        hospital_id="HMS-HOSP-EXPIRED",
        name="Expired Token Hospital",
        address="1 Expired Rd",
        phone="9876543205",
        email="admin@expired.com",
        password_hash="fake",
        plan=PlanType.basic,
        is_active=True,
    )
    db_session.add(hosp)
    db_session.commit()

    raw_token = "expired-token-1234567890"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    setup_token = AccountSetupToken(
        token_hash=token_hash,
        user_id=None,
        hospital_id=hosp_id,
        email="admin@expired.com",
        token_type="setup",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),  # Expired
    )
    db_session.add(setup_token)
    db_session.commit()

    res = client.post(
        "/auth/setup-password",
        json={"token": raw_token, "password": "NewStrongPassword123!"},
    )
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower()
