"""Shared middleware package."""

from shared.middleware.request_logging import RequestLogMiddleware

__all__ = ["RequestLogMiddleware"]
