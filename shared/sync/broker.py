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


def _enqueue(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    """Put an event onto a subscriber queue, evicting the oldest if saturated."""
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        try:
            _ = queue.get_nowait()
            queue.put_nowait(event)
        except Exception:
            pass


class SyncBroker:
    """Manages active SSE subscriber queues and event delivery per hospital."""

    def __init__(self, max_history_per_hospital: int = 200) -> None:
        self._max_history = max_history_per_hospital
        # Each subscriber is (queue, loop) so publish() can safely wake the
        # correct event loop from sync worker threads.
        self._subscribers: dict[str, set[tuple[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop]]] = {}
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, hospital_id: str | UUID) -> asyncio.Queue[dict[str, Any]]:
        hid = str(hospital_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
        with self._lock:
            if hid not in self._subscribers:
                self._subscribers[hid] = set()
            self._subscribers[hid].add((queue, loop))
        return queue

    def unsubscribe(self, hospital_id: str | UUID, queue: asyncio.Queue[dict[str, Any]]) -> None:
        hid = str(hospital_id)
        with self._lock:
            if hid not in self._subscribers:
                return
            to_remove = [entry for entry in self._subscribers[hid] if entry[0] is queue]
            for entry in to_remove:
                self._subscribers[hid].discard(entry)
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
        if details:
            event["details"] = details

        with self._lock:
            # Store in ring buffer for catchup
            if hid not in self._history:
                self._history[hid] = deque(maxlen=self._max_history)
            self._history[hid].append(event)

            # Distribute to subscriber queues
            subscribers = list(self._subscribers.get(hid, set()))

        for queue, loop in subscribers:
            try:
                if loop.is_running():
                    loop.call_soon_threadsafe(_enqueue, queue, event)
                else:
                    _enqueue(queue, event)
            except Exception:
                logger.debug("sync publish dropped for closed subscriber", exc_info=True)

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
