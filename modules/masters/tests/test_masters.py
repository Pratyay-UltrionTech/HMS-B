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

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.appointments.entities.appointment_type import AppointmentType
from modules.beds.entities.bed import Bed, Room, Ward, WardType
from modules.billing.entities.billing_entities import ConsultationPricing
from modules.clinical_records.entities.clinical_record import Prescription
from modules.critical_care.entities.critical_care_entities import CodeBlueIncident, CodeBlueStatus
from modules.doctors.entities.doctor import Holiday, HospitalUser, ShiftType, StaffRole
from modules.masters.api.masters_api import router as masters_router
from modules.masters.entities.organization_entities import Department, Supplier, Wing
from modules.ot.entities.ot_entities import OtRoom
from modules.tenancy.entities.hospital import Hospital
from shared.auth import (
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
        "hospital_uuid": str(hospital_id),
        "role": "hospital_admin",
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

    # Consultation pricing requires a real eligible doctor, even for an admin caller.
    admin = masters_db.get(HospitalUser, masters_client.admin_user_id)
    doctor_role = StaffRole(hospital_id=masters_client.hospital_id, name="Doctor")
    masters_db.add(doctor_role)
    masters_db.flush()
    admin.role_id = doctor_role.id
    masters_db.commit()
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


def test_room_capacity_is_transactional(masters_client, masters_db):
    ward = masters_client.post('/masters/wards', json={'name': 'Capacity ward', 'ward_type': 'general'}).json()
    room = masters_client.post('/masters/rooms', json={'ward_id': ward['id'], 'room_code': 'CAP', 'bed_count': 2}).json()
    beds = masters_db.query(Bed).filter(Bed.room_id == UUID(room['id'])).all()
    assert len(beds) == 2
    assert masters_client.put(f"/masters/rooms/{room['id']}", json={'bed_count': 3}).status_code == 200
    beds = masters_db.query(Bed).filter(Bed.room_id == UUID(room['id'])).all()
    assert len(beds) == 3
    for bed in beds:
        bed.is_occupied = True
    masters_db.commit()
    rejected = masters_client.put(f"/masters/rooms/{room['id']}", json={'bed_count': 1})
    assert rejected.status_code == 409
    assert masters_db.get(Room, UUID(room['id'])).bed_count == 3
    for bed in beds:
        bed.is_occupied = False
    masters_db.commit()
    assert masters_client.put(f"/masters/rooms/{room['id']}", json={'bed_count': 1}).status_code == 200
    assert masters_db.query(Bed).filter(Bed.room_id == UUID(room['id'])).count() == 1


def test_ward_rejects_mismatched_department(masters_client):
    wing = masters_client.post('/masters/wings', json={'name': 'One'}).json()
    other = masters_client.post('/masters/wings', json={'name': 'Two'}).json()
    dept = masters_client.post('/masters/departments', json={'wing_id': wing['id'], 'name': 'Dept'}).json()
    response = masters_client.post('/masters/wards', json={'name': 'Bad', 'ward_type': 'general', 'wing_id': other['id'], 'department_id': dept['id']})
    assert response.status_code == 422


def test_insurance_get_is_read_only_and_inactive_manageable(masters_client, masters_db):
    from modules.masters.entities.insurance_entities import InsuranceProvider
    assert masters_client.get('/masters/insurance').json() == []
    assert masters_db.query(InsuranceProvider).count() == 0
    item = masters_client.post('/masters/insurance', json={'code': ' ins-1 ', 'name': 'Provider', 'is_active': False}).json()
    assert item['code'] == 'INS-1'
    assert len(masters_client.get('/masters/insurance?active=false').json()) == 1
    assert masters_client.get('/masters/insurance?active=true').json() == []
    assert masters_client.post('/masters/insurance', json={'code': 'Ins-1', 'name': 'Duplicate'}).status_code == 409


def test_room_move_and_history_safety(masters_client, masters_db):
    from modules.inpatient.entities.admission import Admission, AdmissionStatus
    ward = masters_client.post('/masters/wards', json={'name': 'Original', 'ward_type': 'general'}).json()
    other = masters_client.post('/masters/wards', json={'name': 'Destination', 'ward_type': 'general'}).json()
    room = masters_client.post('/masters/rooms', json={'ward_id': ward['id'], 'room_code': 'MOVE', 'bed_count': 2}).json()
    assert masters_client.put(f"/masters/rooms/{room['id']}", json={'ward_id': other['id']}).status_code == 200
    beds = masters_db.query(Bed).filter(Bed.room_id == UUID(room['id'])).all()
    assert all(bed.ward_id == UUID(other['id']) for bed in beds)
    # Discharged history still protects bed and room identifiers from deletion/moving.
    masters_db.add(Admission(hospital_id=masters_client.hospital_id, patient_id=uuid4(), ward_id=UUID(other['id']), room_id=UUID(room['id']), bed_id=beds[0].id, status=AdmissionStatus.discharged))
    masters_db.commit()
    assert masters_client.put(f"/masters/rooms/{room['id']}", json={'ward_id': ward['id']}).status_code == 409
    assert masters_client.delete(f"/masters/rooms/{room['id']}").status_code == 409
    assert masters_client.put(f"/masters/rooms/{room['id']}", json={'bed_count': 1}).status_code == 200
    assert masters_db.get(Bed, beds[0].id) is not None


def test_master_delete_dependency_guard(masters_client):
    wing = masters_client.post('/masters/wings', json={'name': 'Dependent wing'}).json()
    dept = masters_client.post('/masters/departments', json={'wing_id': wing['id'], 'name': 'Dependent department'}).json()
    assert masters_client.delete(f"/masters/wings/{wing['id']}").status_code == 409
    masters_client.post('/masters/shift-types', json={'department_id': dept['id'], 'name': 'Linked shift', 'start_time': '09:00', 'end_time': '17:00'})
    assert masters_client.delete(f"/masters/departments/{dept['id']}").status_code == 409


def test_pricing_and_beds_use_secondary_doctor_role(masters_client, masters_db):
    from modules.doctors.entities.doctor import HospitalUserRole
    from modules.beds.api.beds_api import _list_staff_doctors
    wing = masters_client.post('/masters/wings', json={'name': 'Pricing wing'}).json()
    other = masters_client.post('/masters/wings', json={'name': 'Wrong wing'}).json()
    dept = masters_client.post('/masters/departments', json={'wing_id': wing['id'], 'name': 'Pricing dept'}).json()
    appointment = masters_client.post('/masters/appointment-types', json={'name': 'Pricing type'}).json()
    payload = {'wing_id': wing['id'], 'department_id': dept['id'], 'doctor_id': str(masters_client.admin_user_id), 'appointment_type_id': appointment['id'], 'consultation_fee': 50}
    assert masters_client.post('/masters/consultation-pricing', json=payload).status_code == 422
    role = StaffRole(hospital_id=masters_client.hospital_id, name='Doctor')
    masters_db.add(role)
    masters_db.flush()
    masters_db.add(HospitalUserRole(hospital_id=masters_client.hospital_id, user_id=masters_client.admin_user_id, role_id=role.id))
    masters_db.commit()
    assert masters_client.post('/masters/consultation-pricing', json=payload).status_code == 201
    payload['wing_id'] = other['id']
    assert masters_client.post('/masters/consultation-pricing', json=payload).status_code == 422
    assert [item['id'] for item in _list_staff_doctors(masters_db, masters_client.hospital_id)] == [str(masters_client.admin_user_id)]


def test_master_events_publish_after_commit_and_rollback_discards(masters_client, masters_db, monkeypatch):
    from shared.sync.events import sync_broker
    from sqlalchemy.exc import IntegrityError
    published = []
    def publish(hospital, entity, action, *args, **kwargs):
        assert not masters_db.in_transaction() or not masters_db.get_transaction().is_active
        published.append((entity, action))
    monkeypatch.setattr(sync_broker, 'publish', publish)
    assert masters_client.post('/masters/holidays', json={'name': 'Successful', 'holiday_date': '2026-10-14'}).status_code == 201
    assert published == [('holiday', 'create')]
    published.clear()
    def fail_commit():
        raise IntegrityError('simulated', {}, Exception('failure'))
    monkeypatch.setattr(masters_db, 'commit', fail_commit)
    assert masters_client.post('/masters/holidays', json={'name': 'Rolled back', 'holiday_date': '2026-10-15'}).status_code == 409
    assert published == []
    assert 'pending_sync_events' not in masters_db.info


@pytest.mark.parametrize('count', [1, 30])
def test_master_lists_and_bed_selectors_have_bounded_queries(masters_client, masters_db, count):
    from modules.masters.actions.masters_actions import MastersActions
    from modules.beds.api.beds_api import _list_staff_doctors
    from tests.helpers.query_counter import query_counter
    wing = Wing(hospital_id=masters_client.hospital_id, name='Query wing')
    masters_db.add(wing)
    masters_db.flush()
    dept = Department(hospital_id=masters_client.hospital_id, wing_id=wing.id, name='Query dept')
    masters_db.add(dept)
    masters_db.flush()
    masters_db.add_all([OtRoom(hospital_id=masters_client.hospital_id, department_id=dept.id, code=f'OT{i}', name=f'OT {i}') for i in range(count)])
    masters_db.commit()
    masters_db.expire_all()
    actions = MastersActions(masters_db, masters_client.hospital_id)
    with query_counter(session=masters_db) as queries:
        assert len(actions.list_ot_rooms()) == count
        actions.list_departments()
        actions.list_insurance_providers()
        _list_staff_doctors(masters_db, masters_client.hospital_id)
    assert queries.select_count == 4
    assert queries.insert_count == queries.update_count == queries.delete_count == 0


def test_legacy_bed_hydration_reloads_locked_room_state(masters_client, masters_db):
    from sqlalchemy import update
    from modules.beds.db.beds_repository import BedsRepository
    ward = masters_client.post('/masters/wards', json={'name': 'Stale ward', 'ward_type': 'general'}).json()
    other = masters_client.post('/masters/wards', json={'name': 'Current ward', 'ward_type': 'general'}).json()
    room = Room(hospital_id=masters_client.hospital_id, ward_id=UUID(ward['id']), room_code='STALE', bed_count=4)
    masters_db.add(room)
    masters_db.commit()
    assert room.bed_count == 4
    # A database change bypasses the identity map, as a concurrent writer would.
    masters_db.execute(update(Room).where(Room.id == room.id).values(bed_count=1, ward_id=UUID(other['id'])), execution_options={'synchronize_session': False})
    BedsRepository(masters_db)._ensure_beds_bulk(masters_client.hospital_id, [room])
    beds = masters_db.query(Bed).filter(Bed.room_id == room.id).all()
    assert len(beds) == 1
    assert beds[0].ward_id == UUID(other['id'])
    masters_db.execute(update(Room).where(Room.id == room.id).values(is_active=False, bed_count=5), execution_options={'synchronize_session': False})
    BedsRepository(masters_db)._ensure_beds_bulk(masters_client.hospital_id, [room])
    assert masters_db.query(Bed).filter(Bed.room_id == room.id).count() == 1


@pytest.mark.parametrize('count', [1, 30])
def test_bed_inventory_options_queries_do_not_grow_with_rooms(masters_client, masters_db, count):
    from modules.beds.actions.bed_actions import ListBedOptionsAction
    from tests.helpers.query_counter import query_counter
    ward = Ward(hospital_id=masters_client.hospital_id, name='Bed query ward', ward_type=WardType.general)
    masters_db.add(ward)
    masters_db.flush()
    for i in range(count):
        room = Room(hospital_id=masters_client.hospital_id, ward_id=ward.id, room_code=f'BQ{i}', bed_count=1)
        masters_db.add(room)
        masters_db.flush()
        masters_db.add(Bed(hospital_id=masters_client.hospital_id, room_id=room.id, ward_id=ward.id, bed_code='Bed-1'))
    masters_db.commit()
    masters_db.expire_all()
    with query_counter(session=masters_db) as queries:
        assert len(ListBedOptionsAction(masters_db).execute(masters_client.hospital_id)) == count
    assert queries.select_count == 5
    assert queries.insert_count == queries.update_count == queries.delete_count == 0


def test_masters_reject_cross_hospital_references_and_partial_pair_mismatch(masters_client, masters_db):
    other_hospital = Hospital(hospital_id='OTHER-MASTERS', name='Other hospital', address='Elsewhere', phone='123', email='other@masters.test', password_hash='fake')
    masters_db.add(other_hospital)
    masters_db.flush()
    foreign_wing = Wing(hospital_id=other_hospital.id, name='Foreign wing')
    masters_db.add(foreign_wing)
    masters_db.commit()
    assert masters_client.post('/masters/departments', json={'wing_id': str(foreign_wing.id), 'name': 'Invalid'}).status_code == 404
    wing = masters_client.post('/masters/wings', json={'name': 'Partial wing'}).json()
    other = masters_client.post('/masters/wings', json={'name': 'Partial other wing'}).json()
    dept = masters_client.post('/masters/departments', json={'wing_id': wing['id'], 'name': 'Partial dept'}).json()
    ward = masters_client.post('/masters/wards', json={'name': 'Partial ward', 'ward_type': 'general', 'wing_id': wing['id'], 'department_id': dept['id']}).json()
    assert masters_client.put(f"/masters/wards/{ward['id']}", json={'wing_id': other['id']}).status_code == 422
    assert masters_client.put(f"/masters/departments/{dept['id']}", json={'wing_id': other['id']}).status_code == 409
    assert masters_client.post('/masters/ot-rooms', json={'wing_id': other['id'], 'department_id': dept['id'], 'code': 'WRONG', 'name': 'Invalid'}).status_code == 422
    assert masters_client.put(f"/masters/wards/{ward['id']}", json={'department_id': None}).status_code == 200


def test_code_blue_history_protects_beds_and_wards(masters_client, masters_db):
    from modules.critical_care.entities.critical_care_entities import CodeBlueIncident, CodeBlueStatus
    ward = masters_client.post('/masters/wards', json={'name': 'Code Blue ward', 'ward_type': 'general'}).json()
    room = masters_client.post('/masters/rooms', json={'ward_id': ward['id'], 'room_code': 'CB', 'bed_count': 2}).json()
    beds = masters_db.query(Bed).filter(Bed.room_id == UUID(room['id'])).all()
    for index, bed in enumerate(beds):
        masters_db.add(CodeBlueIncident(hospital_id=masters_client.hospital_id, incident_code=f'CB-{index}', location_description='Historic resuscitation', ward_id=UUID(ward['id']), bed_id=bed.id, activated_by_name='Test staff', status=CodeBlueStatus.concluded))
    masters_db.commit()
    assert masters_client.put(f"/masters/rooms/{room['id']}", json={'bed_count': 1}).status_code == 409
    assert masters_client.delete(f"/masters/rooms/{room['id']}").status_code == 409
    empty_ward = masters_client.post('/masters/wards', json={'name': 'Historical ward only', 'ward_type': 'general'}).json()
    masters_db.add(CodeBlueIncident(hospital_id=masters_client.hospital_id, incident_code='CB-WARD', location_description='Historic ward event', ward_id=UUID(empty_ward['id']), activated_by_name='Test staff', status=CodeBlueStatus.concluded))
    masters_db.commit()
    assert masters_client.delete(f"/masters/wards/{empty_ward['id']}").status_code == 409
