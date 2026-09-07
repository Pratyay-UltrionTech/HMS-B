"""Shared middleware package."""

from hms_migration.shared.middleware.request_logging import RequestLogMiddleware

__all__ = ["RequestLogMiddleware"]
