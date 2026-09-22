"""In-memory real-time synchronization broker for cross-session updates.

Provides lightweight event pub/sub partitioned by hospital_id.
Thread-safe for publishers running in sync SQLAlchemy worker threads
and async FastAPI SSE consumers.
"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import threading
import time
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger("hms.sync")


class SyncBroker:
    """Manages active SSE subscriber queues and event delivery per hospital."""

    def __init__(self, max_history_per_hospital: int = 200) -> None:
        self._max_history = max_history_per_hospital
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, hospital_id: str | UUID) -> asyncio.Queue[dict[str, Any]]:
        hid = str(hospital_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        with self._lock:
            if hid not in self._subscribers:
                self._subscribers[hid] = set()
            self._subscribers[hid].add(queue)
        return queue

    def unsubscribe(self, hospital_id: str | UUID, queue: asyncio.Queue[dict[str, Any]]) -> None:
        hid = str(hospital_id)
        with self._lock:
            if hid in self._subscribers:
                self._subscribers[hid].discard(queue)
                if not self._subscribers[hid]:
                    del self._subscribers[hid]

    def publish(
        self,
        hospital_id: str | UUID,
        entity_type: str,
        action: str,
        entity_id: str | UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hid = str(hospital_id)
        event: dict[str, Any] = {
            "event_id": f"evt_{uuid4().hex[:12]}",
            "hospital_id": hid,
            "entity_type": entity_type.lower().strip(),
            "action": action.lower().strip(),
            "entity_id": str(entity_id) if entity_id else None,
            "timestamp": int(time.time() * 1000),
        }

        with self._lock:
            # Store in ring buffer for catchup
            if hid not in self._history:
                self._history[hid] = deque(maxlen=self._max_history)
            self._history[hid].append(event)

            # Distribute to subscriber queues
            queues = list(self._subscribers.get(hid, set()))

        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Evict oldest event if client queue is saturated to prevent memory leak
                try:
                    _ = q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

        return event

    def get_events_since(self, hospital_id: str | UUID, since_timestamp: int) -> list[dict[str, Any]]:
        hid = str(hospital_id)
        with self._lock:
            history = self._history.get(hid)
            if not history:
                return []
            return [e for e in history if e.get("timestamp", 0) > since_timestamp]


# Global singleton instance for the process
sync_broker = SyncBroker()
