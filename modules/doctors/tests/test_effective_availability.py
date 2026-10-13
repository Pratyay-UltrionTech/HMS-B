"""
Comprehensive tests for EffectiveDoctorAvailabilityService and Cross-Module Scheduling Invariants.

Verifies:
1. Daily roster override (StaffDailyShift.status == 'off' / 'leave') blocks availability and booking.
2. Approved full-day DoctorLeave blocks availability and booking.
3. Partial-day DoctorLeave blocks overlapping slot, permits non-overlapping slot.
4. Appointment interval overlap (not just exact start time) blocks conflicting booking.
5. OT surgeon conflict blocks OPD appointment booking.
6. OPD appointment conflict blocks OT surgery creation.
7. Reschedule enforces the same canonical validator.
8. Hospital holiday blocks availability and booking.
"""

from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from modules.appointments.actions.book_appointment_action import BookAppointmentAction
from modules.appointments.actions.appointment_status_actions import RescheduleAction
from modules.appointments.contracts.appointments_contracts import (
    BookAppointmentRequest,
    RescheduleRequest,
)
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.db.availability_reader import AvailabilityReader
from modules.appointments.db.pricing_reader import PricingReader
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.appointments.exceptions.appointments_exceptions import (
    AppointmentConflictError,
    AppointmentValidationError,
)
from modules.doctors.entities.doctor import (
    DoctorLeave,
    Holiday,
    HospitalUser,
    ShiftType,
    StaffDailyShift,
    StaffRole,
)
from modules.doctors.services.effective_availability_service import (
    EffectiveDoctorAvailabilityService,
)
from modules.ot.actions.ot_actions import CreateSurgeryAction
from modules.ot.contracts.ot_contracts import OtPriority, OtSurgeryCreate
from modules.ot.entities.ot_entities import OtRoom, OtSurgery, OtSurgeryStatus
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from tests.conftest import db_session, hospital


@pytest.fixture
def test_setup(db_session: Session, hospital: Hospital):
    # Setup doctor role
    role = StaffRole(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Surgeon Doctor",
        code="SURGEON",
        is_active=True,
    )
    db_session.add(role)

    # Shift 09:00 - 17:00
    shift = ShiftType(
        id=uuid4(),
        hospital_id=hospital.id,
        department_id=uuid4(),
        name="General Morning",
        start_time=time(9, 0),
        end_time=time(17, 0),
        is_active=True,
    )
    db_session.add(shift)

    # Doctor
    doctor = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. Sarah Jenkins",
        phone="9876543210",
        email=f"sarah.{uuid4().hex[:6]}@hospital.com",
        password_hash="fake",
        specialization="General Surgery",
        shift_id=shift.id,
        is_active=True,
    )
    db_session.add(doctor)

    # Patient
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid=f"UHID-{uuid4().hex[:6].upper()}",
        name="John Doe",
        first_name="John",
        last_name="Doe",
        mobile="9876500001",
        gender="male",
    )
    db_session.add(patient)

    # OT Room
    dept_id = uuid4()
    from modules.masters.entities.organization_entities import Department
    dept = Department(
        id=dept_id,
        hospital_id=hospital.id,
        name="Surgery Dept",
        code=f"SURG-{uuid4().hex[:4]}",
        is_active=True,
    )
    db_session.add(dept)

    ot_room = OtRoom(
        id=uuid4(),
        hospital_id=hospital.id,
        department_id=dept_id,
        code="OT-MAIN",
        name="Main Operation Theatre",
        base_ot_charge=5000.0,
        is_active=True,
    )
    db_session.add(ot_room)

    db_session.commit()

    # Set deterministic UTC timezone for test hospital
    hospital.timezone = "UTC"
    db_session.add(hospital)
    db_session.commit()

    return {
        "hospital": hospital,
        "doctor": doctor,
        "shift": shift,
        "patient": patient,
        "ot_room": ot_room,
        "dept_id": dept_id,
    }


def test_doctor_availability_roster_override_off(db_session: Session, test_setup: dict):
    hospital = test_setup["hospital"]
    doctor = test_setup["doctor"]
    patient = test_setup["patient"]
    query_date = date.today() + timedelta(days=5)

    # Add daily roster marking doctor 'off'
    roster_off = StaffDailyShift(
        id=uuid4(),
        hospital_id=hospital.id,
        user_id=doctor.id,
        roster_date=query_date,
        status="off",
    )
    db_session.add(roster_off)
    db_session.commit()

    # 1. Canonical availability check
    avail_svc = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    daily = avail_svc.get_daily_availability(doctor.id, query_date)
    assert daily.available is False
    assert "off-duty" in (daily.reason or "")

    # 2. Direct appointment booking must fail
    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    pricing_reader = PricingReader(db_session, hospital.id)
    book_action = BookAppointmentAction(db_session, hospital.id, repo, avail_reader, pricing_reader)

    req = BookAppointmentRequest(
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(10, 0),
        visit_type="OPD",
        purpose="General Checkup",
    )
    with pytest.raises(AppointmentConflictError) as exc_info:
        book_action.execute(req, {"sub": "test", "name": "Admin"})
    assert "off-duty" in str(exc_info.value)


def test_doctor_availability_full_day_leave(db_session: Session, test_setup: dict):
    hospital = test_setup["hospital"]
    doctor = test_setup["doctor"]
    patient = test_setup["patient"]
    query_date = date.today() + timedelta(days=6)

    # Approved full day leave (09:00 - 17:00)
    leave = DoctorLeave(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        leave_date=query_date,
        start_time=time(9, 0),
        end_time=time(17, 0),
        reason="Medical Conference",
    )
    db_session.add(leave)
    db_session.commit()

    avail_svc = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    daily = avail_svc.get_daily_availability(doctor.id, query_date)
    assert daily.available is False
    assert "Medical Conference" in (daily.reason or "")

    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    pricing_reader = PricingReader(db_session, hospital.id)
    book_action = BookAppointmentAction(db_session, hospital.id, repo, avail_reader, pricing_reader)

    req = BookAppointmentRequest(
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(11, 0),
        visit_type="OPD",
        purpose="General Checkup",
    )
    with pytest.raises(AppointmentConflictError) as exc_info:
        book_action.execute(req, {"sub": "test", "name": "Admin"})
    assert "Medical Conference" in str(exc_info.value)


def test_doctor_availability_partial_leave(db_session: Session, test_setup: dict):
    hospital = test_setup["hospital"]
    doctor = test_setup["doctor"]
    patient = test_setup["patient"]
    query_date = date.today() + timedelta(days=7)

    # Partial leave 14:00 - 16:00
    leave = DoctorLeave(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        leave_date=query_date,
        start_time=time(14, 0),
        end_time=time(16, 0),
        reason="Dentist appointment",
    )
    db_session.add(leave)
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    pricing_reader = PricingReader(db_session, hospital.id)
    book_action = BookAppointmentAction(db_session, hospital.id, repo, avail_reader, pricing_reader)

    # Slot 14:15 falls in leave -> REJECTED
    req_bad = BookAppointmentRequest(
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(14, 15),
        visit_type="OPD",
        purpose="Checkup",
    )
    with pytest.raises(AppointmentConflictError) as exc_info:
        book_action.execute(req_bad, {"sub": "test", "name": "Admin"})
    assert "leave" in str(exc_info.value).lower()

    # Slot 10:00 is outside leave -> ACCEPTED
    req_good = BookAppointmentRequest(
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(10, 0),
        visit_type="OPD",
        purpose="Checkup",
    )
    booked = book_action.execute(req_good, {"sub": "test", "name": "Admin"})
    assert booked.appointment_time == time(10, 0)


def test_interval_overlap_rejected(db_session: Session, test_setup: dict):
    hospital = test_setup["hospital"]
    doctor = test_setup["doctor"]
    patient = test_setup["patient"]
    query_date = date.today() + timedelta(days=8)

    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    pricing_reader = PricingReader(db_session, hospital.id)
    book_action = BookAppointmentAction(db_session, hospital.id, repo, avail_reader, pricing_reader)

    # Book 10:00 (15 min slot covers 10:00 - 10:15)
    req1 = BookAppointmentRequest(
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(10, 0),
        visit_type="OPD",
        purpose="First Checkup",
    )
    book_action.execute(req1, {"sub": "test", "name": "Admin"})

    # Try booking overlapping slot at 10:10 (10:10 < 10:15) -> Must be rejected by interval overlap!
    req2 = BookAppointmentRequest(
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(10, 10),
        visit_type="OPD",
        purpose="Overlapping Checkup",
    )
    with pytest.raises(AppointmentConflictError) as exc_info:
        book_action.execute(req2, {"sub": "test", "name": "Admin"})
    assert "already has an appointment" in str(exc_info.value)


def test_ot_surgeon_conflict_with_opd(db_session: Session, test_setup: dict):
    hospital = test_setup["hospital"]
    doctor = test_setup["doctor"]
    patient = test_setup["patient"]
    query_date = date.today() + timedelta(days=9)

    # 1. Surgeon has OT surgery at 11:00 UTC (duration 60 mins: 11:00 - 12:00)
    scheduled_dt = datetime.combine(query_date, time(11, 0)).replace(tzinfo=timezone.utc)
    ot = OtSurgery(
        id=uuid4(),
        hospital_id=hospital.id,
        surgery_no=f"SURG-{uuid4().hex[:4]}",
        patient_id=patient.id,
        surgeon_id=doctor.id,
        surgery_type="Appendectomy",
        surgery_category="General",
        priority=OtPriority.elective,
        department_id=test_setup["dept_id"],
        ot_room_id=test_setup["ot_room"].id,
        ot_room=test_setup["ot_room"].name,
        scheduled_at=scheduled_dt,
        duration_minutes=60,
        status=OtSurgeryStatus.scheduled,
    )
    db_session.add(ot)
    db_session.commit()

    # 2. Attempt OPD appointment booking during surgery (11:30) -> REJECTED
    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    pricing_reader = PricingReader(db_session, hospital.id)
    book_action = BookAppointmentAction(db_session, hospital.id, repo, avail_reader, pricing_reader)

    req = BookAppointmentRequest(
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(11, 30),
        visit_type="OPD",
        purpose="Post-Op Consultation",
    )
    with pytest.raises(AppointmentConflictError) as exc_info:
        book_action.execute(req, {"sub": "test", "name": "Admin"})
    assert "OT surgery" in str(exc_info.value)


def test_opd_appointment_blocks_ot_surgery(db_session: Session, test_setup: dict):
    hospital = test_setup["hospital"]
    doctor = test_setup["doctor"]
    patient = test_setup["patient"]
    query_date = date.today() + timedelta(days=10)

    # 1. Doctor has OPD appointment at 14:00 (14:00 - 14:15)
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(14, 0),
        purpose="Consultation",
        status=AppointmentStatus.scheduled,
    )
    db_session.add(appt)
    db_session.commit()

    # 2. Attempt to schedule OT surgery overlapping 14:00 (e.g. 13:30 - 14:30) -> REJECTED
    from fastapi import HTTPException
    ot_create = CreateSurgeryAction(
        db_session, hospital.id, {"sub": "admin", "name": "Admin", "role": "admin"}
    )
    # mock admission/prescription checks in ot_repository if needed
    ot_create.repo.check_active_admission = lambda pid: True
    ot_create.repo.check_prescription_exists = lambda pid: True

    payload = OtSurgeryCreate(
        patient_id=patient.id,
        surgeon_id=doctor.id,
        department_id=test_setup["dept_id"],
        ot_room_id=test_setup["ot_room"].id,
        surgery_type="Cholecystectomy",
        scheduled_at=datetime.combine(query_date, time(13, 30)).replace(tzinfo=timezone.utc),
        duration_minutes=60,
    )

    with pytest.raises(HTTPException) as exc_info:
        ot_create.execute(payload)
    assert exc_info.value.status_code == 409
    assert "already has an appointment" in exc_info.value.detail


def test_reschedule_checks_leave_and_conflict(db_session: Session, test_setup: dict):
    hospital = test_setup["hospital"]
    doctor = test_setup["doctor"]
    patient = test_setup["patient"]
    query_date = date.today() + timedelta(days=11)

    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    reschedule_action = RescheduleAction(db_session, hospital.id, repo, avail_reader)

    # 1. Doctor has leave on query_date from 15:00 - 16:00
    leave = DoctorLeave(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        leave_date=query_date,
        start_time=time(15, 0),
        end_time=time(16, 0),
        reason="Family event",
    )
    db_session.add(leave)

    # 2. Existing scheduled appointment at 10:00
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(10, 0),
        purpose="Regular Checkup",
        status=AppointmentStatus.scheduled,
    )
    db_session.add(appt)
    db_session.commit()

    # 3. Reschedule into leave slot (15:15) -> REJECTED
    req_bad = RescheduleRequest(
        appointment_date=query_date,
        appointment_time=time(15, 15),
    )
    with pytest.raises(AppointmentConflictError) as exc_info:
        reschedule_action.execute(appt.id, req_bad, {"sub": "admin", "name": "Admin"})
    assert "leave" in str(exc_info.value).lower()

    # 4. Reschedule into available slot (11:00) -> SUCCEEDS
    req_good = RescheduleRequest(
        appointment_date=query_date,
        appointment_time=time(11, 0),
    )
    rescheduled = reschedule_action.execute(appt.id, req_good, {"sub": "admin", "name": "Admin"})
    assert rescheduled.appointment_time == time(11, 0)


def test_hospital_holiday_blocks_booking(db_session: Session, test_setup: dict):
    hospital = test_setup["hospital"]
    doctor = test_setup["doctor"]
    patient = test_setup["patient"]
    query_date = date.today() + timedelta(days=12)

    # Hospital Holiday
    holiday = Holiday(
        id=uuid4(),
        hospital_id=hospital.id,
        holiday_date=query_date,
        name="National Founders Day",
    )
    db_session.add(holiday)
    db_session.commit()

    # Availability check
    avail_svc = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    daily = avail_svc.get_daily_availability(doctor.id, query_date)
    assert daily.available is False
    assert "National Founders Day" in (daily.reason or "")

    # Booking attempt
    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    pricing_reader = PricingReader(db_session, hospital.id)
    book_action = BookAppointmentAction(db_session, hospital.id, repo, avail_reader, pricing_reader)

    req = BookAppointmentRequest(
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=query_date,
        appointment_time=time(10, 0),
        visit_type="OPD",
        purpose="Holiday checkup",
    )
    with pytest.raises(AppointmentValidationError) as exc_info:
        book_action.execute(req, {"sub": "admin", "name": "Admin"})
    assert "National Founders Day" in str(exc_info.value)
