"""
Doctor shift, scheduling, and leave conflict domain services.

Conforms to UltrionTech-Backend-Template modules/doctors/services/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, time
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.doctors.contracts.doctor_contracts import (
    LEAVE_TYPES,
    LeaveConflictDay,
    LeaveConflictDetail,
)
from hms_migration.modules.doctors.entities.doctor import DoctorLeave, HospitalUser, ShiftType

DEFAULT_SHIFT_START = time(9, 0)
DEFAULT_SHIFT_END = time(17, 0)
DEFAULT_SLOT_MINUTES = 15


def fmt_time_hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def minutes_to_time(total: int) -> time:
    total = max(0, min(total, 24 * 60 - 1))
    return time(total // 60, total % 60)


def add_minutes_to_time(value: time, minutes: int) -> time:
    return minutes_to_time(time_to_minutes(value) + minutes)


def intervals_overlap(start_a: time, end_a: time, start_b: time, end_b: time) -> bool:
    a0, a1 = time_to_minutes(start_a), time_to_minutes(end_a)
    b0, b1 = time_to_minutes(start_b), time_to_minutes(end_b)
    if a1 <= a0 or b1 <= b0:
        return False
    return a0 < b1 and b0 < a1


def get_shift_bounds(doctor: HospitalUser) -> tuple[time, time, str | None]:
    shift: ShiftType | None = doctor.shift
    if shift and shift.is_active:
        return shift.start_time, shift.end_time, shift.name
    return DEFAULT_SHIFT_START, DEFAULT_SHIFT_END, None


def slot_duration_minutes(db: Session, hospital_id: UUID) -> int:
    row = (
        db.query(AppointmentType.slot_duration_minutes)
        .filter(AppointmentType.hospital_id == hospital_id, AppointmentType.is_active.is_(True))
        .order_by(AppointmentType.name.asc())
        .first()
    )
    if row and row[0]:
        return int(row[0])
    return DEFAULT_SLOT_MINUTES


def normalize_leave_type(raw: str | None) -> str:
    value = (raw or "Personal").strip()
    for opt in LEAVE_TYPES:
        if opt.lower() == value.lower():
            return opt
    if value:
        return value[:32]
    return "Personal"


def resolve_leave_times(
    doctor: HospitalUser,
    *,
    full_day: bool,
    start_time: time | None,
    end_time: time | None,
) -> tuple[time, time]:
    shift_start, shift_end, _ = get_shift_bounds(doctor)
    if full_day:
        return shift_start, shift_end
    if start_time is None or end_time is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="From time and To time are required for partial-day leave",
        )
    if start_time >= end_time:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="End time must be after start time"
        )
    s0, s1 = time_to_minutes(shift_start), time_to_minutes(shift_end)
    a0, a1 = time_to_minutes(start_time), time_to_minutes(end_time)
    if a0 < s0 or a1 > s1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Leave must be within your shift ({fmt_time_hhmm(shift_start)}–{fmt_time_hhmm(shift_end)})",
        )
    return start_time, end_time


def conflict_http_exception(blocked_by_date: dict[date, list[Appointment]]) -> HTTPException:
    conflicts: list[LeaveConflictDay] = []
    total = 0
    for d in sorted(blocked_by_date.keys()):
        times = [fmt_time_hhmm(a.appointment_time) for a in blocked_by_date[d]]
        total += len(times)
        conflicts.append(LeaveConflictDay(date=d, times=times))
    detail = LeaveConflictDetail(
        code="appointment_conflict",
        message=f"Leave conflicts with {total} appointment{'s' if total != 1 else ''}.",
        conflicts=conflicts,
    )
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail.model_dump(mode="json"))


def check_leave_conflicts(
    db: Session,
    hospital_id: UUID,
    doctor_id: UUID,
    dates: list[date],
    start_time: time,
    end_time: time,
    slot_minutes: int,
) -> None:
    appts = (
        db.query(Appointment)
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.doctor_id == doctor_id,
            Appointment.appointment_date.in_(dates),
            Appointment.status.notin_(
                [
                    AppointmentStatus.cancelled,
                    AppointmentStatus.no_show,
                ]
            ),
        )
        .all()
    )
    blocked_by_date: dict[date, list[Appointment]] = {}
    for a in appts:
        appt_end = add_minutes_to_time(a.appointment_time, slot_minutes)
        if intervals_overlap(a.appointment_time, appt_end, start_time, end_time):
            blocked_by_date.setdefault(a.appointment_date, []).append(a)

    if blocked_by_date:
        raise conflict_http_exception(blocked_by_date)

    # Check for overlapping existing leaves
    for d in dates:
        existing = (
            db.query(DoctorLeave)
            .filter(
                DoctorLeave.hospital_id == hospital_id,
                DoctorLeave.doctor_id == doctor_id,
                DoctorLeave.leave_date == d,
            )
            .all()
        )
        for ex in existing:
            if intervals_overlap(start_time, end_time, ex.start_time, ex.end_time):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Leave overlaps an existing leave block",
                )

