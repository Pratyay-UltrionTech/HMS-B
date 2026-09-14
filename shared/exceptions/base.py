"""
Base domain exception classes for the application.

Conforms to UltrionTech-Backend-Template shared/exceptions/ specification.
These exceptions represent domain and application error conditions independent
of HTTP framework concerns.
"""

from typing import Any


class AppError(Exception):
    """Base exception for all application-specific errors."""

    def __init__(
        self,
        message: str,
        code: str = "APP_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}

    def __str__(self) -> str:
        return self.message


class NotFoundError(AppError):
    """Raised when an entity or resource is not found (maps to HTTP 404)."""

    def __init__(
        self,
        message: str = "Resource not found",
        code: str = "NOT_FOUND",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, details=details)


class ValidationError(AppError):
    """Raised when input validation or clinical parameters fail (maps to HTTP 400)."""

    def __init__(
        self,
        message: str = "Validation error",
        code: str = "VALIDATION_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, details=details)


class ConflictError(AppError):
    """Raised on invalid entity state transitions or business conflicts (maps to HTTP 400)."""

    def __init__(
        self,
        message: str = "State conflict",
        code: str = "STATE_CONFLICT",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, details=details)


class UnauthorizedError(AppError):
    """Raised when authentication credentials are missing or invalid (maps to HTTP 401)."""

    def __init__(
        self,
        message: str = "Invalid or expired token",
        code: str = "UNAUTHORIZED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, details=details)


class ForbiddenError(AppError):
    """Raised when permissions or tenancy context are insufficient (maps to HTTP 403)."""

    def __init__(
        self,
        message: str = "Access forbidden",
        code: str = "FORBIDDEN",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, details=details)
