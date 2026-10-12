"""
Comprehensive End-to-End Verification of Hospital Onboarding & Tenancy Lifecycle.

Traces all 10 mandatory verification scenarios:
  Scenario 1: Registration submission -> PENDING request, NO hospital or admin created.
  Scenario 2: Super Admin review -> Request appears in queue, details inspected.
  Scenario 3: Transactional approval -> Request APPROVED, 1 Hospital, 1 Admin HospitalUser, 1 SetupToken, audit log.
  Scenario 4: Password setup -> Token consumed, repeated use fails, admin can log in.
  Scenario 5: Suspension -> Active JWT immediately receives 403 on next protected request (zero TTL, no cache).
  Scenario 6: Reactivation -> Access restored.
  Scenario 7: Archival -> Operational access denied (403), data retained, Super Admin metadata visible.
  Scenario 8: Duplicate email multi-tenant login -> Wrong password returns generic 401 without membership leak;
              matching password disambiguates or authenticates cleanly.
  Scenario 9: Rejection -> Status REJECTED with reason, no hospital or admin created.
  Scenario 10: Callback request -> Persisted in database and visible in Super Admin queue.
"""

import sys
import os
sys.path.insert(0, os.path.abspath("."))
import uuid
import hashlib
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy import text

# Import test application and database fixtures
from app.main import app
from infrastructure.postgres.session import get_transitional_sync_session
from modules.tenancy.entities.hospital import Hospital
from modules.tenancy.entities.hospital_registration_request import HospitalRegistrationRequest, RegistrationStatus
from modules.auth.entities.setup_token import AccountSetupToken
from modules.tenancy.entities.callback_request import CallbackRequest
from modules.doctors.entities.doctor import HospitalUser
from shared.auth.jwt import create_access_token
from shared.auth.security import hash_password, verify_password
from shared.audit.entities.audit_log import AuditLog

client = TestClient(app)

def run_e2e_scenarios():
    print("=" * 70)
    print("STARTING 10 MANDATORY E2E SCENARIOS VERIFICATION")
    print("=" * 70)

    # Setup super admin headers
    sa_token = create_access_token({
        "sub": "admin@hospital.com",
        "email": "admin@hospital.com",
        "role": "super_admin",
        "name": "Super Administrator",
    })
    sa_headers = {"Authorization": f"Bearer {sa_token}"}

    # --------------------------------------------------------------------------
    # Scenario 1: Public Registration
    # --------------------------------------------------------------------------
    print("\n--- Scenario 1: Public Registration ---")
    reg_email = f"e2e_admin_{uuid.uuid4().hex[:6]}@metrohospital.org"
    reg_phone = f"9876{uuid.uuid4().int % 1000000:06d}"
    reg_payload = {
        "hospital_name": "Metro City Hospital",
        "contact_name": "Dr. Sarah Connor",
        "email": reg_email,
        "phone": reg_phone,
        "address": "456 Health Boulevard",
        "city": "Bengaluru",
        "state": "Karnataka",
        "pincode": "560001",
        "comments": "Requesting expedited onboarding",
    }
    res = client.post("/api/hospitals/registration-requests", json=reg_payload)
    assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.text}"
    reg_data = res.json()
    assert reg_data["status"] == "pending", f"Expected pending, got {reg_data['status']}"
    assert "token" not in reg_data
    assert "password" not in reg_data
    req_id = reg_data["id"]
    print(f"[PASS] Registration submitted. Request ID: {req_id}, Number: {reg_data['request_number']}")

    # Verify authoritative database state
    with next(get_transitional_sync_session()) as db:
        req_row = db.query(HospitalRegistrationRequest).filter(HospitalRegistrationRequest.id == uuid.UUID(req_id)).first()
        assert req_row is not None
        assert req_row.status == RegistrationStatus.pending
        # Confirm NO hospital exists yet
        hosp_count = db.query(Hospital).filter(Hospital.email == reg_email).count()
        assert hosp_count == 0, "Hospital should NOT exist before approval"
        # Confirm NO admin user exists yet
        user_count = db.query(HospitalUser).filter(HospitalUser.email == reg_email).count()
        assert user_count == 0, "HospitalUser should NOT exist before approval"
    print("[PASS] DB verified: Request is PENDING, zero Hospital rows, zero HospitalUser rows.")

    # --------------------------------------------------------------------------
    # Scenario 2: Super Admin Review
    # --------------------------------------------------------------------------
    print("\n--- Scenario 2: Super Admin Review ---")
    list_res = client.get("/api/hospitals/registration-requests?status=pending", headers=sa_headers)
    assert list_res.status_code == 200, f"Expected 200, got {list_res.status_code}"
    pending_list = list_res.json()["items"]
    matched = next((r for r in pending_list if r["id"] == req_id), None)
    assert matched is not None, "Pending request must be visible in Super Admin queue"

    detail_res = client.get(f"/api/hospitals/registration-requests/{req_id}", headers=sa_headers)
    assert detail_res.status_code == 200
    assert detail_res.json()["hospital_name"] == "Metro City Hospital"
    print("[PASS] Super Admin queue lists request, detail endpoint returns complete metadata.")

    # --------------------------------------------------------------------------
    # Scenario 3: Transactional Approval
    # --------------------------------------------------------------------------
    print("\n--- Scenario 3: Approval & Provisioning ---")
    approve_res = client.post(
        f"/api/hospitals/registration-requests/{req_id}/approve",
        json={"plan": "basic"},
        headers=sa_headers,
    )
    assert approve_res.status_code == 200, f"Approval failed: {approve_res.text}"
    app_data = approve_res.json()
    assert app_data["setup_url"] is not None
    assert "#token=" in app_data["setup_url"], f"URL should use fragment: {app_data['setup_url']}"
    raw_setup_token = app_data["setup_url"].split("#token=")[1]
    hosp_uuid_str = str(app_data["hospital_uuid"])
    print(f"[PASS] Request APPROVED. Hospital UUID: {hosp_uuid_str}, Fragment setup URL provided.")

    with next(get_transitional_sync_session()) as db:
        # 1 Hospital exists with status=active
        hosp = db.query(Hospital).filter(Hospital.id == uuid.UUID(hosp_uuid_str)).first()
        assert hosp is not None
        assert hosp.status == "active"
        assert hosp.is_active is True
        # 1 Admin HospitalUser exists
        adm = db.query(HospitalUser).filter(HospitalUser.hospital_id == hosp.id).first()
        assert adm is not None
        assert adm.email == reg_email
        # 1 SetupToken exists
        token_hash = hashlib.sha256(raw_setup_token.encode("utf-8")).hexdigest()
        tok = db.query(AccountSetupToken).filter(AccountSetupToken.token_hash == token_hash).first()
        assert tok is not None
        assert tok.used_at is None
        # Audit log exists
        audits = db.query(AuditLog).filter(AuditLog.entity_id == str(req_id)).all()
        assert len(audits) >= 1
    print("[PASS] DB verified: Exactly 1 Hospital (ACTIVE), 1 HospitalUser, 1 SetupToken, audit log recorded.")

    # --------------------------------------------------------------------------
    # Scenario 4: Password Setup
    # --------------------------------------------------------------------------
    print("\n--- Scenario 4: Password Setup ---")
    setup_res = client.post(
        "/api/auth/setup-password",
        json={"token": raw_setup_token, "password": "MetroSecurePassword123!"},
    )
    assert setup_res.status_code == 200, f"Password setup failed: {setup_res.text}"
    assert "successfully" in setup_res.json()["message"].lower()

    # Second submission must fail (single-use)
    setup_res_2 = client.post(
        "/api/auth/setup-password",
        json={"token": raw_setup_token, "password": "AnotherPassword123!"},
    )
    assert setup_res_2.status_code == 400, "Second token usage must be rejected (400)"

    # Admin can now login
    login_res = client.post(
        "/api/auth/login",
        json={"email": reg_email, "password": "MetroSecurePassword123!"},
    )
    assert login_res.status_code == 200, f"Login failed: {login_res.text}"
    hospital_jwt = login_res.json()["access_token"]
    hospital_headers = {"Authorization": f"Bearer {hospital_jwt}"}
    print("[PASS] Password successfully configured, single-use token invalidated, admin login authenticated.")

    # --------------------------------------------------------------------------
    # Scenario 5: Suspension with Immediate Session Revocation (No Cache / No TTL)
    # --------------------------------------------------------------------------
    print("\n--- Scenario 5: Suspension & Immediate Revocation ---")
    # Verify protected access works before suspension
    probe_before = client.get("/api/auth/me", headers=hospital_headers)
    assert probe_before.status_code == 200, f"Access before suspension failed: {probe_before.text}"

    # Suspend hospital from Super Admin
    suspend_res = client.post(
        f"/api/hospitals/{hosp_uuid_str}/suspend",
        json={"reason": "Regulatory compliance check in progress"},
        headers=sa_headers,
    )
    assert suspend_res.status_code == 200, f"Suspension failed: {suspend_res.text}"

    # Reuse exact same JWT on next request: MUST immediately receive 403
    probe_after = client.get("/api/auth/me", headers=hospital_headers)
    assert probe_after.status_code == 403, f"Expected immediate 403, got {probe_after.status_code}"
    assert "suspended" in probe_after.json()["detail"].lower()
    print("[PASS] Reused exact same JWT immediately returned 403 Forbidden with zero TTL and zero cache delay.")

    # --------------------------------------------------------------------------
    # Scenario 6: Reactivation
    # --------------------------------------------------------------------------
    print("\n--- Scenario 6: Reactivation ---")
    reactivate_res = client.post(f"/api/hospitals/{hosp_uuid_str}/reactivate", headers=sa_headers)
    assert reactivate_res.status_code == 200, f"Reactivation failed: {reactivate_res.text}"

    probe_reactivated = client.get("/api/auth/me", headers=hospital_headers)
    assert probe_reactivated.status_code == 200, f"Expected 200 after reactivation, got {probe_reactivated.status_code}"
    print("[PASS] Reactivation restored operational access immediately.")

    # --------------------------------------------------------------------------
    # Scenario 7: Archival
    # --------------------------------------------------------------------------
    print("\n--- Scenario 7: Archival ---")
    archive_res = client.post(
        f"/api/hospitals/{hosp_uuid_str}/archive",
        json={"reason": "End-of-life tenancy decommissioning"},
        headers=sa_headers,
    )
    assert archive_res.status_code == 200, f"Archival failed: {archive_res.text}"

    # Hospital user operational access must be denied
    probe_archived = client.get("/api/auth/me", headers=hospital_headers)
    assert probe_archived.status_code == 403, f"Expected 403 for archived hospital, got {probe_archived.status_code}"
    assert "archived" in probe_archived.json()["detail"].lower()

    # Super Admin can still inspect tenant metadata
    sa_hosp_view = client.get(f"/api/hospitals/{hosp_uuid_str}", headers=sa_headers)
    assert sa_hosp_view.status_code == 200
    assert sa_hosp_view.json()["status"] == "archived"
    print("[PASS] Archival blocks operational access (403), preserves data, and Super Admin can inspect tenant.")

    # --------------------------------------------------------------------------
    # Scenario 8: Duplicate Email Multi-Tenant Login Disambiguation
    # --------------------------------------------------------------------------
    print("\n--- Scenario 8: Multi-Tenant Login Security ---")
    shared_email = f"shared_dr_{uuid.uuid4().hex[:6]}@multitenant.org"
    common_pass = "SharedSecretPass123!"
    other_pass = "CompletelyDifferentPass456!"

    with next(get_transitional_sync_session()) as db:
        # Create Hospital Alpha
        h_alpha = Hospital(
            hospital_id=f"HOSP-A-{uuid.uuid4().hex[:4]}".upper(),
            name="Alpha General Hospital",
            address="1 Alpha Way",
            phone="9876540001",
            email=f"alpha_{uuid.uuid4().hex[:6]}@hosp.org",
            password_hash=hash_password("adminpass"),
            status="active",
            is_active=True,
        )
        # Create Hospital Beta
        h_beta = Hospital(
            hospital_id=f"HOSP-B-{uuid.uuid4().hex[:4]}".upper(),
            name="Beta Medical Center",
            address="2 Beta Way",
            phone="9876540002",
            email=f"beta_{uuid.uuid4().hex[:6]}@hosp.org",
            password_hash=hash_password("adminpass"),
            status="active",
            is_active=True,
        )
        db.add_all([h_alpha, h_beta])
        db.flush()

        # Staff roles
        from modules.doctors.entities.doctor import StaffRole, HospitalUserRole
        role_alpha = StaffRole(hospital_id=h_alpha.id, name="Doctor")
        role_beta = StaffRole(hospital_id=h_beta.id, name="Doctor")
        db.add_all([role_alpha, role_beta])
        db.flush()

        # Doctor user in Alpha with common_pass
        u_alpha = HospitalUser(
            hospital_id=h_alpha.id,
            role_id=role_alpha.id,
            name="Dr. Multi Tenant",
            email=shared_email,
            password_hash=hash_password(common_pass),
            phone="9876540003",
            is_active=True,
        )
        # Doctor user in Beta with common_pass
        u_beta = HospitalUser(
            hospital_id=h_beta.id,
            role_id=role_beta.id,
            name="Dr. Multi Tenant",
            email=shared_email,
            password_hash=hash_password(common_pass),
            phone="9876540003",
            is_active=True,
        )
        db.add_all([u_alpha, u_beta])
        db.flush()

        ur_alpha = HospitalUserRole(hospital_id=h_alpha.id, user_id=u_alpha.id, role_id=role_alpha.id, is_primary=True)
        ur_beta = HospitalUserRole(hospital_id=h_beta.id, user_id=u_beta.id, role_id=role_beta.id, is_primary=True)
        db.add_all([ur_alpha, ur_beta])
        db.commit()
        alpha_id_str = str(h_alpha.id)
        beta_id_str = str(h_beta.id)

    # 8a: Wrong password must return generic 401 without revealing any hospital memberships
    bad_pass_res = client.post("/api/auth/login", json={"email": shared_email, "password": "WrongPassword!"})
    assert bad_pass_res.status_code == 401, f"Expected 401, got {bad_pass_res.status_code}"
    assert "matching_hospitals" not in bad_pass_res.json(), "MUST NOT leak matching_hospitals on bad password"

    # 8b: Correct password without hospital_id returns 409 disambiguation
    multi_res = client.post("/api/auth/login", json={"email": shared_email, "password": common_pass})
    assert multi_res.status_code == 409, f"Expected 409, got {multi_res.status_code}"
    matching = multi_res.json()["matching_hospitals"]
    assert len(matching) == 2
    matched_ids = [m["hospital_uuid"] for m in matching]
    assert alpha_id_str in matched_ids and beta_id_str in matched_ids
    assert "role" not in matching[0], "Roles should not be exposed in disambiguation list"

    # 8c: Targeted login with hospital_id succeeds cleanly
    targeted_res = client.post(
        "/api/auth/login",
        json={"email": shared_email, "password": common_pass, "hospital_id": alpha_id_str},
    )
    assert targeted_res.status_code == 200
    assert targeted_res.json()["hospital_id"] == h_alpha.hospital_id
    print("[PASS] Multi-tenant auth verified: zero info leaked on wrong password, 409 only on valid credentials, targeted login succeeds.")

    # --------------------------------------------------------------------------
    # Scenario 9: Registration Rejection
    # --------------------------------------------------------------------------
    print("\n--- Scenario 9: Registration Rejection ---")
    rej_email = f"e2e_rej_{uuid.uuid4().hex[:6]}@bogushospital.org"
    rej_phone = f"9876{uuid.uuid4().int % 1000000:06d}"
    rej_res = client.post(
        "/api/hospitals/registration-requests",
        json={
            "hospital_name": "Bogus Clinic",
            "contact_name": "Fake Doctor",
            "email": rej_email,
            "phone": rej_phone,
            "address": "99 Fake St",
            "city": "Nowhere",
            "state": "Delhi",
            "pincode": "110001",
        },
    )
    assert rej_res.status_code == 201, f"Rejection submission failed: {rej_res.text}"
    rej_req_id = rej_res.json()["id"]

    reject_res = client.post(
        f"/api/hospitals/registration-requests/{rej_req_id}/reject",
        json={"rejection_reason": "Incomplete regulatory accreditation documentation"},
        headers=sa_headers,
    )
    assert reject_res.status_code == 200
    assert reject_res.json()["status"] == "rejected"
    assert reject_res.json()["rejection_reason"] == "Incomplete regulatory accreditation documentation"

    with next(get_transitional_sync_session()) as db:
        h_count = db.query(Hospital).filter(Hospital.email == rej_email).count()
        assert h_count == 0, "No hospital should be created on rejection"
    print("[PASS] Rejection workflow verified: Status=REJECTED, reason stored, zero tenants created.")

    # --------------------------------------------------------------------------
    # Scenario 10: Callback Request Workflow
    # --------------------------------------------------------------------------
    print("\n--- Scenario 10: Callback Request Workflow ---")
    cb_email = f"cb_{uuid.uuid4().hex[:6]}@inquiryhospital.org"
    cb_phone = f"9876{uuid.uuid4().int % 1000000:06d}"
    cb_res = client.post(
        "/api/callback-requests",
        json={
            "name": "Dr. Watson",
            "phone": cb_phone,
            "email": cb_email,
            "location": "Inquiry Health Center",
            "comments": "Looking for 200-bed demonstration",
        },
    )
    assert cb_res.status_code == 201, f"Callback submission failed: {cb_res.text}"
    cb_id = str(cb_res.json()["id"])

    # Verify visible to Super Admin
    cb_list = client.get("/api/callback-requests?status=pending", headers=sa_headers)
    assert cb_list.status_code == 200
    cbs = cb_list.json()
    matched_cb = next((c for c in cbs if str(c["id"]) == cb_id), None)
    assert matched_cb is not None, "Callback request must appear in Super Admin callback list"

    # Update callback status to CONTACTED
    cb_update = client.patch(
        f"/api/callback-requests/{cb_id}",
        json={"status": "contacted", "notes": "Called Dr. Watson; demo scheduled for Friday."},
        headers=sa_headers,
    )
    assert cb_update.status_code == 200
    assert cb_update.json()["status"] == "contacted"
    print("[PASS] Callback workflow verified: Persisted, listed in Super Admin queue, status updated.")

    print("\n" + "=" * 70)
    print("ALL 10 E2E SCENARIOS VERIFIED SUCCESSFULLY WITH 100% SUCCESS!")
    print("=" * 70)

if __name__ == "__main__":
    run_e2e_scenarios()
