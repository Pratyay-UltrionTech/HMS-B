"""
Atomic, tenant-scoped sequence counters.

Provides collision-free sequential identifier generation without the O(N)
brute-force loop previously used for UHIDs (see HMS-FLAW-028).

The counter is stored in the ``sequence_counters`` table and advanced with a
single atomic ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` statement, so
concurrent callers each receive a strictly unique, gap-free value with no
iterative SQL and no read-modify-write race. This works on both PostgreSQL
(the production target) and SQLite (the isolated test harness).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column, Session

from infrastructure.postgres.base import Base


class SequenceCounter(Base):
    """Per-(hospital, counter) atomic counter used for sequential identifiers."""

    __tablename__ = "sequence_counters"

    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    counter_name: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    current_val: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def next_sequence_value(db: Session, hospital_id: uuid.UUID, counter_name: str) -> int:
    """Atomically advance and return the next value of a tenant-scoped counter.

    Uses an upsert that either inserts a fresh counter (current_val = 1) or
    increments an existing one by 1, returning the new value in the same atomic
    statement. Safe under concurrent writers.
    """
    values = {
        "hospital_id": hospital_id,
        "counter_name": counter_name,
        "current_val": 1,
    }
    if db.get_bind().dialect.name == "postgresql":
        stmt = pg_insert(SequenceCounter).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SequenceCounter.hospital_id, SequenceCounter.counter_name],
            set_={"current_val": SequenceCounter.current_val + 1},
        ).returning(SequenceCounter.current_val)
    else:
        stmt = sqlite_insert(SequenceCounter).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SequenceCounter.hospital_id, SequenceCounter.counter_name],
            set_={"current_val": SequenceCounter.current_val + 1},
        ).returning(SequenceCounter.current_val)
    return db.execute(stmt).scalar_one()


def next_uhid(db: Session, hospital_id: uuid.UUID) -> str:
    """Generate the next unique UHID (P0001 format) atomically for a hospital."""
    return f"P{next_sequence_value(db, hospital_id, 'uhid'):04d}"
