"""
Database repository for the Analytics module.

Conforms to UltrionTech-Backend-Template modules/analytics/db/ specification.
Encapsulates all database read queries for platform-level metrics using pure
SQLAlchemy Core constructs, completely independent of app.models or app.database.
"""

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    String,
    column,
    func,
    select,
    table,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session

# Pure SQLAlchemy Core table definitions mapped to the existing database schema
_hospitals = table(
    "hospitals",
    column("id", UUID(as_uuid=True)),
    column("hospital_id", String),
    column("name", String),
    column("plan", String),
    column("is_active", Boolean),
    column("created_at", DateTime(timezone=True)),
)

_hospital_users = table(
    "hospital_users",
    column("id", UUID(as_uuid=True)),
    column("is_active", Boolean),
)

_patients = table(
    "patients",
    column("id", UUID(as_uuid=True)),
    column("created_at", DateTime(timezone=True)),
)


_role_permissions = table(
    "role_permissions",
    column("module_key", String),
    column("can_view", Boolean),
)


class AnalyticsRepository:
    """Repository managing analytical queries and read-only aggregations."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_all_hospitals(self) -> list[dict[str, Any]]:
        """Fetch all hospitals for platform-wide metrics."""
        stmt = select(
            _hospitals.c.id,
            _hospitals.c.hospital_id,
            _hospitals.c.name,
            _hospitals.c.plan,
            _hospitals.c.is_active,
            _hospitals.c.created_at,
        )
        rows = self.db.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_distinct_permission_modules_count(self) -> int:
        """Count distinct module keys where view permission is granted."""
        stmt = (
            select(func.count(func.distinct(_role_permissions.c.module_key)))
            .where(_role_permissions.c.can_view.is_(True))
        )
        val = self.db.execute(stmt).scalar()
        return int(val or 0)

    def get_active_staff_users_count(self) -> int:
        """Count active hospital staff users across the platform."""
        stmt = (
            select(func.count(_hospital_users.c.id))
            .where(_hospital_users.c.is_active.is_(True))
        )
        val = self.db.execute(stmt).scalar()
        return int(val or 0)

    def get_total_patients_count(self) -> int:
        """Count total patients registered across all hospitals."""
        stmt = select(func.count(_patients.c.id))
        val = self.db.execute(stmt).scalar()
        return int(val or 0)

    def get_patient_counts_by_month(self, since: datetime) -> dict[str, int]:
        """Return {"YYYY-MM": count} of patients created on/after `since`, grouped in SQL.

        Replaces pulling every patient row platform-wide into Python just to
        bucket by month — this aggregates in the database instead.
        """
        # Truncate in UTC explicitly — date_trunc on a timestamptz otherwise
        # uses the DB session's timezone, which could shift month boundaries.
        bucket = func.date_trunc("month", func.timezone("UTC", _patients.c.created_at))
        stmt = (
            select(bucket.label("bucket"), func.count(_patients.c.id))
            .where(_patients.c.created_at >= since)
            .group_by(bucket)
        )
        return {row[0].strftime("%Y-%m"): int(row[1]) for row in self.db.execute(stmt).all() if row[0]}

    def get_recent_hospitals(self, limit: int = 8) -> list[dict[str, Any]]:
        """Fetch recently onboarded hospitals ordered by creation timestamp."""
        stmt = (
            select(
                _hospitals.c.id,
                _hospitals.c.hospital_id,
                _hospitals.c.name,
                _hospitals.c.plan,
                _hospitals.c.is_active,
                _hospitals.c.created_at,
            )
            .order_by(_hospitals.c.created_at.desc())
            .limit(limit)
        )
        rows = self.db.execute(stmt).mappings().all()
        return [dict(r) for r in rows]
