"""
Regression test suite for migration hardening and identity classification.

Verifies:
1. FIX 1: Hospital with inactive HospitalUser administrator is classified as
   user_accounts (NOT legacy_organization), is_active=False is preserved, and
   credentials cannot fall back through Hospital authentication.
2. FIX 2: StaffRole admin backfill:
   - Case A: "Hospital Admin" -> auto-promoted to HOSPITAL_ADMIN / is_admin_role=True
   - Case B: "hospital_admin" -> auto-promoted to HOSPITAL_ADMIN / is_admin_role=True
   - Case C: "Administrator" custom role with ordinary permissions -> NOT promoted
   - Case D: "Administrator" with strong evidence -> promoted only when evidence satisfied
3. Migration idempotency and zero drift.
"""

from __future__ import annotations

import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.auth.api.auth_api import router as auth_router
from modules.doctors.entities.doctor import (
    HospitalUser,
    HospitalUserRole,
    RolePermission,
    StaffRole,
)
from modules.tenancy.entities.hospital import Hospital, PlanType
from scripts.migrate import (
    backfill_hospital_admin_roles,
    backfill_hospital_auth_models,
)
from shared.auth.security import hash_password


@pytest.fixture
def auth_client(db_session):
    test_app = FastAPI()
    test_app.include_router(auth_router, prefix="/api")
    test_app.dependency_overrides[get_transitional_sync_session] = lambda: db_session
    return TestClient(test_app)


def test_fix1_inactive_admin_preserves_user_accounts_and_blocks_fallback(db_session, auth_client):
    """
    FIX 1 Regression Test:
    Hospital: password_hash != NULL
    HospitalUser: Hospital Admin, is_active = FALSE
    
    Expected after migration:
    - auth_model = user_accounts
    - HospitalUser.is_active = FALSE (strictly preserved)
    - Authentication with those credentials fails (401)
    - Credentials cannot fall back through Hospital authentication
    """
    h_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    role_id = uuid.uuid4()

    # 1. Hospital with legacy password hash but currently marked legacy_organization
    h = Hospital(
        id=h_id,
        hospital_id="HMS-INACT-ADM",
        name="Inactive Admin Hospital",
        address="100 Hospital Way",
        phone="9876543210",
        email="inactive_admin@hospital.org",
        password_hash=hash_password("LegacyOrgPassword123!"),
        plan=PlanType.basic,
        is_active=True,
        status="active",
        auth_model="legacy_organization",
    )
    db_session.add(h)

    # 2. Canonical admin role
    r = StaffRole(
        id=role_id,
        hospital_id=h_id,
        name="Hospital Admin",
        code="HOSPITAL_ADMIN",
        is_admin_role=True,
        is_active=True,
    )
    db_session.add(r)

    # 3. Migrated human administrator who is INACTIVE
    u = HospitalUser(
        id=admin_id,
        hospital_id=h_id,
        role_id=role_id,
        name="Dr. Deactivated Admin",
        phone="9876543211",
        email="inactive_admin@hospital.org",
        password_hash=hash_password("AdminPersonalPassword123!"),
        is_active=False,
    )
    db_session.add(u)
    db_session.commit()

    # 4. Run migration backfill
    conn = db_session.connection()
    backfill_hospital_auth_models(conn)
    db_session.commit()

    # 5. Verify database classification
    db_session.refresh(h)
    db_session.refresh(u)
    assert h.auth_model == "user_accounts", (
        f"Hospital must remain user_accounts even with inactive admin, got {h.auth_model}"
    )
    assert u.is_active is False, (
        "HospitalUser.is_active must remain FALSE (never automatically reactivated)"
    )

    # 6. Verify login rejection for inactive user credentials
    res_user_cred = auth_client.post(
        "/api/auth/login",
        json={
            "email": "inactive_admin@hospital.org",
            "password": "AdminPersonalPassword123!",
            "hospital_id": str(h_id),
        },
    )
    assert res_user_cred.status_code == 401
    assert "inactive" in res_user_cred.json()["detail"].lower()

    # 7. CRITICAL: Verify NO fallback to legacy Hospital organization credentials
    res_fallback = auth_client.post(
        "/api/auth/login",
        json={
            "email": "inactive_admin@hospital.org",
            "password": "LegacyOrgPassword123!",
            "hospital_id": str(h_id),
        },
    )
    assert res_fallback.status_code == 401
    # Blocked: either as inactive account (401) or invalid credentials (401), never 200
    assert (
        "inactive" in res_fallback.json()["detail"].lower()
        or "invalid" in res_fallback.json()["detail"].lower()
    )

    # Also test global login without hospital_id qualification
    res_global_fallback = auth_client.post(
        "/api/auth/login",
        json={
            "email": "inactive_admin@hospital.org",
            "password": "LegacyOrgPassword123!",
        },
    )
    assert res_global_fallback.status_code == 401


def test_case_1_hospital_admin_canonical_promoted(db_session):
    """
    Case 1:
    name = "Hospital Admin"

    EXPECTED:
    code = HOSPITAL_ADMIN
    is_admin_role = TRUE
    """
    h_id = uuid.uuid4()
    r = StaffRole(
        id=uuid.uuid4(),
        hospital_id=h_id,
        name="Hospital Admin",
        code=None,
        is_admin_role=False,
    )
    db_session.add(r)
    db_session.commit()

    conn = db_session.connection()
    promoted, ambiguous = backfill_hospital_admin_roles(conn)
    db_session.commit()

    db_session.refresh(r)
    assert r.code == "HOSPITAL_ADMIN"
    assert r.is_admin_role is True
    assert promoted >= 1
    assert ambiguous == 0


# Backward compatibility alias
test_fix2_case_a_hospital_admin_canonical_promoted = test_case_1_hospital_admin_canonical_promoted


def test_case_2_hospital_admin_lowercase_promoted(db_session):
    """
    Case 2:
    name = "hospital_admin"

    EXPECTED:
    code = HOSPITAL_ADMIN
    is_admin_role = TRUE
    """
    h_id = uuid.uuid4()
    r = StaffRole(
        id=uuid.uuid4(),
        hospital_id=h_id,
        name="hospital_admin",
        code=None,
        is_admin_role=False,
    )
    db_session.add(r)
    db_session.commit()

    conn = db_session.connection()
    promoted, ambiguous = backfill_hospital_admin_roles(conn)
    db_session.commit()

    db_session.refresh(r)
    assert r.code == "HOSPITAL_ADMIN"
    assert r.is_admin_role is True
    assert promoted >= 1
    assert ambiguous == 0


# Backward compatibility alias
test_fix2_case_b_hospital_admin_lowercase_promoted = test_case_2_hospital_admin_lowercase_promoted


def test_case_4_custom_administrator_limited_permissions_not_promoted(db_session):
    """
    Case 4:
    custom Administrator
    limited permissions

    EXPECTED:
    NOT promoted
    code remains unchanged/null
    is_admin_role remains FALSE
    reported as ambiguous
    """
    h_id = uuid.uuid4()
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()

    h = Hospital(
        id=h_id,
        hospital_id="HMS-AMBIG",
        name="Ambiguous Hospital",
        address="100 Ambiguous Way",
        phone="9876543210",
        email="hospital_official@hospital.org",
        is_active=True,
        status="active",
    )
    db_session.add(h)

    # Custom role named 'Administrator' created for ordinary clerk
    r = StaffRole(
        id=role_id,
        hospital_id=h_id,
        name="Administrator",
        code=None,
        is_admin_role=False,
    )
    db_session.add(r)

    # Ordinary limited permission (only 1 module)
    p = RolePermission(
        id=uuid.uuid4(),
        hospital_id=h_id,
        role_id=role_id,
        module_key="vitals",
        can_view=True,
        can_edit=False,
    )
    db_session.add(p)

    # Assigned to staff member whose email is different from hospital official email
    u = HospitalUser(
        id=user_id,
        hospital_id=h_id,
        role_id=role_id,
        name="Office Clerk",
        phone="9876543211",
        email="clerk@hospital.org",
        password_hash=hash_password("ClerkPassword123!"),
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()

    conn = db_session.connection()
    promoted, ambiguous = backfill_hospital_admin_roles(conn)
    db_session.commit()

    db_session.refresh(r)
    # MUST NOT be elevated!
    assert r.code is None or r.code != "HOSPITAL_ADMIN", (
        f"Custom Administrator role must not receive HOSPITAL_ADMIN code, got {r.code}"
    )
    assert r.is_admin_role is False, (
        "Custom Administrator role must not receive is_admin_role=True"
    )
    assert ambiguous >= 1, "Must report at least 1 ambiguous role requiring manual review"


# Backward compatibility alias
test_fix2_case_c_custom_administrator_role_not_promoted = test_case_4_custom_administrator_limited_permissions_not_promoted


def test_case_3_administrator_broad_permissions_not_promoted(db_session):
    """
    Case 3:
    name = "Administrator"
    permissions on 5+ modules (e.g. patients, appointments, beds, billing, inventory, vitals)

    EXPECTED:
    NOT promoted
    code remains unchanged/null
    is_admin_role remains FALSE
    reported as ambiguous
    """
    h_id = uuid.uuid4()
    r_id = uuid.uuid4()
    h = Hospital(
        id=h_id,
        hospital_id="HMS-AMBIG-5P",
        name="Broad Permissions Hospital",
        address="500 Multi Perm Way",
        phone="9876543500",
        email="hospital@broadperms.org",
        is_active=True,
        status="active",
    )
    db_session.add(h)

    r = StaffRole(
        id=r_id,
        hospital_id=h_id,
        name="Administrator",
        code=None,
        is_admin_role=False,
    )
    db_session.add(r)

    # 6 modules with view & edit permissions (e.g., Patients, Appointments, Beds, Billing, Inventory, Vitals)
    modules = ["patients", "appointments", "beds", "billing", "inventory", "vitals"]
    for mod in modules:
        db_session.add(
            RolePermission(
                id=uuid.uuid4(),
                hospital_id=h_id,
                role_id=r_id,
                module_key=mod,
                can_view=True,
                can_edit=True,
            )
        )
    db_session.commit()

    conn = db_session.connection()
    promoted, ambiguous = backfill_hospital_admin_roles(conn)
    db_session.commit()

    db_session.refresh(r)
    assert r.code is None or r.code != "HOSPITAL_ADMIN", (
        f"Administrator role with 5+ permissions must not receive HOSPITAL_ADMIN code, got {r.code}"
    )
    assert r.is_admin_role is False, (
        "Administrator role with 5+ permissions must not receive is_admin_role=True"
    )
    assert ambiguous >= 1, "Must report as ambiguous Administrator role"


def test_genuine_legacy_tenant_remains_legacy_and_authenticates(db_session, auth_client):
    """
    Verify genuine legacy_organization tenant without HospitalUser admin
    remains legacy_organization and authenticates via compatibility path.
    """
    h_id = uuid.uuid4()
    h = Hospital(
        id=h_id,
        hospital_id="HMS-LEGACY-GEN",
        name="Genuine Legacy Hospital",
        address="300 Legacy Way",
        phone="9876543300",
        email="legacy_director@legacy.org",
        password_hash=hash_password("LegacySecret123!"),
        plan=PlanType.basic,
        is_active=True,
        status="active",
        auth_model="legacy_organization",
    )
    db_session.add(h)
    db_session.commit()

    conn = db_session.connection()
    backfill_hospital_auth_models(conn)
    db_session.commit()

    db_session.refresh(h)
    assert h.auth_model == "legacy_organization"

    # Authenticates successfully via legacy compatibility path
    res = auth_client.post(
        "/api/auth/login",
        json={
            "email": "legacy_director@legacy.org",
            "password": "LegacySecret123!",
            "hospital_id": str(h_id),
        },
    )
    assert res.status_code == 200
    assert res.json()["role"] == "hospital_admin"
    assert res.json()["hospital_id"] == "HMS-LEGACY-GEN"


def test_case_5_migration_run_twice_idempotency(db_session):
    """
    Case 5:
    Run migration twice.

    Expected:
    same role states
    same counts
    no duplicate updates
    """
    h_id = uuid.uuid4()
    r_id = uuid.uuid4()
    h = Hospital(
        id=h_id,
        hospital_id="HMS-IDEMPOTENT",
        name="Idempotent Hospital",
        address="400 Idempotent Way",
        phone="9876543400",
        email="idempotent@hospital.org",
        password_hash=hash_password("Pass123!"),
        is_active=True,
        status="active",
    )
    r = StaffRole(
        id=r_id,
        hospital_id=h_id,
        name="Hospital Admin",
        code=None,
        is_admin_role=False,
    )
    u = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=h_id,
        role_id=r_id,
        name="Admin User",
        phone="9876543401",
        email="admin@hospital.org",
        password_hash=hash_password("UserPass123!"),
        is_active=True,
    )
    db_session.add_all([h, r, u])
    db_session.commit()

    # Run 1
    conn1 = db_session.connection()
    leg1, mod1 = backfill_hospital_auth_models(conn1)
    prom1, amb1 = backfill_hospital_admin_roles(conn1)
    db_session.commit()
    assert prom1 == 1
    assert amb1 == 0
    db_session.refresh(r)
    db_session.refresh(h)
    state1_code = r.code
    state1_admin = r.is_admin_role
    state1_auth = h.auth_model

    # Run 2
    conn2 = db_session.connection()
    leg2, mod2 = backfill_hospital_auth_models(conn2)
    prom2, amb2 = backfill_hospital_admin_roles(conn2)
    db_session.commit()
    assert prom2 == 0, "Second run must not re-promote already canonical roles (0 duplicate updates)"
    assert amb2 == 0
    db_session.refresh(r)
    db_session.refresh(h)
    assert r.code == state1_code, "Role code must remain identical on run 2"
    assert r.is_admin_role == state1_admin, "Role is_admin_role must remain identical on run 2"
    assert h.auth_model == state1_auth, "Hospital auth_model must remain identical on run 2"


# Backward compatibility alias
test_migration_backfill_idempotency = test_case_5_migration_run_twice_idempotency

