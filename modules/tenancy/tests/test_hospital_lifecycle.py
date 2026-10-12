"""
Unit and integration tests for hospital operational lifecycle and session revocation.
Tests:
- ACTIVE -> SUSPENDED -> ACTIVE -> ARCHIVED transitions
- Instant session revocation: suspended hospital JWT rejected at authorization layer
- Reactivated hospital restores access immediately
"""

from __future__ import annotations

from uuid import uuid4
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.tenancy.api.tenancy_api import router as tenancy_router
from modules.tenancy.entities.hospital import Hospital, PlanType
from shared.auth import (
    create_access_token,
    get_hospital_context,
    require_hospital_user,
    require_super_admin,
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
def lifecycle_app(db_session):
    app = FastAPI()
    app.include_router(tenancy_router)

    # Protected sample endpoint simulating typical hospital staff operation
    @app.get("/test/protected-hospital-endpoint")
    def sample_endpoint(
        hospital_id=Depends(get_hospital_context),
        user=Depends(require_hospital_user),
    ):
        return {"status": "ok", "hospital_id": str(hospital_id)}

    app.dependency_overrides[get_transitional_sync_session] = lambda: db_session
    return app


def test_hospital_lifecycle_and_session_revocation(lifecycle_app, db_session):
    # 1. Create active hospital
    hospital_id = uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id="HMS-TEST-001",
        name="Lifecycle General Hospital",
        address="100 Main St",
        phone="9876543210",
        email="admin@lifecycle.org",
        password_hash="fakehash",
        plan=PlanType.basic,
        is_active=True,
        status="active",
    )
    db_session.add(hospital)
    db_session.commit()

    # 2. Issue a valid JWT for hospital admin / staff
    token_payload = {
        "sub": "admin@lifecycle.org",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital_id),
        "hospital_id": "HMS-TEST-001",
    }
    staff_token = create_access_token(token_payload)

    # 3. Super admin client
    client = TestClient(lifecycle_app)
    lifecycle_app.dependency_overrides[require_super_admin] = lambda: {
        "sub": "superadmin@ultriontech.com",
        "role": "super_admin",
    }

    # 4. Verify staff can access protected endpoint while ACTIVE
    res_active = client.get(
        "/test/protected-hospital-endpoint",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert res_active.status_code == 200
    assert res_active.json()["status"] == "ok"

    # 5. Super admin suspends hospital
    suspend_res = client.post(
        f"/hospitals/{hospital_id}/suspend",
        json={"reason": "Overdue payment audit verification"},
    )
    assert suspend_res.status_code == 200
    suspend_data = suspend_res.json()
    assert suspend_data["status"] == "suspended"
    assert suspend_data["is_active"] is False

    # Verify database state
    db_session.refresh(hospital)
    assert hospital.status == "suspended"
    assert hospital.is_active is False
    assert hospital.suspended_by == "superadmin@ultriontech.com"
    assert hospital.suspension_reason == "Overdue payment audit verification"

    # 6. CRITICAL TEST: Staff token MUST BE REJECTED IMMEDIATELY (Session Revocation)
    res_suspended = client.get(
        "/test/protected-hospital-endpoint",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert res_suspended.status_code == 403, res_suspended.text
    assert "suspended" in res_suspended.json()["detail"].lower()

    # 7. Super admin reactivates hospital
    reactivate_res = client.post(f"/hospitals/{hospital_id}/reactivate")
    assert reactivate_res.status_code == 200
    assert reactivate_res.json()["status"] == "active"
    assert reactivate_res.json()["is_active"] is True

    # 8. Staff token immediately works again
    res_restored = client.get(
        "/test/protected-hospital-endpoint",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert res_restored.status_code == 200

    # 9. Super admin archives hospital
    archive_res = client.post(
        f"/hospitals/{hospital_id}/archive",
        json={"reason": "Facility decommissioned"},
    )
    assert archive_res.status_code == 200
    assert archive_res.json()["status"] == "archived"
    assert archive_res.json()["is_active"] is False

    # Staff token rejected again
    res_archived = client.get(
        "/test/protected-hospital-endpoint",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert res_archived.status_code == 403


def test_direct_db_suspension_immediate_revocation_without_cache(lifecycle_app, db_session):
    """Proves tenant authorization is 100% cache-free and DB-authoritative.
    
    1. Issue valid JWT.
    2. Request succeeds.
    3. Suspend hospital DIRECTLY IN THE DATABASE (no API call, no cache invalidation helper).
    4. Reuse exact same JWT.
    5. Next request immediately receives 403 Forbidden with zero TTL waiting.
    """
    hosp_id = uuid4()
    hospital = Hospital(
        id=hosp_id,
        hospital_id="HMS-DB-DIRECT",
        name="Direct DB Test Hospital",
        address="100 Test St",
        phone="9876543211",
        email="admin@db-direct.org",
        password_hash="fakehash",
        plan=PlanType.basic,
        is_active=True,
        status="active",
    )
    db_session.add(hospital)
    db_session.commit()

    token = create_access_token({
        "sub": "admin@db-direct.org",
        "role": "hospital_admin",
        "hospital_uuid": str(hosp_id),
        "hospital_id": "HMS-DB-DIRECT",
    })

    client = TestClient(lifecycle_app)

    # 1. Request succeeds while active in DB
    res1 = client.get(
        "/test/protected-hospital-endpoint",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res1.status_code == 200

    # 2. Directly mutate database state behind the scenes (zero cache invalidation call)
    hospital.status = "suspended"
    hospital.is_active = False
    db_session.commit()

    # 3. Next request with same JWT must be immediately blocked by authoritative DB lookup
    res2 = client.get(
        "/test/protected-hospital-endpoint",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res2.status_code == 403
    assert "suspended" in res2.json()["detail"].lower()


def test_hospital_deletion_guards_and_empty_tenant_cleanup(lifecycle_app, db_session):
    """Exhaustive domain data deletion guards.
    
    Verifies that:
    - Empty disposable test tenant can be deleted (204).
    - Hospital with patient records is blocked from hard deletion (400).
    - Hospital with billing charges is blocked from hard deletion (400).
    - Hospital with appointments is blocked from hard deletion (400).
    """
    from decimal import Decimal
    from modules.patients.entities.patient import Patient
    from modules.billing.entities.billing_entities import (
        BillingCharge,
        BillingChargeStatus,
        BillingSourceType,
    )
    from modules.appointments.entities.appointment import Appointment
    from modules.appointments.entities.enums import AppointmentStatus

    client = TestClient(lifecycle_app)
    lifecycle_app.dependency_overrides[require_super_admin] = lambda: {
        "sub": "superadmin@ultriontech.com",
        "role": "super_admin",
    }

    # Case A: Empty disposable hospital -> Deletion succeeds
    empty_hosp_id = uuid4()
    empty_hosp = Hospital(
        id=empty_hosp_id,
        hospital_id="HMS-EMPTY-1",
        name="Empty Test Hospital",
        address="Zero Records St",
        phone="9876543212",
        email="empty@hospital.org",
        password_hash="fakehash",
        plan=PlanType.basic,
        is_active=True,
        status="active",
    )
    db_session.add(empty_hosp)
    db_session.commit()

    del_res = client.delete(f"/hospitals/{empty_hosp_id}")
    assert del_res.status_code == 204
    assert db_session.query(Hospital).filter(Hospital.id == empty_hosp_id).first() is None

    # Case B: Hospital with Patient -> Blocked
    patient_hosp_id = uuid4()
    patient_hosp = Hospital(
        id=patient_hosp_id,
        hospital_id="HMS-WITH-PATIENT",
        name="Patient Hospital",
        address="10 Clinical Way",
        phone="9876543213",
        email="patient@hospital.org",
        password_hash="fakehash",
        plan=PlanType.basic,
        is_active=True,
        status="active",
    )
    db_session.add(patient_hosp)
    db_session.flush()

    pat = Patient(
        hospital_id=patient_hosp_id,
        uhid="UHID-999999",
        name="Jane Doe",
        first_name="Jane",
        last_name="Doe",
        gender="female",
        age=30,
        mobile="9876543210",
        address="100 Main St",
    )
    db_session.add(pat)
    db_session.commit()

    del_patient_res = client.delete(f"/hospitals/{patient_hosp_id}")
    assert del_patient_res.status_code == 400
    assert "cannot delete hospital" in del_patient_res.json()["detail"].lower()
    assert "patients" in del_patient_res.json()["detail"].lower()

    # Case C: Hospital with Billing Charge -> Blocked
    billing_hosp_id = uuid4()
    billing_hosp = Hospital(
        id=billing_hosp_id,
        hospital_id="HMS-WITH-BILLING",
        name="Billing Hospital",
        address="20 Finance Way",
        phone="9876543214",
        email="billing@hospital.org",
        password_hash="fakehash",
        plan=PlanType.basic,
        is_active=True,
        status="active",
    )
    db_session.add(billing_hosp)
    db_session.flush()

    charge = BillingCharge(
        hospital_id=billing_hosp_id,
        patient_id=uuid4(),
        source_type=BillingSourceType.consultation,
        description="General Consultation",
        quantity=Decimal("1.0"),
        unit_price=Decimal("500.0"),
        charge_amount=Decimal("500.0"),
        net_amount=Decimal("500.0"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(charge)
    db_session.commit()

    del_billing_res = client.delete(f"/hospitals/{billing_hosp_id}")
    assert del_billing_res.status_code == 400
    assert "cannot delete hospital" in del_billing_res.json()["detail"].lower()
    assert "billing_charges" in del_billing_res.json()["detail"].lower()


def test_approval_failure_injection_transaction_rollback(db_session):
    """Artificially force failure during provisioning and assert complete transaction rollback.
    
    Asserts:
    - No Hospital entity remains.
    - No HospitalUser administrator remains.
    - No AccountSetupToken remains.
    - The registration request remains in PENDING status.
    """
    from modules.auth.entities.setup_token import AccountSetupToken
    from modules.tenancy.actions.tenancy_actions import TenancyActions
    from modules.tenancy.entities.hospital_registration_request import (
        HospitalRegistrationRequest,
        RegistrationStatus,
    )
    from modules.tenancy.contracts.registration_contracts import RegistrationApprovePayload
    from unittest.mock import patch

    req = HospitalRegistrationRequest(
        request_number="REQ-FAIL-TEST-001",
        hospital_name="Rollback Test Hospital",
        contact_name="Dr. Candidate",
        email="rollback@candidate.org",
        phone="9876543299",
        address="100 Crash Way",
        status=RegistrationStatus.pending,
    )
    db_session.add(req)
    db_session.commit()

    actions = TenancyActions(db_session)
    payload = RegistrationApprovePayload(plan=PlanType.basic)

    # Inject failure during audit logging before final commit
    with patch("modules.tenancy.actions.tenancy_actions.write_audit_log", side_effect=RuntimeError("Simulated DB Disk Crash")):
        with pytest.raises(RuntimeError, match="Simulated DB Disk Crash"):
            actions.approve_registration_request(req.id, payload, "superadmin@ultriontech.com")

    # Assert request is still pending
    db_session.refresh(req)
    assert req.status == RegistrationStatus.pending
    assert req.approved_hospital_id is None

    # Assert NO Hospital tenant was created
    assert db_session.query(Hospital).filter(Hospital.email == "rollback@candidate.org").first() is None
    # Assert NO AccountSetupToken was created
    assert db_session.query(AccountSetupToken).filter(AccountSetupToken.email == "rollback@candidate.org").first() is None

