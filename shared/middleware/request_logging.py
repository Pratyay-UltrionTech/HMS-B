"""
Request logging middleware.

Conforms to UltrionTech-Backend-Template shared/middleware/ specification.
Provides structured request and response access logging identical to OG HMS-B.

Implemented as pure ASGI middleware (not BaseHTTPMiddleware) so long-lived
responses such as SSE streams are not buffered or deadlocked.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("hms.api")


class RequestLogMiddleware:
    """Logs incoming HTTP request method and path, and outgoing status code."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = scope.get("path", "?")
        logger.info("→ %s %s", method, path)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                logger.info("← %s %s %s", method, path, message.get("status"))
            await send(message)

        await self.app(scope, receive, send_wrapper)
