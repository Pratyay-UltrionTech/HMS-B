"""Shared exceptions package."""

from shared.exceptions.base import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from shared.exceptions.handlers import register_exception_handlers

__all__ = [
    "AppError",
    "NotFoundError",
    "ValidationError",
    "ConflictError",
    "UnauthorizedError",
    "ForbiddenError",
    "register_exception_handlers",
]
