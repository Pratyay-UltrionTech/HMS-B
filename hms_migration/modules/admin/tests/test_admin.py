"""
Comprehensive unit and integration tests for the Admin domain.
Tests:
- GET /admin/modules
- Roles CRUD (/admin/roles)
- Users CRUD (/admin/users)
- Audit logs (/admin/audit-logs)
- Shift Roster endpoints (/admin/shift-roster, /admin/shift-roster/assign, seed, clear, snapshot)
"""

from __future__ import annotations

from datetime import date, time
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.admin.api.admin_api import router as admin_router
from hms_migration.modules.doctors.entities.doctor import HospitalUser, ShiftType, StaffDailyShift, StaffRole
from hms_migration.modules.masters.entities.organization_entities import Department
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.audit import write_audit_log
from hms_migration.shared.auth import (
    get_hospital_context,
    require_hospital_admin,
    require_hospital_user,
)


@pytest.fixture
def admin_db():
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
def admin_client(admin_db):
    hospital_id = uuid4()
    admin_user_id = uuid4()

    hospital = Hospital(
        id=hospital_id,
        hospital_id="HOSP-ADMIN-001",
        name="Admin Test Hospital",
        address="123 Hospital St",
        phone="1234567890",
        email="admin@admintest.com",
        password_hash="fakehash",
        is_active=True,
    )
    role = StaffRole(
        id=uuid4(),
        hospital_id=hospital_id,
        name="admin",
    )
    admin_user = HospitalUser(
        id=admin_user_id,
        hospital_id=hospital_id,
        role_id=role.id,
        name="Admin User",
        phone="1234567890",
        email="admin@admintest.com",
        password_hash="fakehash",
        is_active=True,
    )

    admin_db.add_all([hospital, role, admin_user])
    admin_db.commit()

    app = FastAPI()
    app.include_router(admin_router)

    app.dependency_overrides[get_transitional_sync_session] = lambda: admin_db
    app.dependency_overrides[get_hospital_context] = lambda: hospital_id
    admin_payload = {
        "user_id": str(admin_user_id),
        "hospital_id": str(hospital_id),
        "role": "admin",
        "email": "admin@admintest.com",
    }
    app.dependency_overrides[require_hospital_user] = lambda: admin_payload
    app.dependency_overrides[require_hospital_admin] = lambda: admin_payload

    client = TestClient(app)
    client.hospital_id = hospital_id
    client.admin_user_id = admin_user_id
    return client


def test_get_modules(admin_client):
    res = admin_client.get("/admin/modules")
    assert res.status_code == 200
    modules = res.json()
    assert isinstance(modules, list)
    assert len(modules) > 0
    mod_names = [m["key"] for m in modules]
    assert "admin" in mod_names
    assert "registration" in mod_names
    assert "billing" in mod_names


def test_roles_crud(admin_client):
    # 1. Create role
    create_res = admin_client.post(
        "/admin/roles",
        json={"name": "Pharmacist Assistant", "description": "Assists pharmacy operations"},
    )
    assert create_res.status_code == 201
    role = create_res.json()
    assert role["name"] == "Pharmacist Assistant"
    role_id = role["id"]

    # 2. List roles
    list_res = admin_client.get("/admin/roles")
    assert list_res.status_code == 200
    roles = list_res.json()
    assert any(r["id"] == role_id for r in roles)

    # 3. Update role
    upd_res = admin_client.put(
        f"/admin/roles/{role_id}",
        json={"name": "Senior Pharmacist Assistant"},
    )
    assert upd_res.status_code == 200
    assert upd_res.json()["name"] == "Senior Pharmacist Assistant"

    # 4. Delete role
    del_res = admin_client.delete(f"/admin/roles/{role_id}")
    assert del_res.status_code == 204

    # Verify deleted
    list_after = admin_client.get("/admin/roles")
    assert not any(r["id"] == role_id for r in list_after.json())


def test_users_crud(admin_client, admin_db):
    # Create a role first
    role = StaffRole(
        id=uuid4(),
        hospital_id=admin_client.hospital_id,
        name="Nurse",
    )
    admin_db.add(role)
    admin_db.commit()

    # 1. Create user
    create_res = admin_client.post(
        "/admin/users",
        json={
            "name": "Jane Nurse",
            "email": "jane.nurse@test.com",
            "phone": "9876543210",
            "password": "Password123!",
            "role_id": str(role.id),
        },
    )
    assert create_res.status_code == 201
    user = create_res.json()
    assert user["name"] == "Jane Nurse"
    assert user["email"] == "jane.nurse@test.com"
    user_id = user["id"]

    # 2. List users
    list_res = admin_client.get("/admin/users")
    assert list_res.status_code == 200
    users = list_res.json()
    assert any(u["id"] == user_id for u in users)

    # 3. Update user
    upd_res = admin_client.put(
        f"/admin/users/{user_id}",
        json={"name": "Jane Nurse Senior", "is_active": True},
    )
    assert upd_res.status_code == 200
    assert upd_res.json()["name"] == "Jane Nurse Senior"

    # 4. Delete user
    del_res = admin_client.delete(f"/admin/users/{user_id}")
    assert del_res.status_code == 204


def test_audit_logs(admin_client, admin_db):
    write_audit_log(
        admin_db,
        hospital_id=admin_client.hospital_id,
        actor={"sub": "admin@admintest.com", "name": "Admin", "role": "admin"},
        action="TEST_ACTION",
        entity_type="TestEntity",
        entity_id=str(uuid4()),
        summary="Test audit log entry",
        details={"key": "value"},
    )
    admin_db.commit()

    res = admin_client.get("/admin/audit-logs")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 1
    assert data[0]["action"] == "TEST_ACTION"
    assert data[0]["summary"] == "Test audit log entry"


def test_shift_roster_workflows(admin_client, admin_db):
    # Setup department, doctor/staff and shift types
    dept = Department(
        id=uuid4(),
        hospital_id=admin_client.hospital_id,
        name="General Medicine",
        code="GM",
    )
    role = StaffRole(id=uuid4(), hospital_id=admin_client.hospital_id, name="Doctor")
    staff = HospitalUser(
        id=uuid4(),
        hospital_id=admin_client.hospital_id,
        role_id=role.id,
        name="Dr. John Staff",
        email="john.staff@test.com",
        phone="1122334455",
        password_hash="hash",
        is_active=True,
    )
    st = ShiftType(
        id=uuid4(),
        hospital_id=admin_client.hospital_id,
        department_id=dept.id,
        name="Morning General",
        start_time=time(8, 0),
        end_time=time(14, 0),
        is_active=True,
    )
    admin_db.add_all([dept, role, staff, st])
    admin_db.commit()

    # 1. Assign shift (PUT /admin/shift-roster)
    assign_res = admin_client.put(
        "/admin/shift-roster",
        json={
            "roster_date": "2026-09-10",
            "user_id": str(staff.id),
            "shift_id": str(st.id),
            "status": "on_duty",
        },
    )
    assert assign_res.status_code == 200
    assert assign_res.json()["user_id"] == str(staff.id)

    # 2. Get shift roster list (GET /admin/shift-roster?roster_date=...)
    list_res = admin_client.get(
        "/admin/shift-roster",
        params={"roster_date": "2026-09-10"},
    )
    assert list_res.status_code == 200
    roster = list_res.json()
    assert roster["roster_date"] == "2026-09-10"
    assert len(roster["entries"]) >= 1

    # 3. Snapshot (GET /admin/shift-roster/snapshot?roster_date=...)
    snap_res = admin_client.get(
        "/admin/shift-roster/snapshot",
        params={"roster_date": "2026-09-10"},
    )
    assert snap_res.status_code == 200
    snap = snap_res.json()
    assert snap["roster_date"] == "2026-09-10"
    assert snap["entries_count"] >= 1

    # 4. Clear (DELETE /admin/shift-roster?roster_date=...)
    clear_res = admin_client.delete(
        "/admin/shift-roster",
        params={"roster_date": "2026-09-10"},
    )
    assert clear_res.status_code == 204

    # 5. Seed (POST /admin/shift-roster/seed)
    seed_res = admin_client.post(
        "/admin/shift-roster/seed",
        json={"roster_date": "2026-09-15", "overwrite": False},
    )
    assert seed_res.status_code == 200
    assert "entries" in seed_res.json()
