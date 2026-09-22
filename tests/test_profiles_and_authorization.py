"""
Unit and Integration Tests for Profiles, Multi-Role Permissions, Scoping, and Admin Bypass.

Guarantees Verified:
1. CRITICAL: super_admin and hospital_admin retain 100% unrestricted bypass across all modules and actions.
2. Multi-role mathematical union aggregation across all assigned roles.
3. Action-level permission enforcement (view, edit, create, delete, validate, release, dispense, refund, administer).
4. Practitioner identity verification prevents impersonation on clinical endpoints.
5. Department and ward scoping returns correct sets for staff and empty list (unrestricted) for admins.
6. Token version revocation in auth endpoint invalidates stale sessions immediately.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from modules.doctors.entities.doctor import (
    HospitalUser,
    HospitalUserDepartment,
    HospitalUserLocation,
    HospitalUserRole,
    Practitioner,
    RolePermission,
    StaffRole,
)
from modules.masters.entities.organization_entities import Department
from modules.tenancy.entities.hospital import Hospital
from shared.auth.dependencies import (
    assert_practitioner_identity,
    get_user_department_ids,
    get_user_ward_ids,
    require_permission,
)
from shared.auth.jwt import create_access_token


# ===========================================================================
# 1. Admin Zero-Regression and Freeze Guarantee Tests
# ===========================================================================

def test_admin_complete_permission_bypass(hospital: Hospital):
    """
    Hospital Admin and Super Admin MUST bypass all permission checks,
    even without any assigned roles, permissions, or hospital_user rows.
    """
    admin_user = {
        "user_id": str(hospital.id),
        "email": hospital.email,
        "name": hospital.name,
        "role": "hospital_admin",
        "hospital_id": str(hospital.id),
        "hospital_uuid": str(hospital.id),
    }

    super_admin_user = {
        "user_id": "super-admin-uuid",
        "email": "superadmin@ultrion.internal",
        "name": "Super Admin",
        "role": "super_admin",
        "hospital_id": None,
        "hospital_uuid": None,
    }

    actions_to_check = [
        ("emergency", "view"),
        ("emergency", "administer"),
        ("laboratory", "validate"),
        ("radiology", "release"),
        ("pharmacy", "dispense"),
        ("billing", "refund"),
        ("procurement", "approve"),
        ("insurance", "approve"),
        ("admin", "edit"),
    ]

    for module_key, action in actions_to_check:
        dep_fn = require_permission(module_key, action)
        res_admin = dep_fn(user=admin_user, db=None)
        assert res_admin == admin_user

        res_super = dep_fn(user=super_admin_user, db=None)
        assert res_super == super_admin_user


def test_admin_practitioner_identity_bypass(hospital: Hospital):
    """
    Hospital Admins and Super Admins can execute clinical actions without
    being bound to a specific practitioner ID.
    """
    admin_user = {
        "user_id": str(hospital.id),
        "role": "hospital_admin",
        "hospital_uuid": str(hospital.id),
    }
    super_admin_user = {
        "user_id": "super-admin-uuid",
        "role": "super_admin",
    }

    random_doctor_id = uuid.uuid4()
    assert_practitioner_identity(admin_user, random_doctor_id)
    assert_practitioner_identity(super_admin_user, random_doctor_id)


def test_admin_scoping_is_unrestricted(hospital: Hospital, db_session: Session):
    """
    Admin users must receive empty list from scoping helpers, indicating full, unrestricted access.
    """
    admin_user = {
        "user_id": str(hospital.id),
        "role": "hospital_admin",
        "hospital_uuid": str(hospital.id),
    }
    super_admin_user = {
        "user_id": "super-admin-uuid",
        "role": "super_admin",
    }

    assert get_user_department_ids(admin_user, db_session) == []
    assert get_user_ward_ids(admin_user, db_session) == []
    assert get_user_department_ids(super_admin_user, db_session) == []
    assert get_user_ward_ids(super_admin_user, db_session) == []


# ===========================================================================
# 2. Staff Action-Level Permission Resolution Tests
# ===========================================================================

def test_staff_permission_denied_without_role(hospital: Hospital, db_session: Session):
    """
    Staff user without any role assignments or permissions gets 403 Forbidden.
    """
    user_id = uuid.uuid4()
    user = HospitalUser(
        id=user_id,
        hospital_id=hospital.id,
        role_id=uuid.uuid4(),
        name="Unassigned Staff",
        email="unassigned@hospital.com",
        phone="9999999991",
        password_hash="hash",
    )
    db_session.add(user)
    db_session.commit()

    staff_user_token_payload = {
        "user_id": str(user_id),
        "email": user.email,
        "name": user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "staff_role_id": str(user.role_id),
    }

    dep_fn = require_permission("laboratory", "view")
    with pytest.raises(HTTPException) as exc_info:
        dep_fn(user=staff_user_token_payload, db=db_session)
    assert exc_info.value.status_code == 403
    assert "missing laboratory:view permission" in exc_info.value.detail


def test_staff_action_level_flag_enforcement(hospital: Hospital, db_session: Session):
    """
    Staff user with only can_view can view, but is rejected for can_edit, can_validate, etc.
    """
    role = StaffRole(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Lab Assistant",
        is_active=True,
    )
    db_session.add(role)

    perm = RolePermission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        module_key="laboratory",
        can_view=True,
        can_edit=False,
        can_validate=False,
    )
    db_session.add(perm)

    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Lab Tech Junior",
        email="labtech@hospital.com",
        phone="9999999992",
        password_hash="hash",
    )
    db_session.add(user)

    ur = HospitalUserRole(
        hospital_id=hospital.id,
        user_id=user.id,
        role_id=role.id,
        is_primary=True,
    )
    db_session.add(ur)
    db_session.commit()

    token_payload = {
        "user_id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "staff_role_ids": [str(role.id)],
    }

    # Viewing succeeds
    view_fn = require_permission("laboratory", "view")
    res = view_fn(user=token_payload, db=db_session)
    assert res == token_payload

    # Validating fails with 403
    val_fn = require_permission("laboratory", "validate")
    with pytest.raises(HTTPException) as exc_info:
        val_fn(user=token_payload, db=db_session)
    assert exc_info.value.status_code == 403
    assert "missing laboratory:validate permission" in exc_info.value.detail


# ===========================================================================
# 3. Multi-Role Mathematical Union Aggregation Tests
# ===========================================================================

def test_multi_role_permission_union(hospital: Hospital, db_session: Session):
    """
    When a user has multiple roles:
    Role A: blood_bank view=True, validate=False
    Role B: blood_bank view=False, validate=True
    User should have BOTH view=True and validate=True.
    """
    role_a = StaffRole(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Blood Bank Viewer",
        is_active=True,
    )
    role_b = StaffRole(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Transfusion Officer",
        is_active=True,
    )
    db_session.add_all([role_a, role_b])

    perm_a = RolePermission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role_a.id,
        module_key="blood_bank",
        can_view=True,
        can_validate=False,
        can_edit=False,
    )
    perm_b = RolePermission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role_b.id,
        module_key="blood_bank",
        can_view=False,
        can_validate=True,
        can_edit=False,
    )
    db_session.add_all([perm_a, perm_b])

    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role_a.id,
        name="Dr. Multi Role",
        email="multirole@hospital.com",
        phone="9999999993",
        password_hash="hash",
    )
    db_session.add(user)

    ur_a = HospitalUserRole(hospital_id=hospital.id, user_id=user.id, role_id=role_a.id, is_primary=True)
    ur_b = HospitalUserRole(hospital_id=hospital.id, user_id=user.id, role_id=role_b.id, is_primary=False)
    db_session.add_all([ur_a, ur_b])
    db_session.commit()

    token_payload = {
        "user_id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "staff_role_ids": [str(role_a.id), str(role_b.id)],
    }

    # Both view and validate pass via union
    view_fn = require_permission("blood_bank", "view")
    assert view_fn(user=token_payload, db=db_session) == token_payload

    val_fn = require_permission("blood_bank", "validate")
    assert val_fn(user=token_payload, db=db_session) == token_payload

    # Edit still rejected since neither role has edit
    edit_fn = require_permission("blood_bank", "edit")
    with pytest.raises(HTTPException) as exc_info:
        edit_fn(user=token_payload, db=db_session)
    assert exc_info.value.status_code == 403
    assert "missing blood_bank:edit permission" in exc_info.value.detail


# ===========================================================================
# 4. Practitioner Impersonation Protection Tests
# ===========================================================================

def test_practitioner_impersonation_protection(hospital: Hospital, db_session: Session):
    """
    Staff cannot submit clinical orders or notes on behalf of another doctor.
    """
    doc_id_1 = uuid.uuid4()
    doc_id_2 = uuid.uuid4()

    doc_user_1 = {
        "user_id": str(doc_id_1),
        "email": "doc1@hospital.com",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    }

    nurse_user = {
        "user_id": str(uuid.uuid4()),
        "email": "nurse@hospital.com",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    }

    # Doctor 1 executing action for Doctor 1 -> Allowed
    assert_practitioner_identity(doc_user_1, doc_id_1)

    # Doctor 1 trying to execute action for Doctor 2 -> Denied
    with pytest.raises(HTTPException) as exc_info_1:
        assert_practitioner_identity(doc_user_1, doc_id_2)
    assert exc_info_1.value.status_code == 403
    assert "designated practitioner" in exc_info_1.value.detail

    # Nurse trying to execute action as Doctor 1 -> Denied
    with pytest.raises(HTTPException) as exc_info_2:
        assert_practitioner_identity(nurse_user, doc_id_1)
    assert exc_info_2.value.status_code == 403
    assert "designated practitioner" in exc_info_2.value.detail


# ===========================================================================
# 5. Department and Location Scoping Tests
# ===========================================================================

def test_department_and_ward_scoping_resolution(hospital: Hospital, db_session: Session):
    """
    Verify staff department and ward assignments are resolved correctly.
    """
    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=uuid.uuid4(),
        name="Scoped Staff",
        email="scoped@hospital.com",
        phone="9999999994",
        password_hash="hash",
    )
    db_session.add(user)

    dept_1 = Department(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Cardiology",
        is_active=True,
    )
    dept_2 = Department(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Pediatrics",
        is_active=True,
    )
    db_session.add_all([dept_1, dept_2])

    ward_1 = uuid.uuid4()
    ward_2 = uuid.uuid4()

    # Assign dept_1 to user
    ud = HospitalUserDepartment(
        hospital_id=hospital.id,
        user_id=user.id,
        department_id=dept_1.id,
        is_primary=True,
    )
    # Assign ward_1 and ward_2 to user
    ul_1 = HospitalUserLocation(
        hospital_id=hospital.id,
        user_id=user.id,
        ward_id=ward_1,
    )
    ul_2 = HospitalUserLocation(
        hospital_id=hospital.id,
        user_id=user.id,
        ward_id=ward_2,
    )
    db_session.add_all([ud, ul_1, ul_2])
    db_session.commit()

    token_payload = {
        "user_id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    }

    dept_ids = get_user_department_ids(token_payload, db_session)
    assert set(dept_ids) == {dept_1.id}

    ward_ids = get_user_ward_ids(token_payload, db_session)
    assert set(ward_ids) == {ward_1, ward_2}


# ===========================================================================
# 6. Token Version Revocation Invalidation Test
# ===========================================================================

def test_token_version_invalidation_on_user_update(hospital: Hospital, db_session: Session):
    """
    When token_version in DB is higher than token_version in user claims,
    the /auth/me endpoint rejects the stale token.
    """
    from modules.auth.api.auth_api import me

    role = StaffRole(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Staff Member",
        is_active=True,
    )
    db_session.add(role)

    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Revoked Staff",
        email="revoked@hospital.com",
        phone="9999999995",
        password_hash="hash",
        token_version=2,  # Current DB version is 2
    )
    db_session.add(user)

    ur = HospitalUserRole(
        hospital_id=hospital.id,
        user_id=user.id,
        role_id=role.id,
        is_primary=True,
    )
    db_session.add(ur)
    db_session.commit()

    # Claims with stale version 1
    stale_user_claims = {
        "user_id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "token_version": 1,
    }

    with pytest.raises(HTTPException) as exc_info:
        me(user=stale_user_claims, db=db_session)
    assert exc_info.value.status_code == 401
    assert "Session expired or revoked" in exc_info.value.detail


# ===========================================================================
# 7. Canonical Action Format & Central Authorization Service
# ===========================================================================

def test_canonical_action_format_normalization(hospital: Hospital, db_session: Session):
    """
    AuthorizationService correctly handles 'module.action', 'module:action',
    and ('module', 'action') uniformly.
    """
    from shared.auth.service import authorization

    role = StaffRole(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Pathologist",
        is_active=True,
    )
    db_session.add(role)

    perm = RolePermission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        module_key="laboratory",
        can_view=True,
        can_validate=True,
    )
    db_session.add(perm)

    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. Path",
        email="path@hospital.com",
        phone="9999999996",
        password_hash="hash",
    )
    db_session.add(user)

    ur = HospitalUserRole(
        hospital_id=hospital.id,
        user_id=user.id,
        role_id=role.id,
        is_primary=True,
    )
    db_session.add(ur)
    db_session.commit()

    token_payload = {
        "user_id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "staff_role_ids": [str(role.id)],
    }

    # All 3 representations must allow validate
    assert authorization.can(token_payload, "laboratory.validate", db=db_session) is True
    assert authorization.can(token_payload, "laboratory:validate", db=db_session) is True
    assert authorization.can(token_payload, ("laboratory", "validate"), db=db_session) is True

    # Check permission denied on ungranted action
    assert authorization.can(token_payload, "laboratory.refund", db=db_session) is False


# ===========================================================================
# 8. Role Temporal Expiration (expires_at)
# ===========================================================================

def test_role_temporal_expiration(hospital: Hospital, db_session: Session):
    """
    A staff user whose assigned role is expired (expires_at in the past)
    is denied authorization with ROLE_EXPIRED.
    """
    from datetime import timedelta
    from shared.auth.service import AuthReasonCode, authorization

    past_date = datetime.now(timezone.utc) - timedelta(days=2)

    role = StaffRole(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Temporary Locum",
        is_active=True,
    )
    db_session.add(role)

    perm = RolePermission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        module_key="laboratory",
        can_view=True,
    )
    db_session.add(perm)

    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Expired Locum",
        email="locum@hospital.com",
        phone="9999999997",
        password_hash="hash",
    )
    db_session.add(user)

    ur = HospitalUserRole(
        hospital_id=hospital.id,
        user_id=user.id,
        role_id=role.id,
        is_primary=True,
        expires_at=past_date,
    )
    db_session.add(ur)
    db_session.commit()

    token_payload = {
        "user_id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    }

    decision = authorization.evaluate(token_payload, "laboratory.view", db=db_session)
    assert decision.allowed is False
    assert decision.reason_code == AuthReasonCode.ROLE_EXPIRED

    with pytest.raises(HTTPException) as exc_info:
        authorization.require(token_payload, "laboratory.view", db=db_session)
    assert exc_info.value.status_code == 403


# ===========================================================================
# 9. Tenant Boundary / Hospital Isolation
# ===========================================================================

def test_tenant_boundary_cross_hospital_denial(hospital: Hospital, db_session: Session):
    """
    Staff user belonging to Hospital A cannot access a resource belonging to Hospital B.
    """
    from shared.auth.service import AuthReasonCode, authorization

    other_hospital_id = uuid.uuid4()

    token_payload = {
        "user_id": str(uuid.uuid4()),
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "permissions": {"laboratory": {"can_view": True}},
    }

    class MockResource:
        hospital_id = other_hospital_id

    decision = authorization.evaluate(token_payload, "laboratory.view", resource=MockResource())
    assert decision.allowed is False
    assert decision.reason_code == AuthReasonCode.HOSPITAL_MISMATCH


# ===========================================================================
# 10. Department & Ward Scope Enforcement
# ===========================================================================

def test_department_and_ward_scope_enforcement(hospital: Hospital):
    """
    Staff assigned to Department X cannot access Department Y resource.
    Staff assigned to Ward A cannot access Ward B resource.
    """
    from shared.auth.service import AuthReasonCode, authorization

    cardiology_dept = uuid.uuid4()
    neurology_dept = uuid.uuid4()
    icu_ward = uuid.uuid4()
    general_ward = uuid.uuid4()

    token_payload = {
        "user_id": str(uuid.uuid4()),
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "department_ids": [str(cardiology_dept)],
        "ward_ids": [str(icu_ward)],
        "permissions": {"doctors": {"can_view": True}},
    }

    class ResourceInCardio:
        hospital_id = str(hospital.id)
        department_id = str(cardiology_dept)
        ward_id = str(icu_ward)

    class ResourceInNeuro:
        hospital_id = str(hospital.id)
        department_id = str(neurology_dept)
        ward_id = str(icu_ward)

    class ResourceInGeneralWard:
        hospital_id = str(hospital.id)
        department_id = str(cardiology_dept)
        ward_id = str(general_ward)

    # In scope
    d_ok = authorization.evaluate(token_payload, "doctors.view", resource=ResourceInCardio())
    assert d_ok.allowed is True

    # Department out of scope
    d_dept_denied = authorization.evaluate(token_payload, "doctors.view", resource=ResourceInNeuro())
    assert d_dept_denied.allowed is False
    assert d_dept_denied.reason_code == AuthReasonCode.DEPARTMENT_SCOPE_DENIED

    # Ward out of scope
    d_ward_denied = authorization.evaluate(token_payload, "doctors.view", resource=ResourceInGeneralWard())
    assert d_ward_denied.allowed is False
    assert d_ward_denied.reason_code == AuthReasonCode.WARD_SCOPE_DENIED


# ===========================================================================
# 11. Segregation of Duties (SoD) Enforcement
# ===========================================================================

def test_segregation_of_duties_enforcement(hospital: Hospital):
    """
    Staff user cannot self-validate, self-approve, or self-refund a resource
    that they originally entered or created.
    """
    from shared.auth.service import AuthReasonCode, authorization

    staff_user_id = str(uuid.uuid4())
    other_staff_id = str(uuid.uuid4())

    author_payload = {
        "user_id": staff_user_id,
        "name": "Lab Tech Bob",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "permissions": {"laboratory": {"can_view": True, "can_validate": True}},
    }

    validator_payload = {
        "user_id": other_staff_id,
        "name": "Pathologist Alice",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "permissions": {"laboratory": {"can_view": True, "can_validate": True}},
    }

    class MockLabResult:
        hospital_id = str(hospital.id)
        entered_by_user_id = staff_user_id

    # Self-validation must be denied under SoD
    d_self = authorization.evaluate(author_payload, "laboratory.validate", resource=MockLabResult())
    assert d_self.allowed is False
    assert d_self.reason_code == AuthReasonCode.SELF_APPROVAL_PROHIBITED

    # Independent validator must be allowed
    d_other = authorization.evaluate(validator_payload, "laboratory.validate", resource=MockLabResult())
    assert d_other.allowed is True
    assert d_other.reason_code == AuthReasonCode.ALLOWED


# ===========================================================================
# 12. Admin Complete Bypass Across Scopes, SoD, and Tenant Boundaries
# ===========================================================================

def test_admin_complete_bypass_across_all_constraints(hospital: Hospital):
    """
    Admins bypass SoD, department scopes, ward scopes, and module constraints.
    """
    from shared.auth.service import AuthReasonCode, authorization

    admin_id = str(uuid.uuid4())
    admin_payload = {
        "user_id": admin_id,
        "role": "hospital_admin",
        "hospital_uuid": str(hospital.id),
    }

    class ConstrainedResource:
        hospital_id = str(uuid.uuid4())  # Different hospital
        department_id = str(uuid.uuid4())  # Arbitrary department
        ward_id = str(uuid.uuid4())  # Arbitrary ward
        created_by_user_id = admin_id  # Created by admin himself

    # Admin bypasses SoD and scopes
    decision = authorization.evaluate(admin_payload, "billing.refund", resource=ConstrainedResource())
    assert decision.allowed is True
    assert decision.reason_code == AuthReasonCode.ADMIN_BYPASS


# ===========================================================================
# 13. Authorization Explanation & Simulation Output
# ===========================================================================

def test_authorization_simulation_and_explanation(hospital: Hospital):
    """
    Simulation returns structured evaluation without executing or modifying state.
    """
    from shared.auth.service import authorization

    user_payload = {
        "user_id": str(uuid.uuid4()),
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "permissions": {"billing": {"can_view": True}},
    }

    sim_denied = authorization.simulate(user_payload, "billing.refund")
    assert sim_denied["decision"] == "DENY"
    assert sim_denied["details"]["reason_code"] == "PERMISSION_MISSING"

    sim_allowed = authorization.simulate(user_payload, "billing.view")
    assert sim_allowed["decision"] == "ALLOW"
    assert sim_allowed["details"]["reason_code"] == "ALLOWED"


# ===========================================================================
# 14. HTTP-Level Integration Tests (Simulation Endpoint & Role Guarding)
# ===========================================================================

def test_http_admin_simulation_endpoint(
    client: TestClient,
    hospital: Hospital,
    admin_headers: dict[str, str],
    db_session: Session,
):
    """
    Hospital admin can invoke the /api/admin/authorization/simulate endpoint
    to evaluate access for any staff member in their hospital.
    """
    role = StaffRole(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Pharmacy Clerk",
        is_active=True,
    )
    db_session.add(role)

    perm = RolePermission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        module_key="pharmacy",
        can_view=True,
        can_dispense=True,
    )
    db_session.add(perm)

    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="John Dispenser",
        email="dispenser@hospital.com",
        phone="9999999998",
        password_hash="hash",
    )
    db_session.add(user)

    ur = HospitalUserRole(
        hospital_id=hospital.id,
        user_id=user.id,
        role_id=role.id,
        is_primary=True,
    )
    db_session.add(ur)
    db_session.commit()

    # 1. Simulate allowed action
    resp_allow = client.post(
        "/api/admin/authorization/simulate",
        headers=admin_headers,
        json={
            "user_id": str(user.id),
            "action": "pharmacy.dispense",
        },
    )
    assert resp_allow.status_code == 200
    data_allow = resp_allow.json()
    assert data_allow["decision"] == "ALLOW"
    assert data_allow["details"]["reason_code"] == "ALLOWED"

    # 2. Simulate denied action (refund on pharmacy is not granted)
    resp_deny = client.post(
        "/api/admin/authorization/simulate",
        headers=admin_headers,
        json={
            "user_id": str(user.id),
            "action": "pharmacy.refund",
        },
    )
    assert resp_deny.status_code == 200
    data_deny = resp_deny.json()
    assert data_deny["decision"] == "DENY"
    assert data_deny["details"]["reason_code"] == "PERMISSION_MISSING"


def test_http_staff_simulation_denied(
    client: TestClient,
    hospital: Hospital,
    auth_headers: dict[str, str],
):
    """
    Ordinary staff members cannot invoke the /api/admin/authorization/simulate endpoint.
    """
    resp = client.post(
        "/api/admin/authorization/simulate",
        headers=auth_headers,
        json={
            "user_id": str(uuid.uuid4()),
            "action": "laboratory.validate",
        },
    )
    assert resp.status_code == 403
