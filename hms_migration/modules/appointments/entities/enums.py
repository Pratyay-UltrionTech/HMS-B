"""
Appointment domain enums.

Conforms to UltrionTech-Backend-Template modules/appointments/entities/ specification.
"""

import enum


class AppointmentStatus(str, enum.Enum):
    """Lifecycle statuses for outpatient appointment visits."""

    scheduled = "scheduled"
    waiting = "waiting"  # checked in / in queue
    completed = "completed"
    transferred_to_inpatient = "transferred_to_inpatient"
    ipd_transfer_requested = "ipd_transfer_requested"
    cancelled = "cancelled"
    no_show = "no_show"
