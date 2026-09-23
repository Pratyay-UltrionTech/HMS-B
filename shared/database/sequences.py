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


def ensure_counter_at_least(
    db: Session, hospital_id: uuid.UUID, counter_name: str, minimum: int
) -> int:
    """Bump a counter forward so its next value exceeds ``minimum``.

    Self-healing for counters introduced after rows already existed with
    the old read-MAX-then-+1 numbering (e.g. RCPT-2026-00001 created before
    the atomic counter existed). Returns the counter's current value after
    the bump. Safe to call repeatedly; never moves the counter backwards.
    """
    current = (
        db.query(SequenceCounter.current_val)
        .filter(
            SequenceCounter.hospital_id == hospital_id,
            SequenceCounter.counter_name == counter_name,
        )
        .scalar()
    )
    if current is None:
        db.add(
            SequenceCounter(
                hospital_id=hospital_id,
                counter_name=counter_name,
                current_val=minimum,
            )
        )
        db.flush()
        return minimum
    if current < minimum:
        db.query(SequenceCounter).filter(
            SequenceCounter.hospital_id == hospital_id,
            SequenceCounter.counter_name == counter_name,
        ).update({"current_val": minimum}, synchronize_session=False)
        db.flush()
        return minimum
    return current


def next_uhid(db: Session, hospital_id: uuid.UUID) -> str:
    """Generate the next unique UHID (P0001 format) atomically for a hospital."""
    return f"P{next_sequence_value(db, hospital_id, 'uhid'):04d}"


def next_invoice_number(db: Session, hospital_id: uuid.UUID, year: int | None = None) -> str:
    """Generate sequential invoice number (INV-YYYY-NNNNN format) atomically for a hospital."""
    from datetime import date
    y = year or date.today().year
    counter_name = f"invoice_{y}"
    seq = next_sequence_value(db, hospital_id, counter_name)
    return f"INV-{y}-{seq:05d}"


def next_receipt_number(db: Session, hospital_id: uuid.UUID, year: int | None = None) -> str:
    """Generate sequential payment receipt number (RCPT-YYYY-NNNNN format) atomically for a hospital."""
    from datetime import date
    y = year or date.today().year
    counter_name = f"receipt_{y}"
    seq = next_sequence_value(db, hospital_id, counter_name)
    return f"RCPT-{y}-{seq:05d}"


def next_deposit_number(db: Session, hospital_id: uuid.UUID, year: int | None = None) -> str:
    """Generate sequential deposit voucher number (DEP-YYYY-NNNNN format) atomically for a hospital."""
    from datetime import date
    y = year or date.today().year
    counter_name = f"deposit_{y}"
    seq = next_sequence_value(db, hospital_id, counter_name)
    return f"DEP-{y}-{seq:05d}"


def next_refund_number(db: Session, hospital_id: uuid.UUID, year: int | None = None) -> str:
    """Generate sequential refund voucher number (REF-YYYY-NNNNN format) atomically for a hospital."""
    from datetime import date
    y = year or date.today().year
    counter_name = f"refund_{y}"
    seq = next_sequence_value(db, hospital_id, counter_name)
    return f"REF-{y}-{seq:05d}"

