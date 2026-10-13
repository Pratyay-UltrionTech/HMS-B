"""
Action to check doctor availability on a given date.

Conforms to UltrionTech-Backend-Template modules/appointments/actions/ specification.
"""

from datetime import date
from uuid import UUID

from modules.appointments.contracts.appointments_contracts import (
    DoctorAvailability,
    LeaveBlock,
)
from modules.appointments.db.availability_reader import AvailabilityReader
from modules.appointments.exceptions.appointments_exceptions import (
    AppointmentNotFoundError,
)
from modules.doctors.services.effective_availability_service import (
    EffectiveDoctorAvailabilityService,
)


class CheckAvailabilityAction:
    """Calculates doctor schedule, leave blocks, booked slots, and shift bounds using canonical service."""

    def __init__(self, avail_reader: AvailabilityReader) -> None:
        self.avail_reader = avail_reader

    def execute(self, doctor_id: UUID, on_date: date) -> DoctorAvailability:
        svc = EffectiveDoctorAvailabilityService(self.avail_reader.db, self.avail_reader.hospital_id)
        daily = svc.get_daily_availability(doctor_id, on_date)
        if daily.reason == "Doctor not found":
            raise AppointmentNotFoundError("Doctor not found")

        leave_blocks = [
            LeaveBlock(start=l["start"], end=l["end"], reason=l.get("reason"))
            for l in daily.leave_blocks
        ]

        return DoctorAvailability(
            doctor_id=doctor_id,
            doctor_name=daily.doctor_name,
            date=on_date,
            available=daily.available,
            reason=daily.reason,
            booked_slots=daily.booked_slots,
            leave_blocks=leave_blocks,
            shift_start=daily.shift_start,
            shift_end=daily.shift_end,
        )
