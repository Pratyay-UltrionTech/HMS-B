"""
Explicit real-time synchronization event staging.

Decouples sync broker event publication from audit logging.
Staged events on `session.info['pending_sync_events']` are dispatched
only upon successful transaction commit, and discarded on rollback.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session

from shared.sync.broker import sync_broker

logger = logging.getLogger("hms.sync.events")


def stage_sync_event(
    session: Any,
    hospital_id: str | UUID,
    entity_type: str,
    action: str,
    entity_id: str | UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Stage a real-time sync event to be published upon session commit."""
    if not session:
        return
    raw_session = getattr(session, "sync_session", session)
    if hasattr(raw_session, "info"):
        raw_session.info.setdefault("pending_sync_events", []).append(
            (hospital_id, entity_type, action, str(entity_id) if entity_id is not None else None, details)
        )


@event.listens_for(Session, "after_commit")
def _dispatch_pending_sync_events(session: Session) -> None:
    events = session.info.pop("pending_sync_events", None) if hasattr(session, "info") else None
    if not events:
        return
    for item in events:
        try:
            if len(item) >= 5:
                h_id, ent_type, act, ent_id, details = item[:5]
                sync_broker.publish(h_id, ent_type, act, ent_id, details=details)
            else:
                h_id, ent_type, act, ent_id = item
                sync_broker.publish(h_id, ent_type, act, ent_id)
        except Exception as exc:
            logger.debug("Failed to dispatch staged sync event: %s", exc)


@event.listens_for(Session, "after_rollback")
def _clear_pending_sync_events(session: Session) -> None:
    if hasattr(session, "info"):
        session.info.pop("pending_sync_events", None)
