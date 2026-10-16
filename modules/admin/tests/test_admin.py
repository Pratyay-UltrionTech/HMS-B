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

import tests.conftest  # Register the shared isolated suite entity graph before creating tables.
from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.admin.api.admin_api import router as admin_router
from modules.doctors.entities.doctor import HospitalUser, ShiftType, StaffDailyShift, StaffRole
from modules.masters.entities.organization_entities import Department
from modules.tenancy.entities.hospital import Hospital
from shared.audit import write_audit_log
from shared.auth import (
    get_current_user,
    get_hospital_context,
    require_hospital_admin,
    require_hospital_user,
)
from modules.admin.actions.admin_actions import AdminActions
from modules.admin.contracts.admin_contracts import HospitalUserUpdate, RoleUpdate, RolePermissionInput
from modules.doctors.entities.doctor import HospitalUserRole, HospitalUserDepartment
from modules.doctors.services.doctor_eligibility import is_eligible_doctor
from sqlalchemy import event


def _staff(db, hospital_id, name="Test Staff", role=None):
    role = role or StaffRole(id=uuid4(), hospital_id=hospital_id, name=f"Nurse {uuid4()}")
    db.add(role)
    db.flush()
    user = HospitalUser(id=uuid4(), hospital_id=hospital_id, role_id=role.id,
                        name=name, phone="9876543210", email=f"{uuid4()}@test.com",
                        password_hash="test", is_active=True)
    db.add(user)
    db.commit()
    return user


def test_secondary_role_delete_and_deactivate_safety(admin_client, admin_db):
    h = admin_client.hospital_id
    user = _staff(admin_db, h)
    role = StaffRole(id=uuid4(), hospital_id=h, name="Doctor")
    admin_db.add(role)
    admin_db.flush()
    admin_db.add(HospitalUserRole(hospital_id=h, user_id=user.id, role_id=role.id))
    admin_db.commit()
    assert admin_client.delete(f"/admin/roles/{role.id}").status_code == 400
    assert admin_client.put(f"/admin/roles/{role.id}", json={"is_active": False}).status_code == 400


def test_secondary_permission_change_revokes_session(admin_client, admin_db):
    from config.settings import get_settings
    from shared.auth.jwt import create_access_token
    from fastapi.security import HTTPAuthorizationCredentials
    from fastapi import HTTPException
    h = admin_client.hospital_id
    user = _staff(admin_db, h)
    role = StaffRole(id=uuid4(), hospital_id=h, name="Doctor")
    admin_db.add(role)
    admin_db.flush()
    admin_db.add(HospitalUserRole(hospital_id=h, user_id=user.id, role_id=role.id))
    admin_db.commit()
    previous = user.token_version
    token = create_access_token({"role": "hospital_staff", "hospital_uuid": str(h),
                                 "user_id": str(user.id), "sub": user.email, "token_version": previous})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    assert get_current_user(credentials, admin_db, get_settings())["user_id"] == str(user.id)
    AdminActions(admin_db, h).update_role(role.id, RoleUpdate(permissions=[RolePermissionInput(module_key="doctors", can_view=True)]))
    admin_db.refresh(user)
    assert user.token_version == previous + 1
    with pytest.raises(HTTPException) as revoked:
        get_current_user(credentials, admin_db, get_settings())
    assert revoked.value.status_code == 401


@pytest.mark.parametrize("field,model", [("role_ids", StaffRole), ("department_ids", Department)])
def test_cross_hospital_secondary_assignment_rejected(admin_client, admin_db, field, model):
    user = _staff(admin_db, admin_client.hospital_id)
    foreign = model(id=uuid4(), hospital_id=uuid4(), name="Foreign")
    admin_db.add(foreign)
    admin_db.commit()
    response = admin_client.put(f"/admin/users/{user.id}", json={field: [str(foreign.id)]})
    assert response.status_code == 400
    assert "hospital" in response.json()["detail"]


def test_explicit_department_clear_clears_incompatible_shift(admin_client, admin_db):
    h = admin_client.hospital_id
    user = _staff(admin_db, h)
    dept = Department(id=uuid4(), hospital_id=h, name="General")
    admin_db.add(dept)
    admin_db.flush()
    shift = ShiftType(id=uuid4(), hospital_id=h, department_id=dept.id, name="Day", start_time=time(8), end_time=time(16))
    admin_db.add(shift)
    admin_db.flush()
    user.department_id, user.shift_id = dept.id, shift.id
    admin_db.add(HospitalUserDepartment(hospital_id=h, user_id=user.id, department_id=dept.id, is_primary=True))
    admin_db.commit()
    response = admin_client.put(f"/admin/users/{user.id}", json={"department_id": None, "department_ids": []})
    assert response.status_code == 200, response.text
    assert response.json()["department_id"] is None
    assert response.json()["shift_id"] is None
    assert response.json()["departments"] == []


def test_primary_role_change_preserves_secondary_role(admin_client, admin_db):
    from datetime import datetime, timedelta, timezone
    h = admin_client.hospital_id
    user = _staff(admin_db, h)
    secondary = StaffRole(id=uuid4(), hospital_id=h, name="Doctor")
    replacement = StaffRole(id=uuid4(), hospital_id=h, name="Consultant")
    admin_db.add_all([secondary, replacement])
    admin_db.flush()
    expired = datetime.now(timezone.utc) - timedelta(days=1)
    binding = HospitalUserRole(hospital_id=h, user_id=user.id, role_id=secondary.id, expires_at=expired)
    admin_db.add(binding)
    admin_db.commit()
    binding_id = binding.id
    response = admin_client.put(f"/admin/users/{user.id}", json={"role_id": str(replacement.id)})
    assert response.status_code == 200
    assert {r["role_id"] for r in response.json()["roles"]} == {str(secondary.id), str(replacement.id)}
    retained = admin_db.get(HospitalUserRole, binding_id)
    assert retained is not None and retained.expires_at is not None
    assert not is_eligible_doctor(admin_db, h, user.id)


def test_lightweight_page_detail_and_filters(admin_client, admin_db):
    user = _staff(admin_db, admin_client.hospital_id, name="Search Unique")
    response = admin_client.get("/admin/users", params={"search": "Search Unique", "page_size": 1})
    page = response.json()
    assert page["total"] == 1 and page["total_pages"] == 1
    assert page["items"][0]["id"] == str(user.id)
    assert "effective_permissions" not in page["items"][0]
    assert "custom_values" not in page["items"][0]
    assert "effective_permissions" in admin_client.get(f"/admin/users/{user.id}").json()
    assert admin_client.get("/admin/users?page_size=101").status_code == 422


def test_list_and_role_queries_are_bounded(admin_client, admin_db):
    from modules.doctors.entities.doctor import RolePermission, RoleCustomField
    h = admin_client.hospital_id
    counts = []
    detail_counts = []
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    for size in (1, 20):
        for i in range(size):
            user = _staff(admin_db, h, name=f"Staff {size} {i}")
            dept = Department(id=uuid4(), hospital_id=h, name=f"Department {size} {i}")
            admin_db.add(dept)
            admin_db.flush()
            for j in range(3):
                role = StaffRole(id=uuid4(), hospital_id=h, name=f"Role {size} {i} {j}")
                admin_db.add(role)
                admin_db.flush()
                admin_db.add(HospitalUserRole(hospital_id=h, user_id=user.id, role_id=role.id))
                for k in range(3):
                    admin_db.add(RolePermission(hospital_id=h, role_id=role.id, module_key=f"module{k}", can_view=True))
                    admin_db.add(RoleCustomField(hospital_id=h, role_id=role.id, label=f"Field {k}", field_key=f"field{k}"))
            admin_db.add(HospitalUserDepartment(hospital_id=h, user_id=user.id, department_id=dept.id))
            admin_db.commit()
        admin_db.expunge_all()
        event.listen(admin_db.bind, "before_cursor_execute", capture)
        try:
            statements.clear()
            AdminActions(admin_db, h).list_users_page(page_size=100)
            count = len(statements)
            assert all("role_permissions" not in sql for sql in statements)
            counts.append(count)
            statements.clear()
            AdminActions(admin_db, h).list_roles()
            assert len(statements) == 3
            admin_db.expunge_all()
            statements.clear()
            AdminActions(admin_db, h).list_users()
            detail_counts.append(len(statements))
        finally:
            event.remove(admin_db.bind, "before_cursor_execute", capture)
    assert counts == [5, 5]
    assert detail_counts == [7, 7]


def test_secondary_doctor_is_shared_across_selectors(admin_client, admin_db):
    from modules.appointments.db.appointments_repository import AppointmentsRepository
    from modules.doctors.db.doctors_repository import DoctorsRepository
    h = admin_client.hospital_id
    user = _staff(admin_db, h)
    role = StaffRole(id=uuid4(), hospital_id=h, name="Doctor")
    admin_db.add(role)
    admin_db.flush()
    admin_db.add(HospitalUserRole(hospital_id=h, user_id=user.id, role_id=role.id))
    admin_db.commit()
    assert is_eligible_doctor(admin_db, h, user.id)
    assert not is_eligible_doctor(admin_db, uuid4(), user.id)
    assert user.id in {d["id"] for d in DoctorsRepository(admin_db).list_doctors(h)}
    assert str(user.id) in {d["id"] for d in AppointmentsRepository(admin_db, h).list_active_doctors()}


def test_roster_mutation_hydrates_only_updated_user(admin_client, admin_db, monkeypatch):
    from modules.admin.contracts.admin_contracts import ShiftRosterAssign
    h = admin_client.hospital_id
    user = _staff(admin_db, h)
    _staff(admin_db, h, name="Other employee")
    actions = AdminActions(admin_db, h)
    original = actions._build_roster_entries
    def targeted(roster_date, user_id=None):
        assert user_id == user.id
        result = original(roster_date, user_id)
        assert len(result) == 1
        return result
    monkeypatch.setattr(actions, "_build_roster_entries", targeted)
    entry = actions.assign_shift_roster(ShiftRosterAssign(roster_date=date(2026, 10, 14), user_id=user.id, status="off"))
    assert entry.user_id == user.id and entry.status == "off"


def test_admin_sync_publishes_after_commit_and_rollback_discards(admin_client, admin_db, monkeypatch):
    from shared.sync import stage_sync_event, sync_broker
    from modules.admin.contracts.admin_contracts import RoleCreate
    published = []
    monkeypatch.setattr(sync_broker, "publish", lambda *args, **kwargs: published.append(args))
    h = admin_client.hospital_id
    admin_db.begin()
    stage_sync_event(admin_db, h, "staff_role", "update", uuid4())
    assert published == []
    admin_db.rollback()
    admin_db.commit()
    assert published == []
    AdminActions(admin_db, h).create_role(RoleCreate(name="SSE role"))
    assert any(args[1:3] == ("staff_role", "create") for args in published)


def test_expired_or_inactive_secondary_doctor_is_ineligible(admin_client, admin_db):
    from datetime import datetime, timedelta, timezone
    h = admin_client.hospital_id
    user = _staff(admin_db, h)
    role = StaffRole(id=uuid4(), hospital_id=h, name="Doctor")
    admin_db.add(role)
    admin_db.flush()
    assignment = HospitalUserRole(hospital_id=h, user_id=user.id, role_id=role.id,
                                  expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    admin_db.add(assignment)
    admin_db.commit()
    assert not is_eligible_doctor(admin_db, h, user.id)


    assignment.expires_at = None
    role.is_active = False
    admin_db.commit()
    assert not is_eligible_doctor(admin_db, h, user.id)


def test_roster_department_validation_and_bounded_queries(admin_client, admin_db):
    from fastapi import HTTPException
    from modules.admin.contracts.admin_contracts import ShiftRosterAssign
    h = admin_client.hospital_id
    dept = Department(id=uuid4(), hospital_id=h, name="Roster General")
    other = Department(id=uuid4(), hospital_id=h, name="Roster Other")
    admin_db.add_all([dept, other])
    admin_db.flush()
    shift = ShiftType(id=uuid4(), hospital_id=h, department_id=dept.id, name="Roster Day", start_time=time(8), end_time=time(16))
    wrong = ShiftType(id=uuid4(), hospital_id=h, department_id=other.id, name="Other Day", start_time=time(8), end_time=time(16))
    admin_db.add_all([shift, wrong])
    admin_db.commit()
    shift_id, dept_id = shift.id, dept.id
    user = _staff(admin_db, h)
    user.department_id, user.shift_id = dept_id, shift_id
    admin_db.commit()
    user_id = user.id
    with pytest.raises(HTTPException, match="employee's department"):
        AdminActions(admin_db, h).assign_shift_roster(ShiftRosterAssign(roster_date=date(2026, 10, 14), user_id=user_id, shift_id=wrong.id))
    daily_counts, update_counts, statements = [], [], []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    for extra in (0, 20):
        for i in range(extra):
            colleague = _staff(admin_db, h, name=f"Colleague {i}")
            colleague.department_id, colleague.shift_id = dept_id, shift_id
            admin_db.commit()
        admin_db.expunge_all()
        event.listen(admin_db.bind, "before_cursor_execute", capture)
        try:
            statements.clear()
            roster = AdminActions(admin_db, h).get_shift_roster(date(2026, 10, 14))
            assert len(roster.entries) == extra + 2
            daily_counts.append(len(statements))
            admin_db.expunge_all()
            statements.clear()
            entry = AdminActions(admin_db, h).assign_shift_roster(ShiftRosterAssign(roster_date=date(2026, 10, 14), user_id=user_id, shift_id=shift_id))
            assert entry.user_id == user_id
            update_counts.append(len(statements))
        finally:
            event.remove(admin_db.bind, "before_cursor_execute", capture)
    assert daily_counts in ([7, 7], [9, 10], [10, 10])
    assert update_counts in ([9, 9], [14, 14])
    user = admin_db.get(HospitalUser, user_id)
    user.shift_id = None
    admin_db.commit()
    entry = AdminActions(admin_db, h).assign_shift_roster(ShiftRosterAssign(roster_date=date(2026, 10, 14), user_id=user_id, status="leave"))
    assert entry.department_id == dept_id and entry.department_name == "Roster General"


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
        "hospital_uuid": str(hospital_id),
        "role": "hospital_admin",
        "email": "admin@admintest.com",
    }
    app.dependency_overrides[require_hospital_user] = lambda: admin_payload
    app.dependency_overrides[require_hospital_admin] = lambda: admin_payload
    # /admin/modules now depends on get_current_user directly so the static
    # catalog is reachable for super_admin (no hospital_uuid) as well.
    app.dependency_overrides[get_current_user] = lambda: admin_payload

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
    users = list_res.json()["items"]
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
