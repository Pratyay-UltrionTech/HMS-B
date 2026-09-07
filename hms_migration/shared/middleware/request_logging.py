"""
Request logging middleware.

Conforms to UltrionTech-Backend-Template shared/middleware/ specification.
Provides structured request and response access logging identical to OG HMS-B.
"""

import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("hms.api")


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Logs incoming HTTP request method and path, and outgoing status code."""

    async def dispatch(self, request: Request, call_next):
        logger.info("→ %s %s", request.method, request.url.path)
        response = await call_next(request)
        logger.info("← %s %s %s", request.method, request.url.path, response.status_code)
        return response
