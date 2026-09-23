"""
Apply the missing HMS flaw schema migrations:

  - FLAW-002 : create the partial unique index ``uq_appointments_active_slot``
               on appointments (hospital_id, doctor_id, appointment_date,
               appointment_time) WHERE status NOT IN ('cancelled','no_show'),
               matching the ORM entity.  Sanitizes any legacy duplicate active
               appointment slots first so the index can be created.
  - FLAW-009 : add the amendment columns (``is_amended``, ``amendment_reason``)
               to the radiology orders table, matching the ORM entity.
  - FLAW-017 : add the amendment columns to the lab orders table and the
               ``is_panic`` column to the lab results table, matching the ORM
               entities.
  - FLAW-025 : create the ``bed_stay_segments`` table if it does not exist and
               backfill an initial segment for every ACTIVE admission that does
               not yet have one, replicating the field population performed by
               admission_actions.py.

This follows the repo's standalone-schema-script convention (there is no
Alembic setup in this project).  It is idempotent and safe to re-run.

Run with: python -m scripts.fix_missing_flaw_migrations
Add --dry-run to only report what would change without altering anything.
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hms.scripts.fix_missing_flaw_migrations")

# Statuses considered "active" for the appointment slot index (matches the ORM
# partial index predicate: status NOT IN ('cancelled', 'no_show')).
NON_ACTIVE_APPOINTMENT_STATUSES = ("cancelled", "no_show")

# Open-episode statuses for admissions (matches uq_admissions_active_*,
# Invariant 1: requested/admitted/discharge_requested).
ACTIVE_ADMISSION_STATUSES = ("requested", "admitted", "discharge_requested")


# ---------------------------------------------------------------------------
# FLAW-002: uq_appointments_active_slot partial unique index
# ---------------------------------------------------------------------------
def _sanitize_duplicate_active_appointments(conn: Connection, inspector, dry_run: bool) -> None:
    """Mark legacy duplicate active appointment slots cancelled so the partial
    unique index can be created.

    Keeps the earliest-scheduled row per (hospital, doctor, appointment_date,
    appointment_time); any additional active rows that would violate the new
    uniqueness are marked cancelled with an explanatory note.
    """
    if "appointments" not in inspector.get_table_names():
        logger.info("SKIP appointments sanitization: table does not exist")
        return
    active_sql = ", ".join(f"'{s}'" for s in NON_ACTIVE_APPOINTMENT_STATUSES)
    dup_sql = text(
        f"""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY hospital_id, doctor_id, appointment_date, appointment_time
                       ORDER BY created_at ASC, id ASC
                   ) AS rn
            FROM appointments
            WHERE status NOT IN ({active_sql})
        )
        SELECT id FROM ranked WHERE rn > 1
        """
    )
    dup_ids = conn.execute(dup_sql).scalars().all()
    if not dup_ids:
        logger.info("No duplicate active appointment slots found")
        return
    if dry_run:
        logger.info(
            "WOULD SANITIZE %d duplicate active appointment slot(s): %s",
            len(dup_ids), list(dup_ids),
        )
        return
    conn.execute(
        text(
            "UPDATE appointments SET status = 'cancelled', "
            "notes = COALESCE(notes || '; ', '') || "
            "'[FLAW-002] Auto-sanitized duplicate active appointment slot' "
            "WHERE id = ANY(:ids)"
        ),
        {"ids": list(dup_ids)},
    )
    conn.commit()
    logger.info("SANITIZED %d duplicate active appointment slot(s)", len(dup_ids))


def _create_appointments_active_slot_index(conn: Connection, inspector, dry_run: bool) -> None:
    if "appointments" not in inspector.get_table_names():
        logger.info("SKIP appointments index: table does not exist")
        return
    existing = {i["name"] for i in inspector.get_indexes("appointments")}
    if "uq_appointments_active_slot" in existing:
        logger.info("SKIP uq_appointments_active_slot: already exists")
        return
    sql = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_appointments_active_slot "
        "ON appointments (hospital_id, doctor_id, appointment_date, appointment_time) "
        "WHERE status NOT IN ('cancelled', 'no_show')"
    )
    if dry_run:
        logger.info("WOULD CREATE: %s", sql)
        return
    conn.execute(text(sql))
    conn.commit()
    logger.info("CREATED uq_appointments_active_slot")


# ---------------------------------------------------------------------------
# FLAW-009: radiology_orders amendment columns
# ---------------------------------------------------------------------------
def _add_radiology_order_amendment_columns(conn: Connection, inspector, dry_run: bool) -> None:
    if "radiology_orders" not in inspector.get_table_names():
        logger.info("SKIP radiology_orders amendment columns: table does not exist")
        return
    cols = {c["name"] for c in inspector.get_columns("radiology_orders")}
    statements = [
        (
            "is_amended",
            "ALTER TABLE radiology_orders "
            "ADD COLUMN IF NOT EXISTS is_amended BOOLEAN NOT NULL DEFAULT FALSE",
        ),
        (
            "amendment_reason",
            "ALTER TABLE radiology_orders "
            "ADD COLUMN IF NOT EXISTS amendment_reason TEXT NULL",
        ),
    ]
    for name, stmt in statements:
        if name in cols:
            logger.info("SKIP radiology_orders.%s: column already exists", name)
            continue
        if dry_run:
            logger.info("WOULD ADD radiology_orders.%s", name)
            continue
        conn.execute(text(stmt))
        conn.commit()
        logger.info("ADDED radiology_orders.%s", name)
    if dry_run:
        return
    conn.execute(
        text(
            "UPDATE radiology_orders SET is_amended = FALSE "
            "WHERE is_amended IS NULL"
        )
    )
    conn.commit()


# ---------------------------------------------------------------------------
# FLAW-017: lab_orders amendment columns + lab_results is_panic
# ---------------------------------------------------------------------------
def _add_lab_amendment_columns(conn: Connection, inspector, dry_run: bool) -> None:
    for table, specs in (
        (
            "lab_orders",
            [
                (
                    "is_amended",
                    "ALTER TABLE lab_orders "
                    "ADD COLUMN IF NOT EXISTS is_amended BOOLEAN NOT NULL DEFAULT FALSE",
                ),
                (
                    "amendment_reason",
                    "ALTER TABLE lab_orders "
                    "ADD COLUMN IF NOT EXISTS amendment_reason TEXT NULL",
                ),
            ],
        ),
        (
            "lab_results",
            [
                (
                    "is_panic",
                    "ALTER TABLE lab_results "
                    "ADD COLUMN IF NOT EXISTS is_panic BOOLEAN NOT NULL DEFAULT FALSE",
                ),
            ],
        ),
    ):
        if table not in inspector.get_table_names():
            logger.info("SKIP %s amendment columns: table does not exist", table)
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        for name, stmt in specs:
            if name in cols:
                logger.info("SKIP %s.%s: column already exists", table, name)
                continue
            if dry_run:
                logger.info("WOULD ADD %s.%s", table, name)
                continue
            conn.execute(text(stmt))
            conn.commit()
            logger.info("ADDED %s.%s", table, name)
    if dry_run:
        return
    for table, col, default in (
        ("lab_orders", "is_amended", "FALSE"),
        ("lab_results", "is_panic", "FALSE"),
    ):
        if table in inspector.get_table_names():
            conn.execute(
                text(
                    f"UPDATE {table} SET {col} = {default} WHERE {col} IS NULL"
                )
            )
    conn.commit()


# ---------------------------------------------------------------------------
# FLAW-025: bed_stay_segments table + backfill for active admissions
# ---------------------------------------------------------------------------
BED_STAY_SEGMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bed_stay_segments (
    id UUID PRIMARY KEY,
    hospital_id UUID NOT NULL REFERENCES hospitals (id) ON DELETE CASCADE,
    admission_id UUID NOT NULL REFERENCES admissions (id) ON DELETE CASCADE,
    ward_id UUID REFERENCES wards (id) ON DELETE SET NULL,
    room_id UUID REFERENCES rooms (id) ON DELETE SET NULL,
    bed_id UUID REFERENCES beds (id) ON DELETE SET NULL,
    rate_per_day DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _create_bed_stay_segments_table(conn: Connection, inspector, dry_run: bool) -> None:
    if "bed_stay_segments" in inspector.get_table_names():
        logger.info("SKIP bed_stay_segments: table already exists")
        return
    if dry_run:
        logger.info("WOULD CREATE bed_stay_segments table")
        return
    conn.execute(text(BED_STAY_SEGMENTS_SCHEMA))
    conn.commit()
    logger.info("CREATED bed_stay_segments table")


def _backfill_initial_bed_stay_segments(conn: Connection, inspector, dry_run: bool) -> None:
    """Insert an initial bed_stay_segments row for every ACTIVE admission that
    does not yet have one.

    Replicates admission_actions.py's field population:
      ward_id     = admission.ward_id
      room_id     = admission.room_id
      bed_id      = admission.bed_id
      rate_per_day = ward.bed_charge_per_day (0.0 if unresolvable)
      started_at  = admission.admitted_at
      ended_at    = NULL
    """
    if "admissions" not in inspector.get_table_names():
        logger.info("SKIP bed_stay_segments backfill: admissions table does not exist")
        return
    if "bed_stay_segments" not in inspector.get_table_names():
        logger.info("SKIP bed_stay_segments backfill: table does not exist")
        return
    active_sql = ", ".join(f"'{s}'" for s in ACTIVE_ADMISSION_STATUSES)
    pending_sql = text(
        f"""
        SELECT a.id, a.hospital_id, a.ward_id, a.room_id, a.bed_id, a.admitted_at,
               w.bed_charge_per_day
        FROM admissions a
        LEFT JOIN wards w ON w.id = a.ward_id
        WHERE a.status IN ({active_sql})
          AND NOT EXISTS (
              SELECT 1 FROM bed_stay_segments s WHERE s.admission_id = a.id
          )
        """
    )
    rows = conn.execute(pending_sql).mappings().all()
    if not rows:
        logger.info("No active admissions missing an initial bed_stay_segment")
        return
    if dry_run:
        logger.info(
            "WOULD backfill %d initial bed_stay_segment(s) for active admissions",
            len(rows),
        )
        return
    insert = text(
        """
        INSERT INTO bed_stay_segments
            (id, hospital_id, admission_id, ward_id, room_id, bed_id,
             rate_per_day, started_at, ended_at)
        VALUES
            (:id, :hospital_id, :admission_id, :ward_id, :room_id, :bed_id,
             :rate_per_day, :started_at, NULL)
        """
    )
    for row in rows:
        conn.execute(
            insert,
            {
                "id": _new_uuid(),
                "hospital_id": row["hospital_id"],
                "admission_id": row["id"],
                "ward_id": row["ward_id"],
                "room_id": row["room_id"],
                "bed_id": row["bed_id"],
                "rate_per_day": float(row["bed_charge_per_day"] or 0.0),
                "started_at": row["admitted_at"],
            },
        )
    conn.commit()
    logger.info("BACKFILLED %d initial bed_stay_segment(s)", len(rows))


def _new_uuid():
    import uuid

    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(dry_run: bool = False) -> None:
    engine = get_transitional_sync_engine()
    inspector = inspect(engine)
    with engine.connect() as conn:
        # FLAW-002
        _sanitize_duplicate_active_appointments(conn, inspector, dry_run)
        _create_appointments_active_slot_index(conn, inspector, dry_run)
        # FLAW-009
        _add_radiology_order_amendment_columns(conn, inspector, dry_run)
        # FLAW-017
        _add_lab_amendment_columns(conn, inspector, dry_run)
        # FLAW-025
        _create_bed_stay_segments_table(conn, inspector, dry_run)
        _backfill_initial_bed_stay_segments(conn, inspector, dry_run)
    logger.info("Done%s.", " (dry-run)" if dry_run else "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
