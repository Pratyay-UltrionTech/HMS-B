"""
Corrective regression test suite for HMS Admin + Masters.
Verifies all data integrity, operational correctness, and business rules:
- Multi-department staff editing & default shift validation
- Multi-role custom field backend validation
- Last active hospital admin protection
- Clinical history protection from staff deletion (deactivation required)
- Occupied room & ward deactivation rejection
- Clinically empty room inventory deactivation
- Shift deactivation protection (default shift & future roster)
- Consultation pricing exact location matching & cross-pair prevention
- Shared recurring holiday resolution (including Feb 29 leap year)
- Masters audit logging across all entity mutations
- Operational shift roster contract (eligible departments & operational dept)
"""

from __future__ import annotations

from datetime import date, time, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.conftest
from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.admin.api.admin_api import router as admin_router
from modules.masters.api.masters_api import router as masters_router
from modules.admin.actions.admin_actions import AdminActions
from modules.admin.contracts.admin_contracts import (
    HospitalUserCreate,
    HospitalUserUpdate,
    ShiftRosterAssign,
)
from modules.masters.actions.masters_actions import MastersActions
from modules.masters.contracts.masters_contracts import (
    DepartmentCreate,
    ShiftTypeCreate,
    ShiftTypeUpdate,
    HolidayCreate,
    WardCreate,
    WardUpdate,
    RoomCreate,
    RoomUpdate,
    OtRoomCreate,
    AppointmentTypeCreate,
    AppointmentTypeUpdate,
    ConsultationPricingCreate,
    SupplierCreate,
    WingCreate,
)
from modules.doctors.entities.doctor import (
    CustomFieldType,
    HospitalUser,
    HospitalUserDepartment,
    HospitalUserRole,
    RoleCustomField,
    ShiftType,
    StaffDailyShift,
    StaffRole,
    Holiday,
)
from modules.masters.entities.organization_entities import Department, Supplier, Wing
from modules.appointments.entities.appointment_type import AppointmentType
from modules.appointments.db.pricing_reader import PricingReader
from modules.beds.entities.bed import Bed, Room, Ward, WardType
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from shared.audit import AuditLog
from shared.holidays import holiday_matches_date, get_effective_holiday
from modules.doctors.services.effective_availability_service import EffectiveDoctorAvailabilityService
from shared.auth import (
    get_current_user,
    get_hospital_context,
    require_hospital_admin,
    require_hospital_user,
    require_permission,
)


@pytest.fixture
def test_db():
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
def hospital(test_db):
    h = Hospital(
        id=uuid4(),
        hospital_id="HOSP-CORRECTIVE-01",
        name="Corrective Test Hospital",
        address="100 Main St",
        phone="1234567890",
        email="test@hospital.com",
        password_hash="fakehash",
        is_active=True,
    )
    test_db.add(h)
    test_db.commit()
    return h


@pytest.fixture
def admin_role(test_db, hospital):
    r = StaffRole(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Hospital Administrator",
        code="HOSPITAL_ADMIN",
        is_admin_role=True,
        is_active=True,
    )
    test_db.add(r)
    test_db.commit()
    return r


@pytest.fixture
def admin_user(test_db, hospital, admin_role):
    u = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=admin_role.id,
        name="Primary Admin",
        phone="9876543210",
        email="admin@hospital.com",
        password_hash="fakehash",
        is_active=True,
    )
    test_db.add(u)
    test_db.commit()
    return u


def test_recurring_holiday_shared_resolver():
    """Section 19 & 34: Shared recurring holiday resolution including Feb 29 leap year."""
    # 1. Recurring Dec 25
    xmas = Holiday(
        id=uuid4(),
        hospital_id=uuid4(),
        name="Christmas",
        holiday_date=date(2025, 12, 25),
        is_recurring=True,
    )
    # Same year
    assert holiday_matches_date(xmas, date(2025, 12, 25)) is True
    # One year later
    assert holiday_matches_date(xmas, date(2026, 12, 25)) is True
    # Ten years later
    assert holiday_matches_date(xmas, date(2035, 12, 25)) is True
    # Non-matching day
    assert holiday_matches_date(xmas, date(2026, 12, 24)) is False

    # 2. Non-recurring holiday
    special = Holiday(
        id=uuid4(),
        hospital_id=uuid4(),
        name="Special Event",
        holiday_date=date(2025, 10, 10),
        is_recurring=False,
    )
    assert holiday_matches_date(special, date(2025, 10, 10)) is True
    assert holiday_matches_date(special, date(2026, 10, 10)) is False

    # 3. Leap year Feb 29 recurring
    leap_holiday = Holiday(
        id=uuid4(),
        hospital_id=uuid4(),
        name="Leap Day Festival",
        holiday_date=date(2024, 2, 29),
        is_recurring=True,
    )
    # Leap year 2024 matches 29th
    assert holiday_matches_date(leap_holiday, date(2024, 2, 29)) is True
    assert holiday_matches_date(leap_holiday, date(2024, 2, 28)) is False
    # Non-leap year 2025 does NOT automatically treat Feb 28 as the holiday
    assert holiday_matches_date(leap_holiday, date(2025, 2, 28)) is False
    # Leap year 2028 matches 29th
    assert holiday_matches_date(leap_holiday, date(2028, 2, 29)) is True
    assert holiday_matches_date(leap_holiday, date(2028, 2, 28)) is False


def test_doctor_availability_uses_shared_recurring_holiday(test_db, hospital):
    """Section 19: EffectiveDoctorAvailabilityService matches recurring holiday."""
    h = Holiday(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Annual Foundation Day",
        holiday_date=date(2025, 6, 15),
        is_recurring=True,
    )
    test_db.add(h)
    test_db.commit()

    service = EffectiveDoctorAvailabilityService(test_db, hospital.id)
    # Query one year later: 2026-06-15
    matched = service.get_holiday(date(2026, 6, 15))
    assert matched is not None
    assert matched.name == "Annual Foundation Day"

    # Query unrelated date
    assert service.get_holiday(date(2026, 6, 16)) is None


def test_last_active_admin_protection(test_db, hospital, admin_user, admin_role):
    """Section 11 & 31: Prevent removing or deactivating the last active Hospital Admin."""
    actions = AdminActions(test_db, hospital.id)

    # Attempt to deactivate the only active admin
    with pytest.raises(HTTPException) as exc:
        actions.update_user(
            admin_user.id,
            HospitalUserUpdate(is_active=False),
        )
    assert exc.value.status_code == 409
    assert "At least one active Hospital Administrator must remain" in exc.value.detail

    # Create a second admin
    second_admin = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=admin_role.id,
        name="Second Admin",
        phone="9876543211",
        email="second.admin@hospital.com",
        password_hash="fake",
        is_active=True,
    )
    test_db.add(second_admin)
    test_db.commit()

    # Now deactivating the first admin succeeds because second remains
    updated = actions.update_user(
        admin_user.id,
        HospitalUserUpdate(is_active=False),
    )
    assert updated.is_active is False


def test_staff_deletion_clinical_history_protection(test_db, hospital, admin_user):
    """Section 10 & 31: Physical deletion blocked when clinical history exists; deactivation required."""
    actions = AdminActions(test_db, hospital.id)
    role = StaffRole(id=uuid4(), hospital_id=hospital.id, name="Attending Doctor", is_active=True)
    test_db.add(role)
    test_db.commit()

    doctor = actions.create_user(
        HospitalUserCreate(
            name="Dr. Clinical Staff",
            email="dr.clinical@hospital.com",
            phone="9123456780",
            password="Password123!",
            role_id=role.id,
        )
    )

    # Add an appointment referencing this doctor
    pat = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid="UHID-DEL-01",
        name="Test Patient",
        mobile="9876543200",
    )
    test_db.add(pat)
    test_db.flush()

    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=pat.id,
        op_id="OP-DEL-01",
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        purpose="Consultation",
        status=AppointmentStatus.scheduled,
    )
    test_db.add(appt)
    test_db.commit()

    # Attempt hard deletion -> must be rejected with 409
    with pytest.raises(HTTPException) as exc:
        actions.delete_user(doctor.id)
    assert exc.value.status_code == 409
    assert "appointments" in exc.value.detail.lower() or "deactivate" in exc.value.detail.lower()

    # Appointment must still exist!
    saved_appt = test_db.query(Appointment).filter(Appointment.id == appt.id).first()
    assert saved_appt is not None

    # Normal deactivation must succeed and increment token_version
    db_doc = test_db.query(HospitalUser).filter(HospitalUser.id == doctor.id).first()
    old_version = db_doc.token_version
    deactivated = actions.update_user(doctor.id, HospitalUserUpdate(is_active=False))
    assert deactivated.is_active is False
    test_db.refresh(db_doc)
    assert db_doc.token_version > old_version


def test_multi_department_default_shift_and_roster(test_db, hospital, admin_user):
    """Section 4, 5, 7, 8, 30: Multi-department staff editing, default shifts, and roster."""
    admin_actions = AdminActions(test_db, hospital.id)
    masters_actions = MastersActions(test_db, hospital.id)

    # 1. Setup departments A, B, C, D
    wing = masters_actions.create_wing(WingCreate(name="Main Hospital Wing"))
    dept_a = masters_actions.create_department(DepartmentCreate(name="Cardiology", code="CARD", wing_id=wing.id))
    dept_b = masters_actions.create_department(DepartmentCreate(name="Emergency", code="EMERG", wing_id=wing.id))
    dept_c = masters_actions.create_department(DepartmentCreate(name="ICU", code="ICU", wing_id=wing.id))
    dept_d = masters_actions.create_department(DepartmentCreate(name="Neurology", code="NEUR", wing_id=wing.id))

    shift_b = masters_actions.create_shift_type(
        ShiftTypeCreate(
            department_id=dept_b.id,
            name="Emergency 24H",
            start_time=time(0, 0),
            end_time=time(23, 59),
        )
    )
    shift_d = masters_actions.create_shift_type(
        ShiftTypeCreate(
            department_id=dept_d.id,
            name="Neuro Day",
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
    )

    role = StaffRole(id=uuid4(), hospital_id=hospital.id, name="Doctor", is_active=True)
    test_db.add(role)
    test_db.commit()

    # 2. Create staff with Primary Dept A, Secondary B and C
    staff = admin_actions.create_user(
        HospitalUserCreate(
            name="Dr. Multi Dept",
            email="multi.dept@hospital.com",
            phone="9876543222",
            password="Password123!",
            role_id=role.id,
            department_id=dept_a.id,
            department_ids=[dept_a.id, dept_b.id, dept_c.id],
        )
    )

    # 3. Default shift from B (assigned) is ALLOWED
    updated = admin_actions.update_user(
        staff.id,
        HospitalUserUpdate(shift_id=shift_b.id),
    )
    assert updated.shift_id == shift_b.id

    # 4. Default shift from D (unassigned) is REJECTED
    with pytest.raises(HTTPException) as exc:
        admin_actions.update_user(
            staff.id,
            HospitalUserUpdate(shift_id=shift_d.id),
        )
    assert exc.value.status_code == 400
    assert "selected department" in exc.value.detail.lower()

    # 5. Roster contract: eligible_department_ids contains A, B, C
    roster_date = date(2026, 9, 20)
    roster = admin_actions.get_shift_roster(roster_date)
    staff_entry = next(e for e in roster.entries if e.user_id == staff.id)
    assert set(staff_entry.eligible_department_ids) == {dept_a.id, dept_b.id, dept_c.id}
    assert staff_entry.primary_department_id == dept_a.id

    # 6. Operational department display: when assigned a shift from Dept B,
    # the operational roster department is Dept B, while primary remains A!
    entry_updated = admin_actions.assign_shift_roster(
        ShiftRosterAssign(
            roster_date=roster_date,
            user_id=staff.id,
            shift_id=shift_b.id,
            status="on_duty",
        )
    )
    assert entry_updated.department_id == dept_b.id
    assert entry_updated.department_name == "Emergency"
    assert entry_updated.primary_department_id == dept_a.id


def test_multi_role_custom_field_backend_validation(test_db, hospital):
    """Section 13 & 28: Backend validation requires custom fields for ALL assigned roles."""
    admin_actions = AdminActions(test_db, hospital.id)

    role_a = StaffRole(id=uuid4(), hospital_id=hospital.id, name="Doctor Role", is_active=True)
    role_b = StaffRole(id=uuid4(), hospital_id=hospital.id, name="Safety Officer", is_active=True)
    test_db.add_all([role_a, role_b])
    test_db.flush()

    field_a = RoleCustomField(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role_a.id,
        label="Medical Reg #",
        field_key="med_reg_num",
        field_type=CustomFieldType.text,
        is_required=True,
        sort_order=0,
    )
    field_b = RoleCustomField(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role_b.id,
        label="Safety Cert #",
        field_key="safety_cert_num",
        field_type=CustomFieldType.text,
        is_required=True,
        sort_order=0,
    )
    test_db.add_all([field_a, field_b])
    test_db.commit()

    # 1. Missing field_b should be rejected with 422
    with pytest.raises(HTTPException) as exc:
        admin_actions.create_user(
            HospitalUserCreate(
                name="Dr. Dual Role",
                email="dual.role@hospital.com",
                phone="9876543233",
                password="Password123!",
                role_id=role_a.id,
                role_ids=[role_a.id, role_b.id],
                custom_values={"med_reg_num": "MCI-9988"},
            )
        )
    assert exc.value.status_code == 422
    assert "safety_cert_num" in exc.value.detail

    # 2. Supplying both succeeds
    user = admin_actions.create_user(
        HospitalUserCreate(
            name="Dr. Dual Role",
            email="dual.role@hospital.com",
            phone="9876543233",
            password="Password123!",
            role_id=role_a.id,
            role_ids=[role_a.id, role_b.id],
            custom_values={"med_reg_num": "MCI-9988", "safety_cert_num": "SAFE-101"},
        )
    )
    assert user.custom_values["med_reg_num"] == "MCI-9988"
    assert user.custom_values["safety_cert_num"] == "SAFE-101"


def test_occupied_room_and_ward_deactivation_prevention(test_db, hospital):
    """Section 15, 16, 32: Occupied room/ward deactivation rejected; empty room deactivates inventory."""
    masters_actions = MastersActions(test_db, hospital.id)

    ward = masters_actions.create_ward(
        WardCreate(name="ICU Ward", ward_type=WardType.icu)
    )
    room = masters_actions.create_room(
        RoomCreate(ward_id=ward.id, room_code="ICU-101", bed_count=2)
    )

    # 1. Mark one bed as occupied
    beds = test_db.query(Bed).filter(Bed.room_id == room.id).all()
    assert len(beds) == 2
    beds[0].is_occupied = True
    test_db.commit()

    # 2. Attempt to deactivate occupied room -> 409 Conflict
    with pytest.raises(HTTPException) as exc:
        masters_actions.update_room(room.id, RoomUpdate(is_active=False))
    assert exc.value.status_code == 409
    assert "occupied" in exc.value.detail.lower()

    # 3. Attempt to deactivate ward containing occupied room -> 409 Conflict
    with pytest.raises(HTTPException) as exc:
        masters_actions.update_ward(ward.id, WardUpdate(is_active=False))
    assert exc.value.status_code == 409
    assert "active room" in exc.value.detail.lower() or "occupied" in exc.value.detail.lower()

    # 4. Discharge / free the bed
    beds[0].is_occupied = False
    test_db.commit()

    # 5. Empty room deactivation succeeds and sets its beds to inactive
    updated_room = masters_actions.update_room(room.id, RoomUpdate(is_active=False))
    assert updated_room.is_active is False
    beds_after = test_db.query(Bed).filter(Bed.room_id == room.id).all()
    assert all(b.is_active is False for b in beds_after)

    # 6. Now ward deactivation succeeds
    updated_ward = masters_actions.update_ward(ward.id, WardUpdate(is_active=False))
    assert updated_ward.is_active is False


def test_shift_deactivation_protection(test_db, hospital):
    """Section 6 & 32: Deactivating a shift used as staff default or active roster returns 409."""
    masters_actions = MastersActions(test_db, hospital.id)
    admin_actions = AdminActions(test_db, hospital.id)

    wing = masters_actions.create_wing(WingCreate(name="General Wing"))
    dept = masters_actions.create_department(DepartmentCreate(name="General Medicine", wing_id=wing.id))
    shift = masters_actions.create_shift_type(
        ShiftTypeCreate(
            department_id=dept.id,
            name="General Morning",
            start_time=time(8, 0),
            end_time=time(16, 0),
        )
    )
    role = StaffRole(id=uuid4(), hospital_id=hospital.id, name="Staff", is_active=True)
    test_db.add(role)
    test_db.commit()

    staff = admin_actions.create_user(
        HospitalUserCreate(
            name="Nurse Mary",
            email="nurse.mary@hospital.com",
            phone="9876543244",
            password="Password123!",
            role_id=role.id,
            department_id=dept.id,
            shift_id=shift.id,
        )
    )

    # 1. Attempt to deactivate shift while it's the default shift for active staff -> 409
    with pytest.raises(HTTPException) as exc:
        masters_actions.update_shift_type(shift.id, ShiftTypeUpdate(is_active=False))
    assert exc.value.status_code == 409
    assert "default shift" in exc.value.detail.lower()

    # Reassign staff default shift
    admin_actions.update_user(staff.id, HospitalUserUpdate(shift_id=None))

    # 2. Deactivating shift now succeeds
    deactivated = masters_actions.update_shift_type(shift.id, ShiftTypeUpdate(is_active=False))
    assert deactivated.is_active is False


def test_pricing_exact_location_matching(test_db, hospital):
    """Section 18 & 33: find_pricing strictly matches requested wing and department."""
    masters_actions = MastersActions(test_db, hospital.id)

    wing_a = masters_actions.create_wing(WingCreate(name="Wing A"))
    wing_b = masters_actions.create_wing(WingCreate(name="Wing B"))
    dept_a = masters_actions.create_department(DepartmentCreate(name="Cardiology", wing_id=wing_a.id))
    dept_b = masters_actions.create_department(DepartmentCreate(name="Neurology", wing_id=wing_b.id))
    appt_type = masters_actions.create_appointment_type(AppointmentTypeCreate(name="Consultation"))

    doctor_id = uuid4()
    role = StaffRole(id=uuid4(), hospital_id=hospital.id, name="Doctor", is_active=True)
    doc_user = HospitalUser(
        id=doctor_id,
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. Specialist",
        phone="9876543255",
        email="specialist@hospital.com",
        password_hash="fake",
        department_id=dept_a.id,
        is_active=True,
    )
    test_db.add_all([role, doc_user])
    test_db.commit()

    # Create pricing ONLY for Wing B / Neurology (Fee: 1000)
    p_b = masters_actions.create_consultation_pricing(
        ConsultationPricingCreate(
            doctor_id=doctor_id,
            wing_id=wing_b.id,
            department_id=dept_b.id,
            appointment_type_id=appt_type.id,
            consultation_fee=1000.0,
        )
    )

    pricing_reader = PricingReader(test_db, hospital.id)

    # 1. Requesting Wing B / Neurology returns fee 1000
    matched = pricing_reader.find_pricing(
        doctor_id=doctor_id,
        appt_type_id=appt_type.id,
        wing_id=wing_b.id,
        department_id=dept_b.id,
    )
    assert matched is not None
    assert matched["id"] == p_b.id

    # 2. Requesting Wing A / Cardiology MUST RETURN NONE! Never silently return Wing B's fee!
    mismatch = pricing_reader.find_pricing(
        doctor_id=doctor_id,
        appt_type_id=appt_type.id,
        wing_id=wing_a.id,
        department_id=dept_a.id,
    )
    assert mismatch is None

    # 3. Requesting Wing A with unknown department MUST NOT return Wing B's pricing
    mismatch_wing = pricing_reader.find_pricing(
        doctor_id=doctor_id,
        appt_type_id=appt_type.id,
        wing_id=wing_a.id,
        department_id=None,
    )
    assert mismatch_wing is None


def test_masters_audit_logs_for_all_entities(test_db, hospital):
    """Section 20 & 35: All master entity mutations emit audit records."""
    masters_actions = MastersActions(test_db, hospital.id, actor={"sub": "admin", "name": "Admin Tester"})

    # Shift Type
    wing = masters_actions.create_wing(WingCreate(name="Pathology Wing"))
    dept = masters_actions.create_department(DepartmentCreate(name="Pathology", wing_id=wing.id))
    shift = masters_actions.create_shift_type(
        ShiftTypeCreate(department_id=dept.id, name="Day Shift", start_time=time(9, 0), end_time=time(17, 0))
    )
    # Holiday
    holiday = masters_actions.create_holiday(
        HolidayCreate(name="New Year", holiday_date=date(2027, 1, 1), is_recurring=True)
    )
    # Appointment Type
    appt_type = masters_actions.create_appointment_type(AppointmentTypeCreate(name="General OPD"))
    # Ward
    ward = masters_actions.create_ward(WardCreate(name="Maternity Ward", ward_type=WardType.general))
    # Room
    room = masters_actions.create_room(RoomCreate(ward_id=ward.id, room_code="MAT-01", bed_count=1))
    # Supplier
    supplier = masters_actions.create_supplier(SupplierCreate(name="Med Supplies Ltd"))

    test_db.commit()

    # Query audit logs
    audits = test_db.query(AuditLog).filter(AuditLog.hospital_id == hospital.id).all()
    entity_types = {a.entity_type for a in audits}

    assert "shift_type" in entity_types
    assert "holiday" in entity_types
    assert "appointment_type" in entity_types
    assert "ward" in entity_types
    assert "room" in entity_types
    assert "supplier" in entity_types
