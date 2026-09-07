"""
Comprehensive unit and integration tests for the Masters domain.
Tests all master catalogs:
- Wings
- Departments
- Shift Types
- Holidays
- Appointment Types
- Wards
- Rooms
- OT Rooms
- Suppliers
- Consultation Pricing
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
from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
from hms_migration.modules.beds.entities.bed import Bed, Room, Ward, WardType
from hms_migration.modules.billing.entities.billing_entities import ConsultationPricing
from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
from hms_migration.modules.doctors.entities.doctor import Holiday, HospitalUser, ShiftType, StaffRole
from hms_migration.modules.masters.api.masters_api import router as masters_router
from hms_migration.modules.masters.entities.organization_entities import Department, Supplier, Wing
from hms_migration.modules.ot.entities.ot_entities import OtRoom
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import (
    get_hospital_context,
    require_hospital_admin,
    require_hospital_user,
)


@pytest.fixture
def masters_db():
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
def masters_client(masters_db):
    hospital_id = uuid4()
    admin_user_id = uuid4()

    hospital = Hospital(
        id=hospital_id,
        hospital_id="HOSP-MASTERS-001",
        name="Masters Test Hospital",
        address="123 Hospital St",
        phone="1234567890",
        email="admin@masterstest.com",
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
        email="admin@masterstest.com",
        password_hash="fakehash",
        is_active=True,
    )

    masters_db.add_all([hospital, role, admin_user])
    masters_db.commit()

    app = FastAPI()
    app.include_router(masters_router)

    app.dependency_overrides[get_transitional_sync_session] = lambda: masters_db
    app.dependency_overrides[get_hospital_context] = lambda: hospital_id
    admin_payload = {
        "user_id": str(admin_user_id),
        "hospital_id": str(hospital_id),
        "role": "admin",
        "email": "admin@masterstest.com",
    }
    app.dependency_overrides[require_hospital_user] = lambda: admin_payload
    app.dependency_overrides[require_hospital_admin] = lambda: admin_payload

    client = TestClient(app)
    client.hospital_id = hospital_id
    client.admin_user_id = admin_user_id
    return client


def test_wings_crud(masters_client):
    # 1. Create
    create_res = masters_client.post(
        "/masters/wings",
        json={"name": "North Wing", "code": "NW", "description": "North block"},
    )
    assert create_res.status_code == 201
    wing = create_res.json()
    assert wing["name"] == "North Wing"
    wing_id = wing["id"]

    # 2. List
    list_res = masters_client.get("/masters/wings")
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1

    # 3. Update
    upd_res = masters_client.put(
        f"/masters/wings/{wing_id}",
        json={"name": "North Wing Updated"},
    )
    assert upd_res.status_code == 200
    assert upd_res.json()["name"] == "North Wing Updated"

    # 4. Delete
    del_res = masters_client.delete(f"/masters/wings/{wing_id}")
    assert del_res.status_code == 204


def test_departments_crud(masters_client):
    # Create wing first
    wing_res = masters_client.post("/masters/wings", json={"name": "Surgical Wing", "code": "SW"})
    wing_id = wing_res.json()["id"]

    # 1. Create Department
    create_res = masters_client.post(
        "/masters/departments",
        json={"wing_id": wing_id, "name": "Orthopedics", "code": "ORTHO"},
    )
    assert create_res.status_code == 201
    dept = create_res.json()
    assert dept["name"] == "Orthopedics"
    dept_id = dept["id"]

    # 2. List
    list_res = masters_client.get("/masters/departments")
    assert list_res.status_code == 200
    assert any(d["id"] == dept_id for d in list_res.json())

    # 3. Update
    upd_res = masters_client.put(
        f"/masters/departments/{dept_id}",
        json={"description": "Joints and bones"},
    )
    assert upd_res.status_code == 200
    assert upd_res.json()["description"] == "Joints and bones"

    # 4. Delete
    del_res = masters_client.delete(f"/masters/departments/{dept_id}")
    assert del_res.status_code == 204


def test_shift_types_and_holidays_crud(masters_client):
    # Department for shift
    wing_res = masters_client.post("/masters/wings", json={"name": "Emergency Wing", "code": "EW"})
    dept_res = masters_client.post(
        "/masters/departments",
        json={"wing_id": wing_res.json()["id"], "name": "Emergency", "code": "EMERG"},
    )
    dept_id = dept_res.json()["id"]

    # 1. Shift type
    shift_res = masters_client.post(
        "/masters/shift-types",
        json={
            "department_id": dept_id,
            "name": "Morning Shift",
            "start_time": "08:00",
            "end_time": "16:00",
        },
    )
    assert shift_res.status_code == 201
    shift_id = shift_res.json()["id"]

    list_shifts = masters_client.get(f"/masters/shift-types?department_id={dept_id}")
    assert list_shifts.status_code == 200
    assert len(list_shifts.json()) == 1

    upd_shift = masters_client.put(
        f"/masters/shift-types/{shift_id}",
        json={"name": "Early Morning Shift"},
    )
    assert upd_shift.status_code == 200

    # 2. Holiday
    holiday_res = masters_client.post(
        "/masters/holidays",
        json={"name": "New Year", "holiday_date": "2026-01-01", "is_recurring": True},
    )
    assert holiday_res.status_code == 201
    holiday_id = holiday_res.json()["id"]

    list_holidays = masters_client.get("/masters/holidays")
    assert list_holidays.status_code == 200
    assert any(h["id"] == holiday_id for h in list_holidays.json())

    del_holiday = masters_client.delete(f"/masters/holidays/{holiday_id}")
    assert del_holiday.status_code == 204

    del_shift = masters_client.delete(f"/masters/shift-types/{shift_id}")
    assert del_shift.status_code == 204


def test_appointment_types_and_suppliers_crud(masters_client):
    # 1. Appointment type
    apt_res = masters_client.post(
        "/masters/appointment-types",
        json={"name": "Routine Checkup", "slot_duration_minutes": 20},
    )
    assert apt_res.status_code == 201
    apt_id = apt_res.json()["id"]

    apt_list = masters_client.get("/masters/appointment-types")
    assert apt_list.status_code == 200
    assert any(a["id"] == apt_id for a in apt_list.json())

    apt_del = masters_client.delete(f"/masters/appointment-types/{apt_id}")
    assert apt_del.status_code == 204

    # 2. Supplier
    supp_res = masters_client.post(
        "/masters/suppliers",
        json={"name": "MedEquip Supplies", "contact_person": "Bob", "phone": "9876543210"},
    )
    assert supp_res.status_code == 201
    supp_id = supp_res.json()["id"]

    supp_list = masters_client.get("/masters/suppliers")
    assert supp_list.status_code == 200
    assert any(s["id"] == supp_id for s in supp_list.json())

    supp_del = masters_client.delete(f"/masters/suppliers/{supp_id}")
    assert supp_del.status_code == 204


def test_wards_and_rooms_crud(masters_client):
    # 1. Ward
    ward_res = masters_client.post(
        "/masters/wards",
        json={"name": "ICU Ward A", "ward_type": "icu", "bed_charge_per_day": 2500},
    )
    assert ward_res.status_code == 201
    ward_id = ward_res.json()["id"]

    ward_list = masters_client.get("/masters/wards")
    assert ward_list.status_code == 200
    assert any(w["id"] == ward_id for w in ward_list.json())

    # 2. Room
    room_res = masters_client.post(
        "/masters/rooms",
        json={"ward_id": ward_id, "room_code": "ICU-101", "bed_count": 2},
    )
    assert room_res.status_code == 201
    room_id = room_res.json()["id"]

    room_list = masters_client.get("/masters/rooms")
    assert room_list.status_code == 200
    assert any(r["id"] == room_id for r in room_list.json())

    room_del = masters_client.delete(f"/masters/rooms/{room_id}")
    assert room_del.status_code == 204

    ward_del = masters_client.delete(f"/masters/wards/{ward_id}")
    assert ward_del.status_code == 204


def test_ot_rooms_and_consultation_pricing(masters_client, masters_db):
    # Setup prerequisites
    wing_res = masters_client.post("/masters/wings", json={"name": "Surgery Wing", "code": "SURG"})
    wing_id = wing_res.json()["id"]

    dept_res = masters_client.post(
        "/masters/departments",
        json={"wing_id": wing_id, "name": "General Surgery", "code": "GS"},
    )
    dept_id = dept_res.json()["id"]

    # 1. OT Room
    ot_res = masters_client.post(
        "/masters/ot-rooms",
        json={
            "wing_id": wing_id,
            "department_id": dept_id,
            "code": "OT-01",
            "name": "Main Operation Theatre",
            "base_ot_charge": 5000.0,
        },
    )
    assert ot_res.status_code == 201
    ot_id = ot_res.json()["id"]

    ot_list = masters_client.get("/masters/ot-rooms")
    assert ot_list.status_code == 200
    assert any(o["id"] == ot_id for o in ot_list.json())

    # 2. Consultation Pricing
    apt_res = masters_client.post(
        "/masters/appointment-types",
        json={"name": "Specialist Consultation", "slot_duration_minutes": 30},
    )
    apt_id = apt_res.json()["id"]

    pricing_res = masters_client.post(
        "/masters/consultation-pricing",
        json={
            "wing_id": wing_id,
            "department_id": dept_id,
            "doctor_id": str(masters_client.admin_user_id),
            "appointment_type_id": apt_id,
            "consultation_fee": 800.0,
            "followup_free_days": 7,
        },
    )
    assert pricing_res.status_code == 201
    pricing_id = pricing_res.json()["id"]

    pricing_list = masters_client.get("/masters/consultation-pricing")
    assert pricing_list.status_code == 200
    assert any(p["id"] == pricing_id for p in pricing_list.json())

    del_pricing = masters_client.delete(f"/masters/consultation-pricing/{pricing_id}")
    assert del_pricing.status_code == 204

    del_ot = masters_client.delete(f"/masters/ot-rooms/{ot_id}")
    assert del_ot.status_code == 204
