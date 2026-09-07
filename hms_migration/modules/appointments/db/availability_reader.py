"""
Database reader for doctor availability, schedules, leaves, and holidays.

Conforms to UltrionTech-Backend-Template modules/appointments/db/ specification.
"""

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Date, String, Time, column, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus

# SQLAlchemy Core table definitions for scheduling metadata
_holidays = table(
    "holidays",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("holiday_date", Date),
    column("name", String),
)

_doctor_leaves = table(
    "doctor_leaves",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("doctor_id", PG_UUID(as_uuid=True)),
    column("leave_date", Date),
    column("start_time", Time),
    column("end_time", Time),
    column("reason", String),
)

_hospital_users = table(
    "hospital_users",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("role_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("email", String),
    column("phone", String),
    column("specialization", String),
    column("is_active", Boolean),
    column("shift_id", PG_UUID(as_uuid=True)),
)

_staff_roles = table(
    "staff_roles",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("name", String),
)

_shift_types = table(
    "shift_types",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("start_time", String),
    column("end_time", String),
    column("is_active", Boolean),
)


class AvailabilityReader:
    """Encapsulates schedule, shift, holiday, and conflict queries."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_holiday(self, check_date: date) -> dict[str, Any] | None:
        """Fetch active holiday for date if one exists."""
        stmt = (
            select(_holidays.c.id, _holidays.c.name)
            .where(
                _holidays.c.hospital_id == self.hospital_id,
                _holidays.c.holiday_date == check_date,
            )
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row:
            return {"id": row[0], "name": row[1]}
        return None

    def has_slot_conflict(
        self,
        doctor_id: UUID,
        appt_date: date,
        appt_time: time,
        exclude_id: UUID | None = None,
    ) -> bool:
        """Check if doctor has an active appointment booked at exact date and time."""
        q = (
            self.db.query(Appointment.id)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.doctor_id == doctor_id,
                Appointment.appointment_date == appt_date,
                Appointment.appointment_time == appt_time,
                Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
            )
        )
        if exclude_id:
            q = q.filter(Appointment.id != exclude_id)
        return q.first() is not None

    def get_doctor(self, doctor_id: UUID) -> dict[str, Any] | None:
        """Fetch active doctor details."""
        stmt = (
            select(
                _hospital_users.c.id,
                _hospital_users.c.name,
                _hospital_users.c.shift_id,
                _staff_roles.c.name.label("role_name"),
            )
            .select_from(
                _hospital_users.outerjoin(
                    _staff_roles, _hospital_users.c.role_id == _staff_roles.c.id
                )
            )
            .where(
                _hospital_users.c.hospital_id == self.hospital_id,
                _hospital_users.c.id == doctor_id,
                _hospital_users.c.is_active.is_(True),
            )
        )
        row = self.db.execute(stmt).mappings().first()
        return dict(row) if row else None

    def list_leaves_for_doctor(self, doctor_id: UUID, on_date: date) -> list[dict[str, Any]]:
        """Fetch leave intervals for a doctor on a specific date."""
        stmt = select(
            _doctor_leaves.c.start_time,
            _doctor_leaves.c.end_time,
            _doctor_leaves.c.reason,
        ).where(
            _doctor_leaves.c.hospital_id == self.hospital_id,
            _doctor_leaves.c.doctor_id == doctor_id,
            _doctor_leaves.c.leave_date == on_date,
        )
        rows = self.db.execute(stmt).all()
        leaves = []
        for r in rows:
            st = r[0].strftime("%H:%M") if hasattr(r[0], "strftime") else str(r[0])[:5]
            et = r[1].strftime("%H:%M") if hasattr(r[1], "strftime") else str(r[1])[:5]
            leaves.append({"start": st, "end": et, "reason": r[2]})
        return leaves

    def get_booked_slots(self, doctor_id: UUID, on_date: date) -> list[str]:
        """Fetch formatted booked slot times (HH:MM) for a doctor on a specific date."""
        rows = (
            self.db.query(Appointment.appointment_time)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.doctor_id == doctor_id,
                Appointment.appointment_date == on_date,
                Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
            )
            .all()
        )
        return [r[0].strftime("%H:%M") for r in rows if r[0]]

    def get_shift_bounds(self, shift_id: UUID | None) -> tuple[str | None, str | None]:
        """Fetch shift start and end times."""
        if not shift_id:
            return None, None
        stmt = select(_shift_types.c.start_time, _shift_types.c.end_time).where(
            _shift_types.c.hospital_id == self.hospital_id,
            _shift_types.c.id == shift_id,
            _shift_types.c.is_active.is_(True),
        )
        row = self.db.execute(stmt).first()
        if row:
            st = row[0].strftime("%H:%M") if hasattr(row[0], "strftime") else str(row[0])[:5]
            et = row[1].strftime("%H:%M") if hasattr(row[1], "strftime") else str(row[1])[:5]
            return st, et
        return None, None
