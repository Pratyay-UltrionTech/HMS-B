"""
Appointment data access adapter for the Vitals module boundary.

Conforms to UltrionTech-Backend-Template domain boundary guidelines.
Isolates cross-domain Appointment lookups from VitalsRepository while preserving
exact database query filters, eager loading, and transaction lifecycle.
"""

from datetime import date
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.appointments.entities.appointment import Appointment, AppointmentStatus


class AppointmentReader:
    """Read and status update adapter for appointments accessed by vitals workflows."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_appointment_by_id(self, appointment_id: UUID) -> Appointment | None:
        """Fetch an appointment by ID scoped to this hospital with patient/doctor loaded."""
        return (
            self.db.query(Appointment)
            .options(
                joinedload(Appointment.patient),
                joinedload(Appointment.doctor),
            )
            .filter(
                Appointment.id == appointment_id,
                Appointment.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_today_appointments(self, on_date: date) -> list[Appointment]:
        """Fetch today's non-cancelled appointments for this hospital, ordered chronologically."""
        return (
            self.db.query(Appointment)
            .options(
                joinedload(Appointment.patient),
                joinedload(Appointment.doctor),
            )
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.appointment_date == on_date,
                Appointment.status != AppointmentStatus.cancelled,
            )
            .order_by(Appointment.appointment_time.asc())
            .all()
        )

    def get_next_queue_token(self, doctor_id: UUID, on_date: date) -> int:
        """Calculate the next sequential queue token for a doctor on a given date."""
        current = (
            self.db.query(func.max(Appointment.queue_token))
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.doctor_id == doctor_id,
                Appointment.appointment_date == on_date,
            )
            .scalar()
        )
        return int(current or 0) + 1

    def revert_orphaned_waiting_appointments(
        self,
        appointments: list[Appointment],
        appointment_ids_with_vitals: set[UUID],
    ) -> None:
        """Keep status Scheduled until vitals exist (preserves OG walk-in check-in fix)."""
        if not appointments:
            return
        dirty = False
        for a in appointments:
            if a.status == AppointmentStatus.waiting and a.id not in appointment_ids_with_vitals:
                a.status = AppointmentStatus.scheduled
                a.checked_in_at = None
                dirty = True
        if dirty:
            self.db.commit()
            for a in appointments:
                self.db.refresh(a)
