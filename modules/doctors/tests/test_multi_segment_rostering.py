"""
Comprehensive tests for Multi-Segment Daily Rostering, Recurring Work Patterns,
Overnight Cross-Midnight Duty, and Conflict Resolution.
"""

from datetime import date, datetime, time, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from modules.admin.actions.admin_actions import AdminActions
from modules.admin.contracts.admin_contracts import (
    RosterDutySegmentCreate,
    RosterDutySegmentUpdate,
    StaffRosterDayUpdate,
    ShiftRosterSeed,
)
from modules.appointments.actions.book_appointment_action import BookAppointmentAction
from modules.appointments.contracts.appointments_contracts import BookAppointmentRequest
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.db.availability_reader import AvailabilityReader
from modules.appointments.db.pricing_reader import PricingReader
from modules.appointments.exceptions.appointments_exceptions import (
    AppointmentConflictError,
    AppointmentValidationError,
)
from modules.doctors.entities.doctor import (
    DoctorLeave,
    HospitalUser,
    ShiftType,
    StaffDailyShift,
    StaffRole,
    StaffRosterDay,
    StaffRosterSegment,
    StaffWorkPattern,
    StaffWorkPatternSegment,
)
from modules.admin.contracts.admin_contracts import (
    StaffWorkPatternUpdate,
    WorkPatternSegmentCreate,
    WorkPatternSegmentItem,
)
from modules.doctors.services.effective_availability_service import (
    DoctorAvailabilityService,
    EffectiveDoctorAvailabilityService,
    StaffScheduleService,
)
from modules.ot.actions.ot_actions import CreateSurgeryAction
from modules.ot.entities.ot_entities import OtRoom, OtSurgery, OtSurgeryStatus
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from tests.conftest import db_session, hospital


@pytest.fixture
def multi_roster_setup(db_session: Session, hospital: Hospital):
    role = StaffRole(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Consultant Specialist",
        code="SPECIALIST",
        is_active=True,
    )
    db_session.add(role)

    from modules.masters.entities.organization_entities import Department
    dept = Department(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Cardiology Department",
        code=f"CARD-{uuid4().hex[:4]}",
        is_active=True,
    )
    db_session.add(dept)

    shift_morning = ShiftType(
        id=uuid4(),
        hospital_id=hospital.id,
        department_id=dept.id,
        name="Morning OPD",
        start_time=time(8, 0),
        end_time=time(12, 0),
        is_active=True,
    )
    shift_evening = ShiftType(
        id=uuid4(),
        hospital_id=hospital.id,
        department_id=dept.id,
        name="Evening OPD",
        start_time=time(16, 0),
        end_time=time(20, 0),
        is_active=True,
    )
    shift_night = ShiftType(
        id=uuid4(),
        hospital_id=hospital.id,
        department_id=dept.id,
        name="Night Emergency",
        start_time=time(23, 0),
        end_time=time(2, 0),
        is_active=True,
    )
    db_session.add_all([shift_morning, shift_evening, shift_night])

    doctor = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. Chandra",
        phone="9876543210",
        email=f"chandra.{uuid4().hex[:6]}@hospital.com",
        password_hash="fake",
        specialization="Cardiology",
        shift_id=shift_morning.id,
        is_active=True,
    )
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid=f"UHID-{uuid4().hex[:6].upper()}",
        name="Test Patient",
        first_name="Test",
        last_name="Patient",
        gender="other",
        mobile="9123456789",
    )
    db_session.add_all([doctor, patient])
    db_session.commit()

    return {
        "doctor": doctor,
        "patient": patient,
        "shift_morning": shift_morning,
        "shift_evening": shift_evening,
        "shift_night": shift_night,
        "role": role,
        "dept": dept,
    }


def test_case_a_single_shift_backward_compatible(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """Case A: Normal one-shift employee works exactly as before."""
    doctor = multi_roster_setup["doctor"]
    service = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    target_date = date(2026, 10, 16)

    segments = service.get_effective_duty_segments(doctor.id, target_date)
    assert len(segments) == 1
    assert segments[0].start_time == time(8, 0)
    assert segments[0].end_time == time(12, 0)
    assert segments[0].duty_kind == "OPD"
    assert segments[0].is_bookable is True


def test_case_b_and_c_multi_segment_with_gaps(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """
    Case B & C: Multi-duty employee:
    08:00–10:00 OPD
    14:00–19:00 Ward Duty
    23:00–02:00 Emergency Duty (cross-midnight)
    Gaps must NOT be working time.
    """
    doctor = multi_roster_setup["doctor"]
    target_date = date(2026, 10, 16)

    roster_day = StaffRosterDay(
        hospital_id=hospital.id,
        user_id=doctor.id,
        roster_date=target_date,
        status="working",
    )
    db_session.add(roster_day)
    db_session.flush()

    s1 = StaffRosterSegment(
        hospital_id=hospital.id,
        roster_day_id=roster_day.id,
        user_id=doctor.id,
        start_at=datetime(2026, 10, 16, 8, 0),
        end_at=datetime(2026, 10, 16, 10, 0),
        duty_kind="OPD",
        presence_type="ONSITE",
        is_bookable=True,
    )
    s2 = StaffRosterSegment(
        hospital_id=hospital.id,
        roster_day_id=roster_day.id,
        user_id=doctor.id,
        start_at=datetime(2026, 10, 16, 14, 0),
        end_at=datetime(2026, 10, 16, 19, 0),
        duty_kind="WARD",
        presence_type="ONSITE",
        is_bookable=False,
    )
    s3 = StaffRosterSegment(
        hospital_id=hospital.id,
        roster_day_id=roster_day.id,
        user_id=doctor.id,
        start_at=datetime(2026, 10, 16, 23, 0),
        end_at=datetime(2026, 10, 17, 2, 0),
        duty_kind="EMERGENCY",
        presence_type="ONSITE",
        is_bookable=False,
    )
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    service = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    segments = service.get_effective_duty_segments(doctor.id, target_date)
    assert len(segments) == 3

    # Check gap in between (10:00 to 14:00) is NOT working
    avail, _, _ = service.check_slot_availability(doctor.id, target_date, time(11, 0), 30)
    assert avail is False

    # Check OPD segment (08:00 - 10:00) IS bookable
    avail, _, _ = service.check_slot_availability(doctor.id, target_date, time(8, 30), 30)
    assert avail is True

    # Check Ward segment (14:00 - 19:00) is working but NOT OPD-bookable
    avail, _, _ = service.check_slot_availability(doctor.id, target_date, time(15, 0), 30)
    assert avail is False


def test_case_d_overnight_shift_carryover(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """
    Case D: Overnight 20:00 -> 08:00 next day:
    On the next calendar day (17 Oct), 00:00 to 08:00 is recognized as working time from carryover.
    """
    doctor = multi_roster_setup["doctor"]
    day_16 = date(2026, 10, 16)
    day_17 = date(2026, 10, 17)

    rd = StaffRosterDay(hospital_id=hospital.id, user_id=doctor.id, roster_date=day_16, status="working")
    db_session.add(rd)
    db_session.flush()

    seg = StaffRosterSegment(
        hospital_id=hospital.id,
        roster_day_id=rd.id,
        user_id=doctor.id,
        start_at=datetime(2026, 10, 16, 20, 0),
        end_at=datetime(2026, 10, 17, 8, 0),
        duty_kind="EMERGENCY",
        presence_type="ONSITE",
        is_bookable=False,
    )
    db_session.add(seg)
    db_session.commit()

    service = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    segs_17 = service.get_effective_duty_segments(doctor.id, day_17)
    # Must include carryover segment for overnight duty ending on 17 Oct at 08:00
    carryover = [s for s in segs_17 if s.source == "carryover" or s.end_at.hour == 8]
    assert len(carryover) == 1
    assert carryover[0].duty_kind == "EMERGENCY"


def test_case_e_cross_midnight_overlap_rejection(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """
    Case E: Cross-midnight conflict:
    16 Oct 22:00 -> 17 Oct 06:00
    17 Oct 05:00 -> 17 Oct 12:00
    Must be rejected with 400 because 05:00-06:00 overlaps.
    """
    doctor = multi_roster_setup["doctor"]
    admin = AdminActions(db_session, hospital.id, {"email": "admin@hospital.com"})

    # 1. Add 22:00 to 06:00 on 16 Oct
    admin.add_duty_segment(
        RosterDutySegmentCreate(
            roster_date=date(2026, 10, 16),
            user_id=doctor.id,
            start_time="22:00",
            end_time="06:00",
            duty_kind="EMERGENCY",
            presence_type="ONSITE",
        )
    )

    # 2. Attempt to add 05:00 to 12:00 on 17 Oct -> must raise 400
    with pytest.raises(HTTPException) as exc_info:
        admin.add_duty_segment(
            RosterDutySegmentCreate(
                roster_date=date(2026, 10, 17),
                user_id=doctor.id,
                start_time="05:00",
                end_time="12:00",
                duty_kind="OPD",
                presence_type="ONSITE",
            )
        )
    assert exc_info.value.status_code == 400
    assert "overlaps" in exc_info.value.detail.lower()


def test_case_f_multiple_opd_blocks_and_booking_boundaries(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """
    Case F: Multiple OPD blocks:
    08:00–12:00 OPD
    16:00–20:00 OPD
    Slots 08:00, 08:30, 16:00, 16:30 bookable.
    Slots 12:00, 14:00 rejected.
    Appointment crossing boundary (11:45 to 12:15) rejected.
    """
    doctor = multi_roster_setup["doctor"]
    patient = multi_roster_setup["patient"]
    target_date = date.today() + timedelta(days=1)

    admin = AdminActions(db_session, hospital.id, {"email": "admin@hospital.com"})
    admin.update_roster_day(
        StaffRosterDayUpdate(
            roster_date=target_date,
            user_id=doctor.id,
            status="working",
            segments=[
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=doctor.id,
                    start_time="08:00",
                    end_time="12:00",
                    duty_kind="OPD",
                    is_bookable=True,
                ),
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=doctor.id,
                    start_time="16:00",
                    end_time="20:00",
                    duty_kind="OPD",
                    is_bookable=True,
                ),
            ],
        )
    )

    service = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    # Check 11:00 is available
    assert service.check_slot_availability(doctor.id, target_date, time(11, 0), 30)[0] is True
    # Check 17:00 is available
    assert service.check_slot_availability(doctor.id, target_date, time(17, 0), 30)[0] is True
    # Check 13:00 (in between) is NOT available
    assert service.check_slot_availability(doctor.id, target_date, time(13, 0), 30)[0] is False

    # Check slot extending beyond boundary (11:45 to 12:15)
    assert service.check_slot_availability(doctor.id, target_date, time(11, 45), 30)[0] is False

    # Book valid appointment in block 1
    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    pricing_reader = PricingReader(db_session, hospital.id)
    booker = BookAppointmentAction(
        db=db_session,
        hospital_id=hospital.id,
        repo=repo,
        avail_reader=avail_reader,
        pricing_reader=pricing_reader,
    )
    appt = booker.execute(
        BookAppointmentRequest(
            doctor_id=doctor.id,
            patient_id=patient.id,
            appointment_date=target_date,
            appointment_time=time(16, 30),
            slot_duration_minutes=30,
            appointment_type="consultation",
            reason="Cardio checkup",
        ),
        user={"email": "admin@hospital.com"},
    )
    assert appt.id is not None


def test_case_g_opd_and_ot_conflict(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """
    Case G: OPD + OT:
    08:00–12:00 OPD
    14:00–18:00 OT
    OT period is not exposed as routine OPD availability (OUTSIDE_SHIFT).
    Surgeries scheduled during OPD duty trigger OT_CONFLICT.
    """
    doctor = multi_roster_setup["doctor"]
    patient = multi_roster_setup["patient"]
    target_date = date(2026, 10, 16)

    admin = AdminActions(db_session, hospital.id, {"email": "admin@hospital.com"})
    admin.update_roster_day(
        StaffRosterDayUpdate(
            roster_date=target_date,
            user_id=doctor.id,
            status="working",
            segments=[
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=doctor.id,
                    start_time="08:00",
                    end_time="12:00",
                    duty_kind="OPD",
                    is_bookable=True,
                ),
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=doctor.id,
                    start_time="14:00",
                    end_time="18:00",
                    duty_kind="OT",
                    is_bookable=False,
                ),
            ],
        )
    )

    # Create OT Room
    dept = multi_roster_setup["dept"]
    room = OtRoom(
        hospital_id=hospital.id,
        department_id=dept.id,
        code=f"OT-{uuid4().hex[:4]}",
        name="Main Operation Theatre",
        is_active=True,
    )
    db_session.add(room)
    db_session.flush()

    service = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    # 1. Non-bookable OT duty period (14:00-18:00) is NOT exposed as OPD availability
    avail_ot_duty, _, code_ot_duty = service.check_slot_availability(doctor.id, target_date, time(14, 30), 30)
    assert avail_ot_duty is False
    assert code_ot_duty == "OUTSIDE_SHIFT"

    # 2. Add an OT surgery inside doctor's OPD duty hours (10:00 to 11:30)
    surgery = OtSurgery(
        hospital_id=hospital.id,
        patient_id=patient.id,
        surgeon_id=doctor.id,
        surgery_no=f"SURG-{uuid4().hex[:6].upper()}",
        ot_room_id=room.id,
        department_id=dept.id,
        surgery_type="Emergency Bypass",
        scheduled_at=datetime.combine(target_date, time(10, 0)),
        duration_minutes=90,
        status=OtSurgeryStatus.scheduled,
    )
    db_session.add(surgery)
    db_session.commit()

    # During OT surgery within morning OPD session, doctor is blocked by OT_CONFLICT
    avail, _, code = service.check_slot_availability(doctor.id, target_date, time(10, 15), 30)
    assert avail is False
    assert code == "OT_CONFLICT"


def test_case_h_and_i_partial_and_full_leave(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """
    Case H & I:
    Roster: 08:00–12:00 and 16:00–20:00
    Partial leave 09:00–10:00 subtracts only that interval:
    08:00-09:00, 10:00-12:00, 16:00-20:00 remain available.
    """
    doctor = multi_roster_setup["doctor"]
    target_date = date(2026, 10, 16)

    admin = AdminActions(db_session, hospital.id, {"email": "admin@hospital.com"})
    admin.update_roster_day(
        StaffRosterDayUpdate(
            roster_date=target_date,
            user_id=doctor.id,
            status="working",
            segments=[
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=doctor.id,
                    start_time="08:00",
                    end_time="12:00",
                    duty_kind="OPD",
                    is_bookable=True,
                ),
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=doctor.id,
                    start_time="16:00",
                    end_time="20:00",
                    duty_kind="OPD",
                    is_bookable=True,
                ),
            ],
        )
    )

    # Approved partial leave 09:00 - 10:00
    leave = DoctorLeave(
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        leave_date=target_date,
        start_time=time(9, 0),
        end_time=time(10, 0),
        reason="Dentist appointment",
    )
    db_session.add(leave)
    db_session.commit()

    service = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    # 08:00–08:30 is available
    assert service.check_slot_availability(doctor.id, target_date, time(8, 0), 30)[0] is True
    # 09:00–09:30 is BLOCKED by leave
    leave_slot_avail, leave_reason, leave_code = service.check_slot_availability(doctor.id, target_date, time(9, 0), 30)
    assert leave_slot_avail is False
    assert leave_code == "DOCTOR_LEAVE"
    # 10:30–11:00 is available
    assert service.check_slot_availability(doctor.id, target_date, time(10, 30), 30)[0] is True
    # 17:00–17:30 is available
    assert service.check_slot_availability(doctor.id, target_date, time(17, 0), 30)[0] is True

    # Case I: Full day leave
    full_day_leave = DoctorLeave(
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        leave_date=date(2026, 10, 19),
        start_time=time(0, 0),
        end_time=time(23, 59),
        reason="Full day conference",
    )
    db_session.add(full_day_leave)
    db_session.commit()

    day_avail = service.get_daily_availability(doctor.id, date(2026, 10, 19))
    assert day_avail.available is False
    assert "approved leave" in day_avail.reason.lower()


def test_case_j_off_day_zero_segments(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """Case J: Off day with 0 segments is valid, not corrupt data."""
    doctor = multi_roster_setup["doctor"]
    target_date = date(2026, 10, 16)

    admin = AdminActions(db_session, hospital.id, {"email": "admin@hospital.com"})
    admin.update_roster_day(
        StaffRosterDayUpdate(
            roster_date=target_date,
            user_id=doctor.id,
            status="off",
            notes="Scheduled day off",
            segments=[],
        )
    )

    service = EffectiveDoctorAvailabilityService(db_session, hospital.id)
    segments = service.get_effective_duty_segments(doctor.id, target_date)
    assert len(segments) == 0

    daily = service.get_daily_availability(doctor.id, target_date)
    assert daily.available is False


def test_case_k_apply_defaults_from_work_pattern(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """
    Case K: Apply Defaults populates employee's recurring work pattern segments.
    """
    doctor = multi_roster_setup["doctor"]
    target_date = date(2026, 10, 16)  # Friday (weekday=4)

    # Setup StaffWorkPattern with 2 segments on Friday
    wp = StaffWorkPattern(
        hospital_id=hospital.id,
        user_id=doctor.id,
        name="Consultant Split Schedule",
        is_active=True,
    )
    db_session.add(wp)
    db_session.flush()

    wps1 = StaffWorkPatternSegment(
        hospital_id=hospital.id,
        pattern_id=wp.id,
        day_of_week=4,  # Friday
        start_time=time(8, 0),
        end_time=time(12, 0),
        duty_kind="OPD",
        presence_type="ONSITE",
        is_bookable=True,
    )
    wps2 = StaffWorkPatternSegment(
        hospital_id=hospital.id,
        pattern_id=wp.id,
        day_of_week=4,  # Friday
        start_time=time(16, 0),
        end_time=time(20, 0),
        duty_kind="OPD",
        presence_type="ONSITE",
        is_bookable=True,
    )
    db_session.add_all([wps1, wps2])
    db_session.commit()

    admin = AdminActions(db_session, hospital.id, {"email": "admin@hospital.com"})
    res = admin.seed_shift_roster(ShiftRosterSeed(roster_date=target_date, overwrite=True))

    entry = next(e for e in res.entries if e.user_id == doctor.id)
    assert entry.status == "on_duty"
    assert len(entry.segments) == 2
    assert entry.segments[0].start_time == "08:00"
    assert entry.segments[1].start_time == "16:00"


def test_case_l_rest_fatigue_warning(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """Case L: Warning generated when rest interval between duties is < 8 hours."""
    doctor = multi_roster_setup["doctor"]
    service = EffectiveDoctorAvailabilityService(db_session, hospital.id)

    from modules.doctors.services.effective_availability_service import EffectiveDutySegment

    segs = [
        EffectiveDutySegment(
            start_at=datetime(2026, 10, 16, 23, 0),
            end_at=datetime(2026, 10, 17, 7, 0),
            duty_kind="EMERGENCY",
        ),
        EffectiveDutySegment(
            start_at=datetime(2026, 10, 17, 9, 0),  # only 2 hours rest!
            end_at=datetime(2026, 10, 17, 13, 0),
            duty_kind="OPD",
        ),
    ]
    warnings = service.get_rest_fatigue_warnings(segs)
    assert len(warnings) == 1
    assert "Short rest interval" in warnings[0]


def test_case_m_counts_separate_unique_staff_from_assignments(db_session: Session, hospital: Hospital, multi_roster_setup: dict):
    """
    Case M: Verify unique staff count != total duty assignments count.
    1 staff with 3 segments must report 1 staff working, 3 assignments.
    """
    doctor = multi_roster_setup["doctor"]
    target_date = date(2026, 10, 16)

    admin = AdminActions(db_session, hospital.id, {"email": "admin@hospital.com"})
    admin.update_roster_day(
        StaffRosterDayUpdate(
            roster_date=target_date,
            user_id=doctor.id,
            status="working",
            segments=[
                RosterDutySegmentCreate(roster_date=target_date, user_id=doctor.id, start_time="08:00", end_time="10:00"),
                RosterDutySegmentCreate(roster_date=target_date, user_id=doctor.id, start_time="14:00", end_time="18:00"),
                RosterDutySegmentCreate(roster_date=target_date, user_id=doctor.id, start_time="20:00", end_time="22:00"),
            ],
        )
    )

    roster = admin.get_shift_roster(target_date)
    assert roster.unique_staff_working == 1
    assert roster.total_duty_segments == 3
    assert roster.total_staff == 1

    snapshot = admin.get_shift_roster_snapshot(target_date)
    assert "Staff working: 1" in snapshot.text
    assert "Total duty assignments: 3" in snapshot.text


def test_case_n_unscheduled_doctor_has_zero_availability_and_no_appointment_slots(
    db_session: Session, hospital: Hospital, multi_roster_setup: dict
):
    """
    Case N (Regression): Proves that an unscheduled doctor with no StaffRosterDay,
    no legacy daily assignment, no StaffWorkPattern, and no default shift_id
    safely receives [] duty segments, 0 appointment slots, and cannot be booked.
    Implicit 09:00-17:00 OPD availability is NEVER fabricated.
    """
    role = multi_roster_setup["role"]
    dept = multi_roster_setup["dept"]
    patient = multi_roster_setup["patient"]
    target_date = date(2026, 11, 20)

    unscheduled_doctor = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. Unscheduled",
        phone="9876543299",
        email=f"unscheduled.{uuid4().hex[:6]}@hospital.com",
        password_hash="fake",
        specialization="Cardiology",
        shift_id=None,  # No default shift
        department_id=dept.id,
        is_active=True,
    )
    db_session.add(unscheduled_doctor)
    db_session.commit()

    # 1. StaffScheduleService resolver returns []
    sched_service = StaffScheduleService(db_session, hospital.id)
    duty_segments = sched_service.get_effective_duty_segments(unscheduled_doctor.id, target_date)
    assert duty_segments == [], "Unscheduled doctor must resolve to empty duty segments, never 09:00-17:00"

    # 2. DoctorAvailabilityService returns available=False and empty segments
    doc_service = DoctorAvailabilityService(db_session, hospital.id)
    avail = doc_service.get_daily_availability(unscheduled_doctor.id, target_date)
    assert avail.available is False
    assert avail.segments == []

    # 3. Direct slot check returns False
    slot_ok, reason, _ = doc_service.check_slot_availability(
        unscheduled_doctor.id, target_date, time(10, 0), 30
    )
    assert slot_ok is False
    assert any(term in (reason or "").lower() for term in ("off", "outside", "shift", "unavailable"))

    # 4. Attempting to book an appointment fails cleanly
    repo = AppointmentsRepository(db_session, hospital.id)
    avail_reader = AvailabilityReader(db_session, hospital.id)
    pricing_reader = PricingReader(db_session, hospital.id)
    booker = BookAppointmentAction(
        db=db_session,
        hospital_id=hospital.id,
        repo=repo,
        avail_reader=avail_reader,
        pricing_reader=pricing_reader,
    )

    req = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=unscheduled_doctor.id,
        appointment_date=target_date,
        appointment_time=time(10, 0),
        booking_kind="walk_in",
        purpose="Check unscheduled rejection",
        payment_method="cash",
    )
    user = {"sub": str(uuid4()), "role": "hospital_admin"}

    with pytest.raises((AppointmentValidationError, AppointmentConflictError)) as exc_info:
        booker.execute(req, user)
    assert any(
        term in str(exc_info.value).lower()
        for term in ("outside", "shift", "off", "unavailable", "not on duty")
    )


def test_case_o_generic_non_doctor_nurse_and_lab_tech_rostering(
    db_session: Session, hospital: Hospital, multi_roster_setup: dict
):
    """
    Case O: Generic Staff Rostering:
    Nurse:
      07:00–12:00 WARD
      17:00–22:00 WARD
    Lab Technician:
      06:00–11:00 LAB
      16:00–20:00 LAB
    Pharmacist:
      09:00–17:00 OTHER

    Verifies:
    - Both segments are returned by generic staff schedule resolution;
    - The gap is not working time;
    - Neither is automatically classified as OPD;
    - Neither is appointment-bookable;
    - Overnight behavior works for a non-doctor;
    - Admin roster displays/manages them normally.
    """
    dept = multi_roster_setup["dept"]
    target_date = date(2026, 10, 16)
    next_date = date(2026, 10, 17)

    # 1. Create Nurse and Lab Tech roles and users
    nurse_role = StaffRole(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Staff Nurse",
        code="NURSE",
        is_active=True,
    )
    lab_role = StaffRole(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Lab Technician",
        code="LAB_TECH",
        is_active=True,
    )
    pharm_role = StaffRole(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Pharmacist",
        code="PHARMACIST",
        is_active=True,
    )
    db_session.add_all([nurse_role, lab_role, pharm_role])
    db_session.flush()

    nurse = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=nurse_role.id,
        name="Nurse Nightingale",
        phone="9876543288",
        email=f"nurse.{uuid4().hex[:6]}@hospital.com",
        password_hash="fake",
        department_id=dept.id,
        is_active=True,
    )
    lab_tech = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=lab_role.id,
        name="Lab Tech Raman",
        phone="9876543277",
        email=f"lab.{uuid4().hex[:6]}@hospital.com",
        password_hash="fake",
        department_id=dept.id,
        is_active=True,
    )
    pharmacist = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=pharm_role.id,
        name="Pharmacist Priya",
        phone="9876543266",
        email=f"pharm.{uuid4().hex[:6]}@hospital.com",
        password_hash="fake",
        department_id=dept.id,
        is_active=True,
    )
    db_session.add_all([nurse, lab_tech, pharmacist])
    db_session.commit()

    admin = AdminActions(db_session, hospital.id, {"email": "admin@hospital.com"})

    # 2. Roster Nurse: 07:00-12:00 WARD, 17:00-22:00 WARD
    admin.update_roster_day(
        StaffRosterDayUpdate(
            roster_date=target_date,
            user_id=nurse.id,
            status="working",
            segments=[
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=nurse.id,
                    start_time="07:00",
                    end_time="12:00",
                    duty_kind="WARD",
                    presence_type="ONSITE",
                    is_bookable=False,
                ),
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=nurse.id,
                    start_time="17:00",
                    end_time="22:00",
                    duty_kind="WARD",
                    presence_type="ONSITE",
                    is_bookable=False,
                ),
            ],
        )
    )

    # 3. Roster Lab Tech: 06:00-11:00 LAB, 16:00-20:00 LAB
    admin.update_roster_day(
        StaffRosterDayUpdate(
            roster_date=target_date,
            user_id=lab_tech.id,
            status="working",
            segments=[
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=lab_tech.id,
                    start_time="06:00",
                    end_time="11:00",
                    duty_kind="LAB",
                    presence_type="ONSITE",
                    is_bookable=False,
                ),
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=lab_tech.id,
                    start_time="16:00",
                    end_time="20:00",
                    duty_kind="LAB",
                    presence_type="ONSITE",
                    is_bookable=False,
                ),
            ],
        )
    )

    # 4. Roster Pharmacist: 09:00-17:00 OTHER
    admin.update_roster_day(
        StaffRosterDayUpdate(
            roster_date=target_date,
            user_id=pharmacist.id,
            status="working",
            segments=[
                RosterDutySegmentCreate(
                    roster_date=target_date,
                    user_id=pharmacist.id,
                    start_time="09:00",
                    end_time="17:00",
                    duty_kind="OTHER",
                    presence_type="ONSITE",
                    is_bookable=False,
                ),
            ],
        )
    )

    # 5. Generic schedule resolution via StaffScheduleService
    sched_service = StaffScheduleService(db_session, hospital.id)

    # Nurse verification
    nurse_segs = sched_service.get_effective_duty_segments(nurse.id, target_date)
    assert len(nurse_segs) == 2
    assert nurse_segs[0].start_time == time(7, 0)
    assert nurse_segs[0].end_time == time(12, 0)
    assert nurse_segs[0].duty_kind == "WARD"
    assert nurse_segs[0].is_bookable is False
    assert nurse_segs[1].start_time == time(17, 0)
    assert nurse_segs[1].end_time == time(22, 0)
    assert nurse_segs[1].duty_kind == "WARD"
    assert nurse_segs[1].is_bookable is False

    # Verify Nurse gap is NOT working time (e.g. 14:00)
    nurse_midday = any(s.start_time <= time(14, 0) < s.end_time for s in nurse_segs)
    assert nurse_midday is False, "Gap 12:00-17:00 must not be working time"

    # Lab Tech verification
    lab_segs = sched_service.get_effective_duty_segments(lab_tech.id, target_date)
    assert len(lab_segs) == 2
    assert lab_segs[0].start_time == time(6, 0)
    assert lab_segs[0].end_time == time(11, 0)
    assert lab_segs[0].duty_kind == "LAB"
    assert lab_segs[0].is_bookable is False
    assert lab_segs[1].start_time == time(16, 0)
    assert lab_segs[1].end_time == time(20, 0)
    assert lab_segs[1].duty_kind == "LAB"
    assert lab_segs[1].is_bookable is False

    # Verify Lab Tech gap is NOT working time (e.g. 13:00)
    lab_gap = any(s.start_time <= time(13, 0) < s.end_time for s in lab_segs)
    assert lab_gap is False, "Gap 11:00-16:00 must not be working time"

    # 6. Verify non-doctors have zero doctor appointment availability
    doc_service = DoctorAvailabilityService(db_session, hospital.id)
    nurse_avail = doc_service.get_daily_availability(nurse.id, target_date)
    assert nurse_avail.available is False

    nurse_slot_ok, _, _ = doc_service.check_slot_availability(nurse.id, target_date, time(8, 0), 30)
    assert nurse_slot_ok is False

    lab_avail = doc_service.get_daily_availability(lab_tech.id, target_date)
    assert lab_avail.available is False

    lab_slot_ok, _, _ = doc_service.check_slot_availability(lab_tech.id, target_date, time(8, 0), 30)
    assert lab_slot_ok is False

    # 7. Overnight behavior for non-doctor
    # Nurse works overnight from 22:00 on 16 Oct to 06:00 on 17 Oct
    admin.add_duty_segment(
        RosterDutySegmentCreate(
            roster_date=target_date,
            user_id=nurse.id,
            start_time="22:30",
            end_time="06:30",
            duty_kind="WARD",
            presence_type="ONSITE",
            is_bookable=False,
        )
    )

    nurse_next_day_segs = sched_service.get_effective_duty_segments(nurse.id, next_date)
    carryovers = [s for s in nurse_next_day_segs if s.source == "carryover" or s.end_at.hour == 6]
    assert len(carryovers) == 1
    assert carryovers[0].duty_kind == "WARD"
    assert carryovers[0].is_bookable is False

    # 8. Verify Admin roster displays/manages them normally
    roster = admin.get_shift_roster(target_date)
    nurse_entry = next(e for e in roster.entries if e.user_id == nurse.id)
    lab_entry = next(e for e in roster.entries if e.user_id == lab_tech.id)
    pharm_entry = next(e for e in roster.entries if e.user_id == pharmacist.id)

    assert nurse_entry.role_name == "Staff Nurse"
    assert len(nurse_entry.segments) >= 2
    assert any(s.duty_kind == "WARD" for s in nurse_entry.segments)

    assert lab_entry.role_name == "Lab Technician"
    assert len(lab_entry.segments) == 2
    assert all(s.duty_kind == "LAB" for s in lab_entry.segments)

    assert pharm_entry.role_name == "Pharmacist"
    assert len(pharm_entry.segments) == 1
    assert pharm_entry.segments[0].duty_kind == "OTHER"


def test_case_p_recurring_work_pattern_crud_and_apply_defaults(
    db_session: Session, hospital: Hospital, multi_roster_setup: dict
):
    """
    Case P: Recurring Work Pattern Management:
    - View pattern
    - Add recurring segments
    - Edit recurring segment
    - Delete recurring segment
    - Mark weekday off / reset it
    - Apply Defaults actually creates configured recurring segments.
    """
    doctor = multi_roster_setup["doctor"]
    admin = AdminActions(db_session, hospital.id, {"email": "admin@hospital.com"})

    # 1. View pattern (initially empty or default)
    pattern_resp = admin.get_staff_work_pattern(doctor.id)
    assert pattern_resp.user_id == doctor.id
    assert pattern_resp.name is not None

    # 2. Update recurring work pattern with multiple weekdays:
    # Monday (0): 08:00-10:00 OPD (bookable), 14:00-19:00 WARD (not bookable)
    # Tuesday (1): 08:00-12:00 OPD (bookable)
    # Wednesday (2): Off (no segments)
    update_data = StaffWorkPatternUpdate(
        name="Dr. Chandra Split Practice",
        is_active=True,
        segments=[
            WorkPatternSegmentItem(
                day_of_week=0,
                start_time="08:00",
                end_time="10:00",
                duty_kind="OPD",
                presence_type="ONSITE",
                is_bookable=True,
            ),
            WorkPatternSegmentItem(
                day_of_week=0,
                start_time="14:00",
                end_time="19:00",
                duty_kind="WARD",
                presence_type="ONSITE",
                is_bookable=False,
            ),
            WorkPatternSegmentItem(
                day_of_week=1,
                start_time="08:00",
                end_time="12:00",
                duty_kind="OPD",
                presence_type="ONSITE",
                is_bookable=True,
            ),
        ],
    )
    saved_pattern = admin.update_staff_work_pattern(doctor.id, update_data)
    assert len(saved_pattern.segments) == 3

    # 3. Add single recurring segment (Thursday, day 3: 09:00-13:00 OPD)
    added_seg = admin.add_work_pattern_segment(
        doctor.id,
        WorkPatternSegmentCreate(
            user_id=doctor.id,
            day_of_week=3,
            start_time="09:00",
            end_time="13:00",
            duty_kind="OPD",
            presence_type="ONSITE",
            is_bookable=True,
        ),
    )
    assert added_seg.day_of_week == 3
    assert added_seg.start_time == "09:00"

    # Verify pattern now has 4 segments
    pattern_resp = admin.get_staff_work_pattern(doctor.id)
    assert len(pattern_resp.segments) == 4

    # 4. Delete segment
    del_res = admin.delete_work_pattern_segment(doctor.id, added_seg.id)
    assert del_res["success"] is True

    # 5. Clear a day (mark day off) - e.g. Tuesday (day 1)
    clear_res = admin.clear_work_pattern_day(doctor.id, 1)
    assert clear_res["success"] is True

    pattern_resp = admin.get_staff_work_pattern(doctor.id)
    tuesday_segs = [s for s in pattern_resp.segments if s.day_of_week == 1]
    assert len(tuesday_segs) == 0

    # 6. Apply Defaults on a Monday (2026-10-19 is a Monday, day_of_week=0)
    monday_date = date(2026, 10, 19)
    seed_res = admin.seed_shift_roster(ShiftRosterSeed(roster_date=monday_date, overwrite=True))
    entry = next(e for e in seed_res.entries if e.user_id == doctor.id)
    assert entry.status == "on_duty"
    assert len(entry.segments) == 2
    assert entry.segments[0].start_time == "08:00"
    assert entry.segments[0].duty_kind == "OPD"
    assert entry.segments[0].is_bookable is True
    assert entry.segments[1].start_time == "14:00"
    assert entry.segments[1].duty_kind == "WARD"
    assert entry.segments[1].is_bookable is False

    # 7. Apply Defaults on a Wednesday (2026-10-21 is a Wednesday, day_of_week=2, marked off)
    wed_date = date(2026, 10, 21)
    seed_res_wed = admin.seed_shift_roster(ShiftRosterSeed(roster_date=wed_date, overwrite=True))
    wed_entry = next(e for e in seed_res_wed.entries if e.user_id == doctor.id)
    assert wed_entry.status == "off"
    assert len(wed_entry.segments) == 0


def test_case_q_migration_51_vs_47_integrity_assertion(db_session: Session):
    """
    Case Q: Explains and enforces the 51 StaffRosterDay vs 47 StaffRosterSegment invariant.
    In multi-segment rostering, non-working days (status 'off' or 'leave') correctly
    have zero duty segments.
    Asserts that any StaffRosterDay without segments is an intentional non-working state.
    """
    days_without_segments = (
        db_session.query(StaffRosterDay)
        .filter(~StaffRosterDay.segments.any())
        .all()
    )
    for day in days_without_segments:
        assert day.status.lower() in ("off", "leave"), (
            f"StaffRosterDay {day.id} on {day.roster_date} has 0 segments but status '{day.status}' is not off/leave"
        )
