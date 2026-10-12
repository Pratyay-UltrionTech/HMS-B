"""
Targeted Verification of Audit Scenarios A through G.
Proves all 7 foundational identity, authorization, revocation, and lifecycle invariants.
"""

import sys
import os
sys.path.insert(0, os.path.abspath("."))
import uuid
import hashlib
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.main import app
from infrastructure.postgres.session import get_transitional_sync_session
from modules.tenancy.entities.hospital import Hospital
from modules.tenancy.entities.hospital_registration_request import HospitalRegistrationRequest
from modules.auth.entities.setup_token import AccountSetupToken
from modules.doctors.entities.doctor import HospitalUser, StaffRole, RolePermission, HospitalUserRole
from shared.auth.jwt import create_access_token
from shared.auth.security import hash_password

client = TestClient(app)

def run_scenarios():
    print("=" * 80)
    print("RUNNING TARGETED AUDIT VERIFICATION: SCENARIOS A THROUGH G")
    print("=" * 80)

    sa_token = create_access_token({
        "sub": "superadmin@ultriontech.com",
        "email": "superadmin@ultriontech.com",
        "role": "super_admin",
        "name": "Super Admin",
    })
    sa_headers = {"Authorization": f"Bearer {sa_token}"}

    # =========================================================================
    # SCENARIO A: Hospital Admin Identity & Authority
    # =========================================================================
    print("\n[SCENARIO A] Public approval -> Human Admin -> Hospital Admin Authority")
    admin_email = f"scenario_a_{uuid.uuid4().hex[:6]}@cityhealth.org"
    reg_payload = {
        "hospital_name": "City Health Hospital",
        "contact_name": "Dr. Alice Smith",
        "email": admin_email,
        "phone": f"9123{uuid.uuid4().int % 1000000:06d}",
        "address": "123 Health Ave",
        "city": "Mumbai",
        "state": "Maharashtra",
        "pincode": "400001",
    }
    r = client.post("/api/hospitals/registration-requests", json=reg_payload)
    assert r.status_code == 201
    req_id = r.json()["id"]

    # Approve
    appr = client.post(f"/api/hospitals/registration-requests/{req_id}/approve", json={"plan": "basic"}, headers=sa_headers)
    assert appr.status_code == 200, f"Approve failed: {appr.text}"
    appr_data = appr.json()
    setup_url = appr_data["setup_url"]
    assert "#token=" in setup_url
    raw_token = setup_url.split("#token=")[1]
    hosp_uuid_str = str(appr_data["hospital_uuid"])

    # Setup Password
    setup_res = client.post("/api/auth/setup-password", json={"token": raw_token, "password": "AdminPassword123!"})
    assert setup_res.status_code == 200

    # Human admin login
    login_res = client.post("/api/auth/login", json={"email": admin_email, "password": "AdminPassword123!"})
    assert login_res.status_code == 200
    login_data = login_res.json()
    assert login_data["role"] == "hospital_admin", f"Expected hospital_admin, got {login_data['role']}"
    admin_jwt = login_data["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_jwt}"}

    # Call actual admin-only route: POST /api/admin/roles
    role_res = client.post(
        "/api/admin/roles",
        json={
            "name": f"Junior Nurse {uuid.uuid4().hex[:4]}",
            "description": "General care staff",
            "permissions": [{"module_key": "vitals", "can_view": True, "can_edit": True}],
        },
        headers=admin_headers,
    )
    assert role_res.status_code == 201, f"Admin route failed for hospital_admin: {role_res.text}"
    created_role_id = role_res.json()["id"]
    print(f"  -> Human administrator successfully accessed admin-only route (POST /api/admin/roles: 201 Created).")

    # Now create ordinary staff user
    staff_email = f"nurse_{uuid.uuid4().hex[:6]}@cityhealth.org"
    with next(get_transitional_sync_session()) as db:
        ordinary_user = HospitalUser(
            hospital_id=uuid.UUID(hosp_uuid_str),
            role_id=uuid.UUID(created_role_id),
            name="Nurse Bob",
            email=staff_email,
            password_hash=hash_password("StaffPassword123!"),
            phone="9000000001",
            is_active=True,
        )
        db.add(ordinary_user)
        db.flush()
        ur = HospitalUserRole(hospital_id=uuid.UUID(hosp_uuid_str), user_id=ordinary_user.id, role_id=uuid.UUID(created_role_id), is_primary=True)
        db.add(ur)
        db.commit()

    # Staff login
    staff_login = client.post("/api/auth/login", json={"email": staff_email, "password": "StaffPassword123!"})
    assert staff_login.status_code == 200
    assert staff_login.json()["role"] == "hospital_staff"
    staff_jwt = staff_login.json()["access_token"]
    staff_headers = {"Authorization": f"Bearer {staff_jwt}"}

    # Ordinary staff calls admin-only route -> MUST FAIL with 403 Forbidden
    staff_denied = client.post(
        "/api/admin/roles",
        json={"name": "Hacker Role", "description": "Unauthorized", "permissions": []},
        headers=staff_headers,
    )
    assert staff_denied.status_code == 403, f"Expected 403 for ordinary staff, got {staff_denied.status_code}"
    print(f"  -> Ordinary staff forbidden from admin-only route (POST /api/admin/roles: 403 Forbidden).")
    print("[PASS] SCENARIO A VERIFIED")

    # =========================================================================
    # SCENARIO B: Hospital vs Human Credential Separation & Deactivation Guard
    # =========================================================================
    print("\n[SCENARIO B] Hospital vs Human Credential Separation & Deactivation Guard")
    with next(get_transitional_sync_session()) as db:
        h_row = db.query(Hospital).filter(Hospital.id == uuid.UUID(hosp_uuid_str)).first()
        assert h_row.password_hash is None, f"Hospital.password_hash must be None for user-account tenants, got: {h_row.password_hash}"
        assert h_row.auth_model == "user_accounts"
    print("  -> Hospital organization row has password_hash = NULL (zero organization credentials written).")

    # Deactivate the administrator
    with next(get_transitional_sync_session()) as db:
        admin_row = db.query(HospitalUser).filter(HospitalUser.email == admin_email).first()
        admin_row.is_active = False
        db.commit()

    # Attempt login with deactivated credentials
    deact_login = client.post("/api/auth/login", json={"email": admin_email, "password": "AdminPassword123!"})
    assert deact_login.status_code == 401, f"Expected 401 for deactivated user, got {deact_login.status_code}"
    assert "invalid" in deact_login.json()["detail"].lower() or "inactive" in deact_login.json()["detail"].lower()
    print("  -> Deactivated user login blocked with 401 (Zero fallback to Hospital organization row).")

    # Attempt protected API request with deactivated user's previously issued JWT
    deact_req = client.get("/api/auth/me", headers=admin_headers)
    assert deact_req.status_code == 401
    assert "inactive" in deact_req.json()["detail"].lower()
    print("  -> Existing active JWT of deactivated user immediately blocked on protected routes (401).")

    # Reactivate admin for subsequent tests
    with next(get_transitional_sync_session()) as db:
        admin_row = db.query(HospitalUser).filter(HospitalUser.email == admin_email).first()
        admin_row.is_active = True
        db.commit()
    print("[PASS] SCENARIO B VERIFIED")

    # =========================================================================
    # SCENARIO C: Direct Super Admin Onboarding
    # =========================================================================
    print("\n[SCENARIO C] Direct Super Admin Onboarding (No 5-char password, identical architecture)")
    dir_admin_email = f"direct_adm_{uuid.uuid4().hex[:6]}@stjude.org"
    dir_payload = {
        "hospital_id": f"HOSP-{uuid.uuid4().hex[:5]}".upper(),
        "name": "St. Jude Premier Clinic",
        "email": dir_admin_email,
        "phone": f"9888{uuid.uuid4().int % 1000000:06d}",
        "address": "777 Grace Road",
        "admin_name": "Dr. Gregory House",
        "admin_email": dir_admin_email,
    }
    dir_res = client.post("/api/hospitals", json=dir_payload, headers=sa_headers)
    assert dir_res.status_code == 201, f"Direct hospital creation failed: {dir_res.text}"
    dir_data = dir_res.json()
    assert "password" not in dir_data or dir_data.get("password") is None
    assert dir_data.get("setup_url") is not None
    assert "#token=" in dir_data["setup_url"]
    dir_hosp_id = dir_data["id"]

    # Verify DB: Hospital.password_hash is NULL, HospitalUser created, StaffRole has is_admin_role=True
    with next(get_transitional_sync_session()) as db:
        dir_h = db.query(Hospital).filter(Hospital.id == uuid.UUID(dir_hosp_id)).first()
        assert dir_h.password_hash is None, "Direct onboarding MUST NOT set Hospital.password_hash"
        assert dir_h.auth_model == "user_accounts"
        dir_u = db.query(HospitalUser).filter(HospitalUser.hospital_id == dir_h.id).first()
        assert dir_u is not None
        assert dir_u.email == dir_admin_email
        dir_r = db.query(StaffRole).filter(StaffRole.id == dir_u.role_id).first()
        assert dir_r.is_admin_role is True
        assert dir_r.code == "HOSPITAL_ADMIN"
    print("  -> Direct hospital creation produced SetupToken fragment URL, null Hospital.password_hash, canonical Hospital Admin role.")
    print("[PASS] SCENARIO C VERIFIED")

    # =========================================================================
    # SCENARIO D: Session Revocation via token_version
    # =========================================================================
    print("\n[SCENARIO D] Session Revocation via token_version on Normal Protected Endpoints")
    # Fresh login as Alice
    alice_login = client.post("/api/auth/login", json={"email": admin_email, "password": "AdminPassword123!"})
    assert alice_login.status_code == 200
    old_jwt = alice_login.json()["access_token"]
    old_headers = {"Authorization": f"Bearer {old_jwt}"}

    # Verify old JWT succeeds on normal protected route
    norm_res_before = client.get(f"/api/admin/roles?hospital_id={hosp_uuid_str}", headers=old_headers)
    assert norm_res_before.status_code == 200

    # Forgot password -> obtain reset token
    forgot_res = client.post("/api/auth/forgot-password", json={"email": admin_email, "hospital_id": hosp_uuid_str})
    assert forgot_res.status_code == 200
    reset_tok = forgot_res.json().get("reset_token")

    # Reset password
    reset_res = client.post("/api/auth/reset-password", json={"token": reset_tok, "password": "NewAlicePassword456!"})
    assert reset_res.status_code == 200

    # Test OLD JWT on normal protected endpoint (NOT just /auth/me) -> MUST FAIL (401)
    norm_res_after = client.get(f"/api/admin/roles?hospital_id={hosp_uuid_str}", headers=old_headers)
    assert norm_res_after.status_code == 401, f"Expected 401 on old JWT after password reset, got {norm_res_after.status_code}"
    assert "expired or revoked" in norm_res_after.json()["detail"].lower()
    print("  -> Password reset incremented token_version; old JWT immediately rejected on /api/admin/roles with 401.")
    print("[PASS] SCENARIO D VERIFIED")

    # =========================================================================
    # SCENARIO E: Cache Audit & Permission Freshness
    # =========================================================================
    print("\n[SCENARIO E] Permission Freshness (Zero application authorization cache)")
    # Alice logs in with new password
    alice_login_new = client.post("/api/auth/login", json={"email": admin_email, "password": "NewAlicePassword456!"})
    assert alice_login_new.status_code == 200
    alice_headers = {"Authorization": f"Bearer {alice_login_new.json()['access_token']}"}

    # Staff logs in
    staff_login_2 = client.post("/api/auth/login", json={"email": staff_email, "password": "StaffPassword123!"})
    assert staff_login_2.status_code == 200
    staff_headers_2 = {"Authorization": f"Bearer {staff_login_2.json()['access_token']}"}

    # Grant staff role procurement view permission
    with next(get_transitional_sync_session()) as db:
        rp = RolePermission(
            hospital_id=uuid.UUID(hosp_uuid_str),
            role_id=uuid.UUID(created_role_id),
            module_key="procurement",
            can_view=True,
            can_edit=False,
            can_create=False,
        )
        db.add(rp)
        db.commit()

    # Staff request to procurement succeeds
    probe1 = client.get("/api/procurement/requisitions", headers=staff_headers_2)
    assert probe1.status_code == 200, f"Expected 200 with permission, got {probe1.status_code}: {probe1.text}"

    # Immediate permission revocation in DB
    with next(get_transitional_sync_session()) as db:
        rp_row = db.query(RolePermission).filter(
            RolePermission.hospital_id == uuid.UUID(hosp_uuid_str),
            RolePermission.role_id == uuid.UUID(created_role_id),
            RolePermission.module_key == "procurement",
        ).first()
        rp_row.can_view = False
        db.commit()

    # Immediate repeat request with NO sleep, NO cache invalidation endpoint -> MUST FAIL (403)
    probe2 = client.get("/api/procurement/requisitions", headers=staff_headers_2)
    assert probe2.status_code == 403, f"Expected immediate 403 on permission revocation, got {probe2.status_code}"
    print("  -> Permission removed in DB; identical request immediately returned 403 with zero cache lag.")
    print("[PASS] SCENARIO E VERIFIED")

    # =========================================================================
    # SCENARIO F: Token Purpose Separation
    # =========================================================================
    print("\n[SCENARIO F] Token Purpose Separation (Setup token vs Reset token)")
    # Generate a new setup token via registration approval
    reg_f = client.post(
        "/api/hospitals/registration-requests",
        json={
            "hospital_name": "Clinic F",
            "contact_name": "Dr. F",
            "email": f"f_{uuid.uuid4().hex[:6]}@clinicf.org",
            "phone": f"9777{uuid.uuid4().int % 1000000:06d}",
            "address": "F Road",
            "city": "Pune",
            "state": "Maharashtra",
            "pincode": "411001",
        },
    )
    appr_f = client.post(f"/api/hospitals/registration-requests/{reg_f.json()['id']}/approve", json={"plan": "basic"}, headers=sa_headers)
    setup_tok_f = appr_f.json()["setup_url"].split("#token=")[1]

    # Generate a reset token for Alice
    forgot_f = client.post("/api/auth/forgot-password", json={"email": admin_email, "hospital_id": hosp_uuid_str})
    reset_tok_f = forgot_f.json()["reset_token"]

    # Cross 1: Attempt SETUP token on reset-password endpoint -> MUST FAIL (400)
    cross_1 = client.post("/api/auth/reset-password", json={"token": setup_tok_f, "password": "HackPassword123!"})
    assert cross_1.status_code == 400
    assert "purpose" in cross_1.json()["detail"].lower() or "invalid" in cross_1.json()["detail"].lower()

    # Cross 2: Attempt RESET token on setup-password endpoint -> MUST FAIL (400)
    cross_2 = client.post("/api/auth/setup-password", json={"token": reset_tok_f, "password": "HackPassword123!"})
    assert cross_2.status_code == 400
    assert "purpose" in cross_2.json()["detail"].lower() or "invalid" in cross_2.json()["detail"].lower()
    print("  -> Cross-endpoint token interchange strictly rejected (Setup token rejected on reset; Reset token rejected on setup).")
    print("[PASS] SCENARIO F VERIFIED")

    # =========================================================================
    # SCENARIO G: State Machine Transitions & Invariant Guard
    # =========================================================================
    print("\n[SCENARIO G] State Machine Transitions & Single Source of Truth")
    test_h_id = dir_hosp_id

    # ACTIVE -> SUSPENDED (200)
    s1 = client.post(f"/api/hospitals/{test_h_id}/suspend", json={"reason": "Audit"}, headers=sa_headers)
    assert s1.status_code == 200

    # SUSPENDED -> SUSPENDED (409 Duplicate)
    s2 = client.post(f"/api/hospitals/{test_h_id}/suspend", json={"reason": "Audit again"}, headers=sa_headers)
    assert s2.status_code == 409

    # SUSPENDED -> ACTIVE (200)
    r1 = client.post(f"/api/hospitals/{test_h_id}/reactivate", headers=sa_headers)
    assert r1.status_code == 200

    # ACTIVE -> ACTIVE (409 Duplicate)
    r2 = client.post(f"/api/hospitals/{test_h_id}/reactivate", headers=sa_headers)
    assert r2.status_code == 409

    # ACTIVE -> ARCHIVED (200)
    a1 = client.post(f"/api/hospitals/{test_h_id}/archive", json={"reason": "Retirement"}, headers=sa_headers)
    assert a1.status_code == 200

    # ARCHIVED -> ARCHIVED (409 Duplicate)
    a2 = client.post(f"/api/hospitals/{test_h_id}/archive", json={"reason": "Retirement again"}, headers=sa_headers)
    assert a2.status_code == 409

    # ARCHIVED -> ACTIVE (400 Terminal rejection)
    inv1 = client.post(f"/api/hospitals/{test_h_id}/reactivate", headers=sa_headers)
    assert inv1.status_code == 400
    assert "terminal" in inv1.json()["detail"].lower()

    # ARCHIVED -> SUSPENDED (400 Terminal rejection)
    inv2 = client.post(f"/api/hospitals/{test_h_id}/suspend", json={"reason": "Illegal transition"}, headers=sa_headers)
    assert inv2.status_code == 400
    assert "terminal" in inv2.json()["detail"].lower()

    # Invariant guard: generic update cannot set is_active=True on an archived hospital
    illegal_up = client.patch(f"/api/hospitals/{test_h_id}", json={"is_active": True}, headers=sa_headers)
    assert illegal_up.status_code == 400
    assert "terminal" in illegal_up.json()["detail"].lower()

    # Generic rename works while preserving lockstep status (status=archived, is_active=False)
    up1 = client.patch(f"/api/hospitals/{test_h_id}", json={"name": "Renamed Archived Hospital"}, headers=sa_headers)
    assert up1.status_code == 200
    assert up1.json()["name"] == "Renamed Archived Hospital"
    assert up1.json()["status"] == "archived"
    assert up1.json()["is_active"] is False
    print("  -> State machine strictly enforced: terminal ARCHIVED blocks transitions (400), duplicate transitions rejected (409), single source of truth preserved.")
    print("[PASS] SCENARIO G VERIFIED")

    print("\n" + "=" * 80)
    print("ALL AUDIT SCENARIOS A THROUGH G VERIFIED WITH 100% SUCCESS!")
    print("=" * 80)

if __name__ == "__main__":
    run_scenarios()

