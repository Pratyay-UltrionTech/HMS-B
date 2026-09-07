"""
Action to check doctor availability on a given date.

Conforms to UltrionTech-Backend-Template modules/appointments/actions/ specification.
"""

from datetime import date
from uuid import UUID

from hms_migration.modules.appointments.contracts.appointments_contracts import (
    DoctorAvailability,
    LeaveBlock,
)
from hms_migration.modules.appointments.db.availability_reader import AvailabilityReader
from hms_migration.modules.appointments.exceptions.appointments_exceptions import (
    AppointmentNotFoundError,
)


class CheckAvailabilityAction:
    """Calculates doctor schedule, leave blocks, booked slots, and shift bounds."""

    def __init__(self, avail_reader: AvailabilityReader) -> None:
        self.avail_reader = avail_reader

    def execute(self, doctor_id: UUID, on_date: date) -> DoctorAvailability:
        doctor = self.avail_reader.get_doctor(doctor_id)
        if not doctor:
            raise AppointmentNotFoundError("Doctor not found")

        holiday = self.avail_reader.get_holiday(on_date)
        if holiday:
            return DoctorAvailability(
                doctor_id=doctor_id,
                doctor_name=doctor["name"],
                date=on_date,
                available=False,
                reason=f"Hospital closed — {holiday['name']}",
                booked_slots=[],
                leave_blocks=[],
                shift_start=None,
                shift_end=None,
            )

        leave_rows = self.avail_reader.list_leaves_for_doctor(doctor_id, on_date)
        leave_blocks = [
            LeaveBlock(start=l["start"], end=l["end"], reason=l.get("reason"))
            for l in leave_rows
        ]

        booked_slots = self.avail_reader.get_booked_slots(doctor_id, on_date)
        shift_start, shift_end = self.avail_reader.get_shift_bounds(doctor.get("shift_id"))

        return DoctorAvailability(
            doctor_id=doctor_id,
            doctor_name=doctor["name"],
            date=on_date,
            available=True,
            reason=None,
            booked_slots=booked_slots,
            leave_blocks=leave_blocks,
            shift_start=shift_start,
            shift_end=shift_end,
        )
