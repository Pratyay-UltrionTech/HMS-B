"""
Domain validation rules for the Appointments module.

Conforms to UltrionTech-Backend-Template modules/appointments/validators/ specification.
"""

from datetime import date, datetime, time

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.appointments.exceptions.appointments_exceptions import (
    AppointmentValidationError,
)


class AppointmentsValidator:
    """Validator for appointment booking and state transitions."""

    @staticmethod
    def assert_not_past_slot(appointment_date: date, appointment_time: time) -> None:
        """Prevent booking appointments in the past."""
        today = date.today()
        if appointment_date < today:
            raise AppointmentValidationError("Cannot book an appointment slot in the past")
        if appointment_date == today:
            now_time = datetime.now().time()
            if appointment_time < now_time:
                raise AppointmentValidationError("Cannot book an appointment slot in the past")

    @staticmethod
    def assert_can_check_in(appt: Appointment) -> None:
        """Validate appointment visit is in a state eligible for check-in."""
        if appt.status in {
            AppointmentStatus.completed,
            AppointmentStatus.transferred_to_inpatient,
            AppointmentStatus.cancelled,
            AppointmentStatus.no_show,
        }:
            raise AppointmentValidationError(
                f"Cannot check in appointment with status {appt.status.value}"
            )

    @staticmethod
    def assert_can_reschedule(appt: Appointment) -> None:
        """Validate appointment visit can be rescheduled."""
        if appt.status in {
            AppointmentStatus.completed,
            AppointmentStatus.transferred_to_inpatient,
            AppointmentStatus.cancelled,
            AppointmentStatus.no_show,
        }:
            raise AppointmentValidationError(
                f"Cannot reschedule appointment with status {appt.status.value}"
            )
