"""Server-Sent Events (SSE) and sync endpoints for cross-role real-time updates."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from config.settings import Settings, get_settings
from shared.sync.broker import sync_broker

logger = logging.getLogger("hms.sync")

router = APIRouter(prefix="/sync", tags=["sync"])
security_optional = HTTPBearer(auto_error=False)


def get_sync_user(
    token_param: str | None = Query(default=None, alias="token"),
    auth_header: HTTPAuthorizationCredentials | None = Depends(security_optional),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Authenticate via either Bearer header or ?token= query parameter (required for EventSource)."""
    raw_token = None
    if auth_header and auth_header.credentials:
        raw_token = auth_header.credentials
    elif token_param:
        raw_token = token_param

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token required",
        )

    try:
        payload = jwt.decode(
            raw_token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc

    role = payload.get("role")
    if role not in {"super_admin", "hospital_admin", "hospital_staff"}:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token role",
        )
    if not payload.get("hospital_uuid"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hospital context missing from token",
        )
    return payload


@router.get("/events")
async def sync_events_stream(
    request: Request,
    user: dict[str, Any] = Depends(get_sync_user),
    since: int | None = Query(default=None, description="Replay missed events since timestamp ms"),
):
    """Real-time SSE stream delivering entity change notifications for the caller's hospital."""
    hospital_id = str(user["hospital_uuid"])
    queue = sync_broker.subscribe(hospital_id)

    async def event_generator():
        try:
            # 1. Connection established greeting
            init_msg = {
                "type": "connected",
                "hospital_id": hospital_id,
                "timestamp": int(time.time() * 1000),
            }
            yield f"data: {json.dumps(init_msg)}\n\n"

            # 2. Replay any historical events since client's last seen timestamp
            if since and since > 0:
                for past_event in sync_broker.get_events_since(hospital_id, since):
                    yield f"data: {json.dumps(past_event)}\n\n"

            # 3. Stream incoming events + keepalives
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # SSE comment keepalive to prevent timeouts through reverse proxies
                    yield ":keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sync_broker.unsubscribe(hospital_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/poll")
def poll_sync_events(
    since: int = Query(..., description="Timestamp in epoch ms to fetch events after"),
    user: dict[str, Any] = Depends(get_sync_user),
):
    """Fallback polling endpoint for environments where streaming SSE is blocked."""
    hospital_id = str(user["hospital_uuid"])
    events = sync_broker.get_events_since(hospital_id, since)
    return {
        "events": events,
        "server_time": int(time.time() * 1000),
    }
