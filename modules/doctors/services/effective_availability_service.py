"""
Canonical Effective Doctor & Staff Availability Service.

Conforms to UltrionTech-Backend-Template modules/doctors/services/ specification.
Single authoritative calculation used by:
- Appointment Availability (GET /api/appointments/availability)
- Appointment Booking (POST /api/appointments)
- Appointment Reschedule (POST /api/appointments/{id}/reschedule)
- Operating Theatre surgeon scheduling (POST /api/ot/surgeries)
- Doctor Leave validation
- Shift Roster & Schedule Context

Enforces deterministic precedence:
1. Hospital holiday → unavailable
2. Staff active/employment state → unavailable if inactive
3. Daily roster override (StaffRosterDay / legacy StaffDailyShift off / leave) → unavailable
4. Effective duty segments resolution:
   a. StaffRosterDay.segments (multi-segment daily roster)
   b. Fallback to StaffDailyShift (legacy single shift override)
   c. Fallback to StaffWorkPattern (recurring weekly schedule for that weekday)
   d. Fallback to HospitalUser.shift_id (legacy employee default shift)
   e. Carryover segments from previous calendar day (e.g. overnight 23:00–02:00)
5. Only eligible duty kinds (OPD / bookable) create routine appointment slots
6. Gaps between duty segments are NOT available
7. Partial-day and full-day leave intervals subtracted
8. Existing OPD appointments subtracted (interval overlap)
9. Existing OT surgeries subtracted (interval overlap)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload, selectinload

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
from modules.doctors.entities.rostering_entities import (
    StaffRosterDay,
    StaffRosterSegment,
    StaffWorkPattern,
    StaffWorkPatternSegment,
)
from modules.ot.entities.ot_entities import OtSurgery, OtSurgeryStatus
from shared.holidays import get_effective_holiday
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


def dt_intervals_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


@dataclass
class EffectiveDutySegment:
    start_at: datetime
    end_at: datetime
    id: UUID | None = None
    start_time: time | None = None
    end_time: time | None = None
    duty_kind: str = "OTHER"  # OPD, WARD, ICU, EMERGENCY, OT, LAB, RADIOLOGY, ON_CALL, ADMIN, TRAINING, OTHER
    presence_type: str = "ONSITE"  # ONSITE, ON_CALL, REMOTE
    is_bookable: bool = False
    shift_type_id: UUID | None = None
    shift_name: str | None = None
    department_id: UUID | None = None
    department_name: str | None = None
    notes: str | None = None
    source: str = "roster"  # "roster" | "pattern" | "default_shift" | "carryover"
    is_cross_midnight: bool = False

    def __post_init__(self) -> None:
        if self.start_time is None and self.start_at is not None:
            self.start_time = self.start_at.time()
        if self.end_time is None and self.end_at is not None:
            self.end_time = self.end_at.time()
        if self.start_at is not None and self.end_at is not None:
            self.is_cross_midnight = self.start_at.date() != self.end_at.date()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id) if self.id else None,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "start_time": self.start_time.strftime("%H:%M") if self.start_time else "",
            "end_time": self.end_time.strftime("%H:%M") if self.end_time else "",
            "duty_kind": self.duty_kind,
            "presence_type": self.presence_type,
            "is_bookable": self.is_bookable,
            "shift_type_id": str(self.shift_type_id) if self.shift_type_id else None,
            "shift_name": self.shift_name,
            "department_id": str(self.department_id) if self.department_id else None,
            "department_name": self.department_name,
            "notes": self.notes,
            "source": self.source,
            "is_cross_midnight": self.is_cross_midnight,
        }


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
    segments: list[dict[str, Any]] = field(default_factory=list)
    rest_warnings: list[str] = field(default_factory=list)


class StaffScheduleService:
    """Canonical evaluator answering: 'When is this employee working?'
    Operates on generic user_id for any HospitalUser (doctor, nurse, technician, admin, etc.).
    """

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_holiday(self, check_date: date) -> Holiday | None:
        return get_effective_holiday(self.db, self.hospital_id, check_date)

    def get_staff_user(self, user_id: UUID, *, for_update: bool = False) -> HospitalUser | None:
        q = (
            self.db.query(HospitalUser)
            .options(selectinload(HospitalUser.role))
            .filter(
                HospitalUser.id == user_id,
                HospitalUser.hospital_id == self.hospital_id,
            )
        )
        if for_update and self.db.bind and self.db.bind.dialect.name == "postgresql":
            q = q.with_for_update(of=HospitalUser)
        return q.first()

    def get_daily_roster_day(self, user_id: UUID, on_date: date) -> StaffRosterDay | None:
        return (
            self.db.query(StaffRosterDay)
            .options(
                selectinload(StaffRosterDay.segments).joinedload(StaffRosterSegment.shift_type),
                selectinload(StaffRosterDay.segments).joinedload(StaffRosterSegment.department),
            )
            .filter(
                StaffRosterDay.hospital_id == self.hospital_id,
                StaffRosterDay.user_id == user_id,
                StaffRosterDay.roster_date == on_date,
            )
            .first()
        )

    def get_daily_roster_shift(self, user_id: UUID, on_date: date) -> StaffDailyShift | None:
        """Legacy helper for backward compatibility."""
        return (
            self.db.query(StaffDailyShift)
            .filter(
                StaffDailyShift.hospital_id == self.hospital_id,
                StaffDailyShift.user_id == user_id,
                StaffDailyShift.roster_date == on_date,
            )
            .first()
        )

    def resolve_effective_shift(
        self,
        staff: HospitalUser,
        roster_shift: StaffDailyShift | None = None,
    ) -> tuple[time, time, str | None]:
        """Resolves active shift times: roster shift override takes precedence over default shift."""
        shift_id = (
            roster_shift.shift_id
            if (roster_shift and roster_shift.shift_id)
            else staff.shift_id
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

    def _resolve_raw_day_segments(
        self,
        user_id: UUID,
        on_date: date,
        tz: ZoneInfo,
    ) -> tuple[str, list[EffectiveDutySegment]]:
        """
        Resolves duty segments originating strictly on on_date (without carryovers).
        Operates on generic user_id. Returns (status, segments). Status is 'working', 'off', or 'leave'.
        """
        staff_user = self.get_staff_user(user_id)
        is_doctor = False
        if staff_user:
            is_doctor = (
                (staff_user.role and "doctor" in staff_user.role.name.lower())
                or bool(staff_user.specialization)
                or bool(staff_user.medical_registration_number)
            )

        # 1. StaffRosterDay check (Canonical Authority)
        roster_day = self.get_daily_roster_day(user_id, on_date)
        if roster_day:
            status_val = roster_day.status.lower()
            if status_val in ("off", "leave"):
                return status_val, []
            segments: list[EffectiveDutySegment] = []
            for seg in roster_day.segments:
                seg_start = seg.start_at if seg.start_at.tzinfo else seg.start_at.replace(tzinfo=tz)
                seg_end = seg.end_at if seg.end_at.tzinfo else seg.end_at.replace(tzinfo=tz)
                st_time = seg_start.astimezone(tz).time()
                et_time = seg_end.astimezone(tz).time()
                cross = seg_end.date() > seg_start.date()
                segments.append(
                    EffectiveDutySegment(
                        id=seg.id,
                        start_at=seg_start,
                        end_at=seg_end,
                        start_time=st_time,
                        end_time=et_time,
                        duty_kind=seg.duty_kind or "OTHER",
                        presence_type=seg.presence_type or "ONSITE",
                        is_bookable=seg.is_bookable,
                        shift_type_id=seg.shift_type_id,
                        shift_name=seg.shift_type.name if seg.shift_type else None,
                        department_id=seg.department_id,
                        department_name=seg.department.name if seg.department else None,
                        notes=seg.notes,
                        source="roster",
                        is_cross_midnight=cross,
                    )
                )
            return "working", segments

        # 2. Legacy StaffDailyShift fallback
        legacy_shift = self.get_daily_roster_shift(user_id, on_date)
        if legacy_shift:
            status_val = legacy_shift.status.lower()
            if status_val in ("off", "leave"):
                return status_val, []
            if legacy_shift.shift_id:
                st = (
                    self.db.query(ShiftType)
                    .filter(
                        ShiftType.id == legacy_shift.shift_id,
                        ShiftType.hospital_id == self.hospital_id,
                        ShiftType.is_active.is_(True),
                    )
                    .first()
                )
                if st:
                    cross = st.end_time <= st.start_time
                    start_dt = datetime.combine(on_date, st.start_time).replace(tzinfo=tz)
                    end_dt = datetime.combine(
                        on_date + timedelta(days=1 if cross else 0), st.end_time
                    ).replace(tzinfo=tz)
                    is_opd = is_doctor or "OPD" in (st.name or "").upper()
                    return "working", [
                        EffectiveDutySegment(
                            id=legacy_shift.id,
                            start_at=start_dt,
                            end_at=end_dt,
                            start_time=st.start_time,
                            end_time=st.end_time,
                            duty_kind="OPD" if is_opd else "OTHER",
                            presence_type="ONSITE",
                            is_bookable=is_opd,
                            shift_type_id=st.id,
                            shift_name=st.name,
                            department_id=st.department_id,
                            notes=legacy_shift.notes,
                            source="roster",
                            is_cross_midnight=cross,
                        )
                    ]
            return "working", []

        # 3. Recurring work pattern fallback
        pattern = (
            self.db.query(StaffWorkPattern)
            .options(
                selectinload(StaffWorkPattern.segments).joinedload(StaffWorkPatternSegment.shift_type),
                selectinload(StaffWorkPattern.segments).joinedload(StaffWorkPatternSegment.department),
            )
            .filter(
                StaffWorkPattern.hospital_id == self.hospital_id,
                StaffWorkPattern.user_id == user_id,
                StaffWorkPattern.is_active.is_(True),
            )
            .first()
        )
        if pattern:
            day_segs: list[EffectiveDutySegment] = []
            for p_seg in (pattern.segments or []):
                if p_seg.day_of_week == on_date.weekday():
                    cross = p_seg.end_time <= p_seg.start_time
                    start_dt = datetime.combine(on_date, p_seg.start_time).replace(tzinfo=tz)
                    end_dt = datetime.combine(
                        on_date + timedelta(days=1 if cross else 0), p_seg.end_time
                    ).replace(tzinfo=tz)
                    day_segs.append(
                        EffectiveDutySegment(
                            id=p_seg.id,
                            start_at=start_dt,
                            end_at=end_dt,
                            start_time=p_seg.start_time,
                            end_time=p_seg.end_time,
                            duty_kind=p_seg.duty_kind or "OTHER",
                            presence_type=p_seg.presence_type or "ONSITE",
                            is_bookable=p_seg.is_bookable,
                            shift_type_id=p_seg.shift_type_id,
                            shift_name=p_seg.shift_type.name if p_seg.shift_type else None,
                            department_id=p_seg.department_id,
                            department_name=p_seg.department.name if p_seg.department else None,
                            notes=p_seg.notes,
                            source="pattern",
                            is_cross_midnight=cross,
                        )
                    )
            return ("working" if day_segs else "off"), day_segs

        # 4. Legacy default HospitalUser.shift_id fallback
        if staff_user and staff_user.shift_id:
            st = (
                self.db.query(ShiftType)
                .filter(
                    ShiftType.id == staff_user.shift_id,
                    ShiftType.hospital_id == self.hospital_id,
                    ShiftType.is_active.is_(True),
                )
                .first()
            )
            if st:
                cross = st.end_time <= st.start_time
                start_dt = datetime.combine(on_date, st.start_time).replace(tzinfo=tz)
                end_dt = datetime.combine(
                    on_date + timedelta(days=1 if cross else 0), st.end_time
                ).replace(tzinfo=tz)
                is_opd = is_doctor or "OPD" in (st.name or "").upper()
                return "working", [
                    EffectiveDutySegment(
                        id=None,
                        start_at=start_dt,
                        end_at=end_dt,
                        start_time=st.start_time,
                        end_time=st.end_time,
                        duty_kind="OPD" if is_opd else "OTHER",
                        presence_type="ONSITE",
                        is_bookable=is_opd,
                        shift_type_id=st.id,
                        shift_name=st.name,
                        department_id=st.department_id,
                        source="default_shift",
                        is_cross_midnight=cross,
                    )
                ]

        # 5. No fabricated availability: unscheduled staff member safely defaults to unscheduled (off)
        return "off", []

    def get_effective_duty_segments(
        self,
        user_id: UUID,
        on_date: date,
    ) -> list[EffectiveDutySegment]:
        """
        Canonical evaluator answering: 'What is this employee actually scheduled to do during this date?'
        Includes:
        - All duty segments scheduled on on_date (according to precedence hierarchy)
        - Overnight duty segments carrying over from previous calendar day (e.g. 23:00 to 02:00)
        """
        tz = get_hospital_tz(self.db, self.hospital_id)
        status_val, current_day_segs = self._resolve_raw_day_segments(user_id, on_date, tz)

        if status_val == "leave":
            return []

        all_segs: list[EffectiveDutySegment] = list(current_day_segs)

        # Carryover from previous calendar day
        prev_date = on_date - timedelta(days=1)
        prev_status, prev_day_segs = self._resolve_raw_day_segments(user_id, prev_date, tz)
        if prev_status == "working":
            start_of_current_day = datetime.combine(on_date, time.min).replace(tzinfo=tz)
            for p_seg in prev_day_segs:
                if p_seg.end_at > start_of_current_day:
                    carryover_seg = replace(p_seg, source="carryover")
                    all_segs.append(carryover_seg)

        all_segs.sort(key=lambda s: s.start_at)
        return all_segs

    def get_rest_fatigue_warnings(self, segments: list[EffectiveDutySegment]) -> list[str]:
        """Detect suspicious transitions or inadequate rest periods (< 8h) between consecutive duty segments."""
        warnings: list[str] = []
        if len(segments) < 2:
            return warnings

        sorted_segs = sorted(segments, key=lambda s: s.start_at)
        for i in range(len(sorted_segs) - 1):
            prev = sorted_segs[i]
            curr = sorted_segs[i + 1]
            if curr.start_at > prev.end_at:
                gap_hours = (curr.start_at - prev.end_at).total_seconds() / 3600.0
                if 0 < gap_hours < 8.0:
                    warnings.append(
                        f"Short rest interval ({gap_hours:.1f}h) between duty ending at {prev.end_time.strftime('%H:%M')} and duty starting at {curr.start_time.strftime('%H:%M')}"
                    )
        return warnings


class EffectiveDoctorAvailabilityService(StaffScheduleService):
    """Canonical availability and scheduling conflict evaluator for doctors and clinical staff."""

    def get_doctor(self, doctor_id: UUID, *, for_update: bool = False) -> HospitalUser | None:
        return self.get_staff_user(doctor_id, for_update=for_update)

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
                local_dt = dt.replace(tzinfo=tz)
            else:
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

        tz = get_hospital_tz(self.db, self.hospital_id)
        raw_status, _ = self._resolve_raw_day_segments(doctor_id, query_date, tz)
        if raw_status in ("off", "leave"):
            status_text = "off-duty" if raw_status == "off" else "on leave"
            return DoctorDailyAvailability(
                doctor_id=doctor_id,
                doctor_name=doctor.name,
                date=query_date,
                available=False,
                reason=f"Doctor is marked {status_text} in the roster on this date",
            )

        # 3. Canonical effective duty segments
        effective_segments = self.get_effective_duty_segments(doctor_id, query_date)
        if not effective_segments:
            return DoctorDailyAvailability(
                doctor_id=doctor_id,
                doctor_name=doctor.name,
                date=query_date,
                available=False,
                reason="Doctor is not scheduled for duty on this date",
            )

        # 4. Filter bookable intervals (only eligible duties create routine appointment slots)
        bookable_segments = [s for s in effective_segments if s.is_bookable and s.duty_kind in ("OPD",)]
        # If doctor has duties (like Ward, OT, Emergency) but no bookable OPD duty:
        if not bookable_segments:
            duties_str = ", ".join(sorted({s.duty_kind for s in effective_segments}))
            return DoctorDailyAvailability(
                doctor_id=doctor_id,
                doctor_name=doctor.name,
                date=query_date,
                available=False,
                reason=f"Doctor is scheduled for non-consultation duties ({duties_str}) on this date",
                segments=[s.to_dict() for s in effective_segments],
            )

        # 5. Approved leaves check
        leaves = self.get_leaves_for_date(doctor_id, query_date)
        # Check if full-day leave covers all bookable intervals
        full_day = next(
            (
                l
                for l in leaves
                if all(l.start_time <= s.start_time and l.end_time >= s.end_time for s in bookable_segments)
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
                shift_start=bookable_segments[0].start_time.strftime("%H:%M") if len(bookable_segments) == 1 else None,
                shift_end=bookable_segments[0].end_time.strftime("%H:%M") if len(bookable_segments) == 1 else None,
                segments=[s.to_dict() for s in effective_segments],
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

        # 8. Compute discrete booked slots for slot pickers across bookable duty intervals
        booked_slots: set[str] = set()
        for seg in bookable_segments:
            curr_min = time_to_minutes(seg.start_time)
            end_min = time_to_minutes(seg.end_time) if not seg.is_cross_midnight else (24 * 60)
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

        # Compatibility shift_start / shift_end:
        # If exactly one bookable segment exists, report its bounds.
        # If multiple segments exist, do not emit a misleading concatenated range.
        compat_start = bookable_segments[0].start_time.strftime("%H:%M") if len(bookable_segments) == 1 else None
        compat_end = bookable_segments[0].end_time.strftime("%H:%M") if len(bookable_segments) == 1 else None

        rest_warnings = self.get_rest_fatigue_warnings(effective_segments)

        return DoctorDailyAvailability(
            doctor_id=doctor_id,
            doctor_name=doctor.name,
            date=query_date,
            available=True,
            reason=None,
            shift_start=compat_start,
            shift_end=compat_end,
            booked_slots=sorted(list(booked_slots)),
            leave_blocks=leave_blocks,
            blocked_intervals=blocked_intervals,
            segments=[s.to_dict() for s in effective_segments],
            rest_warnings=rest_warnings,
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
        purpose: str = "appointment",
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

        tz = get_hospital_tz(self.db, self.hospital_id)
        raw_status, _ = self._resolve_raw_day_segments(doctor_id, query_date, tz)
        if raw_status in ("off", "leave"):
            status_text = "off-duty" if raw_status == "off" else "on leave"
            return (
                False,
                f"Doctor is marked {status_text} in the daily roster on {query_date}",
                "ROSTER_OFF",
            )

        effective_segments = self.get_effective_duty_segments(doctor_id, query_date)
        if not effective_segments:
            return (
                False,
                f"Doctor is not scheduled on duty on {query_date}",
                "ROSTER_OFF",
            )

        # 3. Check duty boundaries
        slot_start_dt = datetime.combine(query_date, start_time).replace(tzinfo=tz)
        slot_end_dt = slot_start_dt + timedelta(minutes=duration_minutes)

        if purpose == "appointment":
            # Must fall entirely inside at least one eligible bookable segment
            eligible_segments = [s for s in effective_segments if s.is_bookable and s.duty_kind in ("OPD",)]
            fits_in_segment = any(
                seg.start_at <= slot_start_dt and slot_end_dt <= seg.end_at
                for seg in eligible_segments
            )
            if not fits_in_segment:
                end_time = add_minutes_to_time(start_time, duration_minutes)
                if eligible_segments:
                    ranges = ", ".join(
                        f"{s.start_time.strftime('%H:%M')}–{s.end_time.strftime('%H:%M')}"
                        for s in eligible_segments
                    )
                    msg = f"Requested slot {start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')} falls outside doctor's active consultation duty intervals ({ranges})"
                else:
                    msg = f"Doctor has no eligible OPD consultation duty scheduled on {query_date}"
                return False, msg, "OUTSIDE_SHIFT"
        elif purpose == "ot":
            # For OT surgery: surgeon must be scheduled on duty
            pass

        # 4. Approved leaves
        leaves = self.get_leaves_for_date(doctor_id, query_date)
        for l in leaves:
            l_start_dt = datetime.combine(query_date, l.start_time).replace(tzinfo=tz)
            l_end_dt = datetime.combine(query_date, l.end_time).replace(tzinfo=tz)
            if dt_intervals_overlap(slot_start_dt, slot_end_dt, l_start_dt, l_end_dt):
                reason_detail = f": {l.reason}" if l.reason else ""
                return (
                    False,
                    f"Doctor has approved leave from {l.start_time.strftime('%H:%M')} to {l.end_time.strftime('%H:%M')}{reason_detail}",
                    "DOCTOR_LEAVE",
                )

        # 5. Existing OPD appointments
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
            a_start_dt = datetime.combine(query_date, a.appointment_time).replace(tzinfo=tz)
            a_end_dt = a_start_dt + timedelta(minutes=a_dur)
            if dt_intervals_overlap(slot_start_dt, slot_end_dt, a_start_dt, a_end_dt):
                return (
                    False,
                    f"Doctor already has an appointment from {a.appointment_time.strftime('%H:%M')} to {a_end_dt.time().strftime('%H:%M')}",
                    "APPOINTMENT_CONFLICT",
                )

        # 6. Existing OT Surgeries
        surgeries = self.get_ot_surgeries_for_date(
            doctor_id, query_date, exclude_id=exclude_ot_id
        )
        for s, s_start, s_end in surgeries:
            s_start_dt = datetime.combine(query_date, s_start).replace(tzinfo=tz)
            s_end_dt = datetime.combine(query_date, s_end).replace(tzinfo=tz)
            if dt_intervals_overlap(slot_start_dt, slot_end_dt, s_start_dt, s_end_dt):
                return (
                    False,
                    f"Doctor is scheduled for OT surgery {s.surgery_no} ({s.surgery_type}) from {s_start.strftime('%H:%M')} to {s_end.strftime('%H:%M')}",
                    "OT_CONFLICT",
                )

        return True, None, None


# Canonical architecture alias
DoctorAvailabilityService = EffectiveDoctorAvailabilityService
