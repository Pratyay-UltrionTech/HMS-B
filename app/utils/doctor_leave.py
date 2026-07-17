"""Doctor shift bounds and leave overlap checks for scheduling."""

from datetime import date, time
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Appointment, AppointmentStatus, AppointmentType, DoctorLeave, HospitalUser, ShiftType

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


def get_shift_bounds(doctor: HospitalUser) -> tuple[time, time, str | None]:
    shift: ShiftType | None = doctor.shift
    if shift and shift.is_active:
        return shift.start_time, shift.end_time, shift.name
    return DEFAULT_SHIFT_START, DEFAULT_SHIFT_END, None


def intervals_overlap(start_a: time, end_a: time, start_b: time, end_b: time) -> bool:
    a0, a1 = time_to_minutes(start_a), time_to_minutes(end_a)
    b0, b1 = time_to_minutes(start_b), time_to_minutes(end_b)
    if a1 <= a0 or b1 <= b0:
        return False
    return a0 < b1 and b0 < a1


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


def leave_for_slot(
    db: Session,
    hospital_id: UUID,
    doctor_id: UUID,
    on_date: date,
    slot_time: time,
    slot_minutes: int | None = None,
) -> DoctorLeave | None:
    duration = slot_minutes or DEFAULT_SLOT_MINUTES
    slot_end = add_minutes_to_time(slot_time, duration)
    leaves = (
        db.query(DoctorLeave)
        .filter(
            DoctorLeave.hospital_id == hospital_id,
            DoctorLeave.doctor_id == doctor_id,
            DoctorLeave.leave_date == on_date,
        )
        .all()
    )
    for leave in leaves:
        if intervals_overlap(slot_time, slot_end, leave.start_time, leave.end_time):
            return leave
    return None


def list_leaves_for_date(
    db: Session,
    hospital_id: UUID,
    doctor_id: UUID,
    on_date: date,
) -> list[DoctorLeave]:
    return (
        db.query(DoctorLeave)
        .filter(
            DoctorLeave.hospital_id == hospital_id,
            DoctorLeave.doctor_id == doctor_id,
            DoctorLeave.leave_date == on_date,
        )
        .order_by(DoctorLeave.start_time.asc())
        .all()
    )


def list_leaves_in_range(
    db: Session,
    hospital_id: UUID,
    doctor_id: UUID,
    date_from: date,
    date_to: date,
) -> list[DoctorLeave]:
    return (
        db.query(DoctorLeave)
        .filter(
            DoctorLeave.hospital_id == hospital_id,
            DoctorLeave.doctor_id == doctor_id,
            DoctorLeave.leave_date >= date_from,
            DoctorLeave.leave_date <= date_to,
        )
        .order_by(DoctorLeave.leave_date.asc(), DoctorLeave.start_time.asc())
        .all()
    )


def appointments_blocking_leave(
    db: Session,
    hospital_id: UUID,
    doctor_id: UUID,
    on_date: date,
    start_time: time,
    end_time: time,
    slot_minutes: int,
) -> list[Appointment]:
    rows = (
        db.query(Appointment)
        .options()
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.doctor_id == doctor_id,
            Appointment.appointment_date == on_date,
            Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
        )
        .all()
    )
    blocked: list[Appointment] = []
    for appt in rows:
        appt_end = add_minutes_to_time(appt.appointment_time, slot_minutes)
        if intervals_overlap(start_time, end_time, appt.appointment_time, appt_end):
            blocked.append(appt)
    return blocked


def leave_blocks_overlap_existing(
    db: Session,
    hospital_id: UUID,
    doctor_id: UUID,
    on_date: date,
    start_time: time,
    end_time: time,
    exclude_leave_id: UUID | None = None,
) -> DoctorLeave | None:
    q = db.query(DoctorLeave).filter(
        DoctorLeave.hospital_id == hospital_id,
        DoctorLeave.doctor_id == doctor_id,
        DoctorLeave.leave_date == on_date,
    )
    if exclude_leave_id:
        q = q.filter(DoctorLeave.id != exclude_leave_id)
    for existing in q.all():
        if intervals_overlap(start_time, end_time, existing.start_time, existing.end_time):
            return existing
    return None


def iter_shift_slots(shift_start: time, shift_end: time, step_minutes: int) -> list[time]:
    """Times at which an appointment slot may start (within shift, slot fits before shift end)."""
    start_m = time_to_minutes(shift_start)
    end_m = time_to_minutes(shift_end)
    if end_m <= start_m:
        return []
    slots: list[time] = []
    t = start_m
    while t + step_minutes <= end_m:
        slots.append(minutes_to_time(t))
        t += step_minutes
    return slots


def time_within_shift(value: time, shift_start: time, shift_end: time) -> bool:
    v = time_to_minutes(value)
    return time_to_minutes(shift_start) <= v < time_to_minutes(shift_end)
