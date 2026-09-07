"""
Domain exceptions for the Appointments module.

Conforms to UltrionTech-Backend-Template modules/appointments/exceptions/ specification.
Maps domain errors cleanly to HTTP status codes via shared global exception handlers.
"""

from hms_migration.shared.exceptions import ConflictError, NotFoundError, ValidationError


class AppointmentNotFoundError(NotFoundError):
    """Raised when an appointment record is not found."""

    def __init__(self, message: str = "Appointment not found") -> None:
        super().__init__(message=message)


class AppointmentValidationError(ValidationError):
    """Raised when appointment booking or update details are invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


class AppointmentConflictError(ConflictError):
    """Raised when a doctor schedule conflict or double booking occurs."""

    def __init__(self, message: str = "Doctor already has an appointment at this time") -> None:
        super().__init__(message=message)
