"""
Canonical Hospital Clock & Timezone Resolution Service.

Provides hospital-local clock helpers backed by IANA timezones (ZoneInfo)
and project-declared tzdata.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def resolve_hospital_tz(tz_str: str | None) -> ZoneInfo:
    """Resolve a hospital timezone string to a ZoneInfo instance.

    Rules:
    - If configured and valid IANA timezone name: return ZoneInfo(tz_str).
    - If missing or empty: fall back safely to canonical platform default (UTC).
    - If invalid (typo, unknown zone): log warning and fall back safely to UTC.
    Never silently default invalid or unknown timezones to Asia/Kolkata.
    """
    if not tz_str or not tz_str.strip():
        return ZoneInfo("UTC")

    cleaned = tz_str.strip()
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        logger.warning(
            "Invalid hospital timezone %r; falling back safely to UTC. Error: %s",
            cleaned,
            exc,
        )
        return ZoneInfo("UTC")


def get_hospital_tz(db: Session, hospital_id: UUID) -> ZoneInfo:
    """Query Hospital entity and return its resolved ZoneInfo."""
    from modules.tenancy.entities.hospital import Hospital

    row = db.query(Hospital.timezone).filter(Hospital.id == hospital_id).first()
    tz_str = row[0] if row and row[0] else None
    return resolve_hospital_tz(tz_str)


def hospital_now(db: Session, hospital_id: UUID, now_utc: datetime | None = None) -> datetime:
    """Return the current instant expressed in the hospital-local timezone."""
    tz = get_hospital_tz(db, hospital_id)
    if now_utc is None:
        return datetime.now(tz)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(tz)


def hospital_today(db: Session, hospital_id: UUID, now_utc: datetime | None = None) -> date:
    """Return the current calendar date in the hospital's local timezone."""
    return hospital_now(db, hospital_id, now_utc=now_utc).date()


def get_hospital_local_date(db: Session, hospital_id: UUID, now_utc: datetime | None = None) -> date:
    """Backward-compatible alias for hospital_today."""
    return hospital_today(db, hospital_id, now_utc=now_utc)
