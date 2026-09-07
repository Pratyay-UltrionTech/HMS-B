"""
Domain-specific exceptions for the Vitals module.

Conforms to UltrionTech-Backend-Template modules/<name>/exceptions specification.
Inherits from shared base exception classes, eliminating HTTP coupling from domain logic.
"""

from hms_migration.shared.exceptions.base import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


class VitalAppointmentNotFoundError(NotFoundError):
    """Raised when the specified appointment does not exist in the hospital scope."""

    def __init__(self, message: str = "Appointment not found") -> None:
        super().__init__(message=message, code="APPOINTMENT_NOT_FOUND")


class VitalReadingNotFoundError(NotFoundError):
    """Raised when the specified vital reading does not exist."""

    def __init__(self, message: str = "Vital reading not found") -> None:
        super().__init__(message=message, code="VITAL_NOT_FOUND")


class VitalMutationNotAllowedError(ConflictError):
    """Raised when an appointment status blocks recording or modifying vitals."""

    def __init__(self, message: str = "Cannot change vitals on this visit") -> None:
        super().__init__(message=message, code="VITAL_MUTATION_DISALLOWED")


class VitalValidationError(ValidationError):
    """Raised when vitals query filters or measurement parameters are invalid."""

    def __init__(self, message: str = "Validation error") -> None:
        super().__init__(message=message, code="VITAL_VALIDATION_ERROR")
