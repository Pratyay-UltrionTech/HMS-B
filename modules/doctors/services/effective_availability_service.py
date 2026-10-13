"""
Canonical Effective Doctor & Staff Availability Service.

Conforms to UltrionTech-Backend-Template modules/doctors/services/ specification.
Single authoritative calculation used by:
- Appointment Availability (GET /api/appointments/availability)
- Appointment Booking (POST /api/appointments)
- Appointment Reschedule (POST /api/appointments/{id}/reschedule)
- Operating Theatre surgeon scheduling (POST /api/ot/surgeries)
- Doctor Leave validation

Enforces deterministic precedence:
1. Hospital holiday → unavailable
2. Staff active/employment state → unavailable if inactive
3. Daily StaffDailyShift roster status (off / leave) → unavailable
4. Approved DoctorLeave (full day or covering shift) → unavailable
5. Active Shift determination (StaffDailyShift.shift_id override, else default HospitalUser.shift_id)
6. Shift bounds check (slot within shift_start and shift_end)
7. Partial-day leave intervals
8. Existing OPD appointments (interval overlap, not just exact start time)
9. Existing OT surgeries (interval overlap)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.appointment_type import AppointmentType
from modules.appointments.entities.enums import AppointmentStatus
from modules.doctors.entities.doctor import (
    DoctorLeave,
    Holiday,
    HospitalUser,
    ShiftType,
    StaffDailyShift,
)
from modules.ot.entities.ot_entities import OtSurgery, OtSurgeryStatus
from shared.hospital_clock import get_hospital_tz

DEFAULT_SHIFT_START = time(9, 0)
DEFAULT_SHIFT_END = time(17, 0)
DEFAULT_SLOT_MINUTES = 15


def time_to_minutes(val: time) -> int:
    return val.hour * 60 + val.minute


def minutes_to_time(total: int) -> time:
    total = max(0, min(total, 24 * 60 - 1))
    return time(total // 60, total % 60)


def add_minutes_to_time(val: time, minutes: int) -> time:
    return minutes_to_time(time_to_minutes(val) + minutes)


def intervals_overlap(start_a: time, end_a: time, start_b: time, end_b: time) -> bool:
    a0, a1 = time_to_minutes(start_a), time_to_minutes(end_a)
    b0, b1 = time_to_minutes(start_b), time_to_minutes(end_b)
    if a1 <= a0 or b1 <= b0:
        return False
    return a0 < b1 and b0 < a1


@dataclass
class LeaveBlockItem:
    start: str
    end: str
    reason: str | None = None


@dataclass
class BlockedIntervalItem:
    start: str
    end: str
    source: str  # "leave" | "appointment" | "ot"
    reason: str | None = None


@dataclass
class DoctorDailyAvailability:
    doctor_id: UUID
    doctor_name: str
    date: date
    available: bool
    reason: str | None = None
    shift_start: str | None = None
    shift_end: str | None = None
    booked_slots: list[str] = field(default_factory=list)
    leave_blocks: list[dict[str, Any]] = field(default_factory=list)
    blocked_intervals: list[dict[str, Any]] = field(default_factory=list)


class EffectiveDoctorAvailabilityService:
    """Canonical availability and scheduling conflict evaluator for doctors and clinical staff."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_slot_duration(self) -> int:
        """Resolve configured appointment slot duration or fall back to default (15 min)."""
        row = (
            self.db.query(AppointmentType.slot_duration_minutes)
            .filter(
                AppointmentType.hospital_id == self.hospital_id,
                AppointmentType.is_active.is_(True),
            )
            .order_by(AppointmentType.name.asc())
            .first()
        )
        if row and row[0]:
            return int(row[0])
        return DEFAULT_SLOT_MINUTES

    def get_holiday(self, check_date: date) -> Holiday | None:
        return (
            self.db.query(Holiday)
            .filter(
                Holiday.hospital_id == self.hospital_id,
                Holiday.holiday_date == check_date,
            )
            .first()
        )

    def get_doctor(self, doctor_id: UUID, *, for_update: bool = False) -> HospitalUser | None:
        q = self.db.query(HospitalUser).filter(
            HospitalUser.id == doctor_id,
            HospitalUser.hospital_id == self.hospital_id,
        )
        if for_update and self.db.bind and self.db.bind.dialect.name == "postgresql":
            q = q.with_for_update()
        return q.first()

    def get_daily_roster_shift(self, doctor_id: UUID, on_date: date) -> StaffDailyShift | None:
        return (
            self.db.query(StaffDailyShift)
            .filter(
                StaffDailyShift.hospital_id == self.hospital_id,
                StaffDailyShift.user_id == doctor_id,
                StaffDailyShift.roster_date == on_date,
            )
            .first()
        )

    def resolve_effective_shift(
        self,
        doctor: HospitalUser,
        roster_shift: StaffDailyShift | None,
    ) -> tuple[time, time, str | None]:
        """Resolves active shift times: roster shift override takes precedence over default shift."""
        shift_id = (
            roster_shift.shift_id
            if (roster_shift and roster_shift.shift_id)
            else doctor.shift_id
        )
        if shift_id:
            shift = (
                self.db.query(ShiftType)
                .filter(
                    ShiftType.hospital_id == self.hospital_id,
                    ShiftType.id == shift_id,
                    ShiftType.is_active.is_(True),
                )
                .first()
            )
            if shift:
                return shift.start_time, shift.end_time, shift.name
        return DEFAULT_SHIFT_START, DEFAULT_SHIFT_END, None

    def get_leaves_for_date(self, doctor_id: UUID, on_date: date) -> list[DoctorLeave]:
        return (
            self.db.query(DoctorLeave)
            .filter(
                DoctorLeave.hospital_id == self.hospital_id,
                DoctorLeave.doctor_id == doctor_id,
                DoctorLeave.leave_date == on_date,
            )
            .all()
        )

    def get_appointments_for_date(
        self,
        doctor_id: UUID,
        on_date: date,
        exclude_id: UUID | None = None,
    ) -> list[Appointment]:
        q = (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.doctor_id == doctor_id,
                Appointment.appointment_date == on_date,
                Appointment.status.notin_(
                    [AppointmentStatus.cancelled, AppointmentStatus.no_show]
                ),
            )
        )
        if exclude_id:
            q = q.filter(Appointment.id != exclude_id)
        return q.all()

    def get_ot_surgeries_for_date(
        self,
        doctor_id: UUID,
        on_date: date,
        exclude_id: UUID | None = None,
    ) -> list[tuple[OtSurgery, time, time]]:
        """Fetch OT surgeries scheduled for doctor on local date, returning (surgery, local_start, local_end)."""
        tz = get_hospital_tz(self.db, self.hospital_id)
        # 1-day safety window around target date to cover timezone boundaries
        window_start = datetime.combine(on_date - timedelta(days=1), time.min).replace(tzinfo=tz)
        window_end = datetime.combine(on_date + timedelta(days=1), time.max).replace(tzinfo=tz)

        q = (
            self.db.query(OtSurgery)
            .filter(
                OtSurgery.hospital_id == self.hospital_id,
                OtSurgery.surgeon_id == doctor_id,
                OtSurgery.scheduled_at >= window_start,
                OtSurgery.scheduled_at <= window_end,
                OtSurgery.status != OtSurgeryStatus.cancelled,
            )
        )
        if exclude_id:
            q = q.filter(OtSurgery.id != exclude_id)

        rows = q.all()
        results: list[tuple[OtSurgery, time, time]] = []
        for s in rows:
            dt = s.scheduled_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local_dt = dt.astimezone(tz)
            if local_dt.date() == on_date:
                st = local_dt.time()
                dur = s.duration_minutes or 60
                et = (local_dt + timedelta(minutes=dur)).time()
                results.append((s, st, et))
        return results

    def get_daily_availability(self, doctor_id: UUID, query_date: date) -> DoctorDailyAvailability:
        """Computes comprehensive daily availability for slot-picker and schedule views."""
        doctor = self.get_doctor(doctor_id)
        if not doctor:
            return DoctorDailyAvailability(
                doctor_id=doctor_id,
                doctor_name="Unknown Doctor",
                date=query_date,
                available=False,
                reason="Doctor not found",
            )

        # 1. Holiday rule
        holiday = self.get_holiday(query_date)
        if holiday:
            return DoctorDailyAvailability(
                doctor_id=doctor_id,
                doctor_name=doctor.name,
                date=query_date,
                available=False,
                reason=f"Hospital closed — {holiday.name}",
            )

        # 2. Doctor active rule
        if not doctor.is_active:
            return DoctorDailyAvailability(
                doctor_id=doctor_id,
                doctor_name=doctor.name,
                date=query_date,
                available=False,
                reason="Doctor is inactive",
            )

        # 3. Roster override (StaffDailyShift)
        roster_shift = self.get_daily_roster_shift(doctor_id, query_date)
        if roster_shift and roster_shift.status.lower() in ("off", "leave"):
            status_text = "off-duty" if roster_shift.status.lower() == "off" else "on leave"
            return DoctorDailyAvailability(
                doctor_id=doctor_id,
                doctor_name=doctor.name,
                date=query_date,
                available=False,
                reason=f"Doctor is marked {status_text} in the roster on this date",
            )

        # 4. Resolve shift bounds
        shift_start, shift_end, _ = self.resolve_effective_shift(doctor, roster_shift)

        # 5. Approved leaves
        leaves = self.get_leaves_for_date(doctor_id, query_date)
        # Check full-day leave
        full_day = next(
            (
                l
                for l in leaves
                if l.start_time <= shift_start and l.end_time >= shift_end
            ),
            None,
        )
        if full_day:
            reason = f"On approved leave: {full_day.reason}" if full_day.reason else "On approved leave"
            return DoctorDailyAvailability(
                doctor_id=doctor_id,
                doctor_name=doctor.name,
                date=query_date,
                available=False,
                reason=reason,
                shift_start=shift_start.strftime("%H:%M"),
                shift_end=shift_end.strftime("%H:%M"),
            )

        leave_blocks = [
            {
                "start": l.start_time.strftime("%H:%M"),
                "end": l.end_time.strftime("%H:%M"),
                "reason": l.reason,
            }
            for l in leaves
        ]

        blocked_intervals: list[dict[str, Any]] = []
        for l in leaves:
            blocked_intervals.append(
                {
                    "start": l.start_time.strftime("%H:%M"),
                    "end": l.end_time.strftime("%H:%M"),
                    "source": "leave",
                    "reason": l.reason or "Approved Leave",
                }
            )

        # 6. Existing appointments
        slot_dur = self.get_slot_duration()
        appts = self.get_appointments_for_date(doctor_id, query_date)
        for a in appts:
            a_dur = (
                a.appointment_type.slot_duration_minutes
                if (a.appointment_type and a.appointment_type.slot_duration_minutes)
                else slot_dur
            )
            a_end = add_minutes_to_time(a.appointment_time, a_dur)
            blocked_intervals.append(
                {
                    "start": a.appointment_time.strftime("%H:%M"),
                    "end": a_end.strftime("%H:%M"),
                    "source": "appointment",
                    "reason": f"Appointment #{a.queue_token or ''}".strip(),
                }
            )

        # 7. Existing OT Surgeries
        surgeries = self.get_ot_surgeries_for_date(doctor_id, query_date)
        for s, s_start, s_end in surgeries:
            blocked_intervals.append(
                {
                    "start": s_start.strftime("%H:%M"),
                    "end": s_end.strftime("%H:%M"),
                    "source": "ot",
                    "reason": f"OT Surgery {s.surgery_no} ({s.surgery_type})",
                }
            )

        # 8. Compute discrete booked slots for slot pickers
        booked_slots: set[str] = set()
        curr_min = time_to_minutes(shift_start)
        end_min = time_to_minutes(shift_end)
        while curr_min + slot_dur <= end_min:
            slot_s = minutes_to_time(curr_min)
            slot_e = minutes_to_time(curr_min + slot_dur)
            for b in blocked_intervals:
                b_start = time.fromisoformat(b["start"])
                b_end = time.fromisoformat(b["end"])
                if intervals_overlap(slot_s, slot_e, b_start, b_end):
                    booked_slots.add(slot_s.strftime("%H:%M"))
                    break
            curr_min += slot_dur

        return DoctorDailyAvailability(
            doctor_id=doctor_id,
            doctor_name=doctor.name,
            date=query_date,
            available=True,
            reason=None,
            shift_start=shift_start.strftime("%H:%M"),
            shift_end=shift_end.strftime("%H:%M"),
            booked_slots=sorted(list(booked_slots)),
            leave_blocks=leave_blocks,
            blocked_intervals=blocked_intervals,
        )

    def check_slot_availability(
        self,
        doctor_id: UUID,
        query_date: date,
        start_time: time,
        duration_minutes: int,
        *,
        exclude_appointment_id: UUID | None = None,
        exclude_ot_id: UUID | None = None,
        for_update: bool = False,
    ) -> tuple[bool, str | None, str | None]:
        """
        Validates if doctor is available for the given time slot [start_time, start_time + duration].
        Returns (is_available, error_message, error_code).
        Error codes:
        - HOLIDAY
        - DOCTOR_NOT_FOUND
        - DOCTOR_INACTIVE
        - ROSTER_OFF
        - DOCTOR_LEAVE
        - OUTSIDE_SHIFT
        - APPOINTMENT_CONFLICT
        - OT_CONFLICT
        """
        # 1. Holiday
        holiday = self.get_holiday(query_date)
        if holiday:
            return False, f"Hospital is closed on {query_date} ({holiday.name})", "HOLIDAY"

        # 2. Doctor exists & active (with optional row lock for concurrency)
        doctor = self.get_doctor(doctor_id, for_update=for_update)
        if not doctor:
            return False, "Doctor not found", "DOCTOR_NOT_FOUND"
        if not doctor.is_active:
            return False, "Doctor is inactive", "DOCTOR_INACTIVE"

        # 3. Daily roster override
        roster_shift = self.get_daily_roster_shift(doctor_id, query_date)
        if roster_shift and roster_shift.status.lower() in ("off", "leave"):
            status_text = "off-duty" if roster_shift.status.lower() == "off" else "on leave"
            return (
                False,
                f"Doctor is marked {status_text} in the daily roster on {query_date}",
                "ROSTER_OFF",
            )

        # 4. Resolve shift
        shift_start, shift_end, _ = self.resolve_effective_shift(doctor, roster_shift)

        # 5. Check slot bounds against shift
        end_time = add_minutes_to_time(start_time, duration_minutes)
        if start_time < shift_start or end_time > shift_end:
            return (
                False,
                f"Requested slot {start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')} falls outside doctor's active shift ({shift_start.strftime('%H:%M')}–{shift_end.strftime('%H:%M')})",
                "OUTSIDE_SHIFT",
            )

        # 6. Approved leaves
        leaves = self.get_leaves_for_date(doctor_id, query_date)
        for l in leaves:
            if intervals_overlap(start_time, end_time, l.start_time, l.end_time):
                reason_detail = f": {l.reason}" if l.reason else ""
                return (
                    False,
                    f"Doctor has approved leave from {l.start_time.strftime('%H:%M')} to {l.end_time.strftime('%H:%M')}{reason_detail}",
                    "DOCTOR_LEAVE",
                )

        # 7. Existing OPD appointments
        slot_dur = self.get_slot_duration()
        appts = self.get_appointments_for_date(
            doctor_id, query_date, exclude_id=exclude_appointment_id
        )
        for a in appts:
            a_dur = (
                a.appointment_type.slot_duration_minutes
                if (a.appointment_type and a.appointment_type.slot_duration_minutes)
                else slot_dur
            )
            a_end = add_minutes_to_time(a.appointment_time, a_dur)
            if intervals_overlap(start_time, end_time, a.appointment_time, a_end):
                return (
                    False,
                    f"Doctor already has an appointment from {a.appointment_time.strftime('%H:%M')} to {a_end.strftime('%H:%M')}",
                    "APPOINTMENT_CONFLICT",
                )

        # 8. Existing OT Surgeries
        surgeries = self.get_ot_surgeries_for_date(
            doctor_id, query_date, exclude_id=exclude_ot_id
        )
        for s, s_start, s_end in surgeries:
            if intervals_overlap(start_time, end_time, s_start, s_end):
                return (
                    False,
                    f"Doctor is scheduled for OT surgery {s.surgery_no} ({s.surgery_type}) from {s_start.strftime('%H:%M')} to {s_end.strftime('%H:%M')}",
                    "OT_CONFLICT",
                )

        return True, None, None
