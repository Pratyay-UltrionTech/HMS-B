"""Vitals exceptions package."""

from hms_migration.modules.vitals.exceptions.vitals_exceptions import (
    VitalAppointmentNotFoundError,
    VitalMutationNotAllowedError,
    VitalReadingNotFoundError,
    VitalValidationError,
)

__all__ = [
    "VitalAppointmentNotFoundError",
    "VitalReadingNotFoundError",
    "VitalMutationNotAllowedError",
    "VitalValidationError",
]
