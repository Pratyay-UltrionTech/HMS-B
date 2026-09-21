"""
Apply the Group 1 database integrity & relational schema fixes:

  - HMS-FLAW-015  : change patient FK ondelete from CASCADE to RESTRICT so
                    deleting a patient can never cascade-purge financial
                    (billing_charges/payments/invoices/receipts) or clinical
                    (prescriptions, medical_records, patient_documents,
                    admissions, ipd_form_submissions) records; add the
                    ``patients.is_deleted`` soft-delete flag.
  - HMS-FLAW-027  : add partial unique indexes on active admissions so a bed
                    and a patient each have at most one active admission.
                    Sanitizes any legacy duplicate active admissions first.
  - HMS-FLAW-028  : create the ``sequence_counters`` table backing atomic UHID
                    generation and backfill the per-hospital ``uhid`` counter
                    from the highest existing numeric UHID so the new atomic
                    sequence never collides with legacy data.

This follows the repo's standalone-schema-script convention (there is no
Alembic setup in this project). It is idempotent and safe to re-run.

Run with: python -m scripts.fix_db_integrity_and_relations
Add --dry-run to only report what would change without altering anything.
"""

from __future__ import annotations

import argparse
import logging
import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hms.scripts.fix_db_integrity_and_relations")

# (table, column) -> column that references patients.id and currently has an
# ON DELETE CASCADE FK constraint that must become RESTRICT.
PATIENT_FK_TABLES = [
    ("billing_charges", "patient_id"),
    ("billing_payments", "patient_id"),
    ("billing_invoices", "patient_id"),
    ("billing_receipts", "patient_id"),
    ("prescriptions", "patient_id"),
    ("medical_records", "patient_id"),
    ("patient_documents", "patient_id"),
    ("admissions", "patient_id"),
    ("ipd_form_submissions", "patient_id"),
]

ACTIVE_STATUSES = ("admitted", "discharge_requested")

_UHID_NUM_RE = re.compile(r"^P(\d+)$")


def _fk_constraint_names(inspector, table_name: str, column: str) -> list[str]:
    """Return the names of FK constraints on ``table`` that reference patients(column)."""
    names: list[str] = []
    for fk in inspector.get_foreign_keys(table_name):
        cols = fk.get("constrained_columns") or []
        if column in cols and fk.get("referred_table") == "patients":
            names.append(fk["name"])
    return names


def _switch_patient_fk_to_restrict(conn: Connection, inspector, dry_run: bool) -> None:
    for table, column in PATIENT_FK_TABLES:
        if table not in inspector.get_table_names():
            logger.info("SKIP %s.%s -> patients.id: table does not exist", table, column)
            continue
        for fk_name in _fk_constraint_names(inspector, table, column):
            if not fk_name:
                continue
            # Only alter constraints that still cascade.
            fk_meta = next(
                (f for f in inspector.get_foreign_keys(table) if f.get("name") == fk_name),
                None,
            )
            ondelete = (fk_meta or {}).get("options", {}).get("ondelete", "")
            if ondelete != "CASCADE":
                logger.info("SKIP %s: already %r (not CASCADE)", fk_name, ondelete)
                continue
            drop_sql = f'ALTER TABLE "{table}" DROP CONSTRAINT "{fk_name}"'
            add_sql = (
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{fk_name}" '
                f'FOREIGN KEY ({column}) REFERENCES "patients" (id) ON DELETE RESTRICT'
            )
            if dry_run:
                logger.info("WOULD ALTER %s:\n  %s\n  %s", fk_name, drop_sql, add_sql)
                continue
            conn.execute(text(drop_sql))
            conn.execute(text(add_sql))
            conn.commit()
            logger.info("ALTERED %s -> ON DELETE RESTRICT", fk_name)


def _add_is_deleted_column(conn: Connection, inspector, dry_run: bool) -> None:
    if "patients" not in inspector.get_table_names():
        logger.info("SKIP patients.is_deleted: table does not exist")
        return
    cols = {c["name"] for c in inspector.get_columns("patients")}
    if "is_deleted" in cols:
        logger.info("SKIP patients.is_deleted: column already exists")
        return
    if dry_run:
        logger.info("WOULD ADD patients.is_deleted")
        return
    conn.execute(
        text(
            'ALTER TABLE "patients" ADD COLUMN "is_deleted" BOOLEAN NOT NULL DEFAULT FALSE'
        )
    )
    conn.commit()
    logger.info("ADDED patients.is_deleted")


def _sanitize_duplicate_active_admissions(conn: Connection, inspector, dry_run: bool) -> None:
    """Mark legacy duplicate active admissions discharged so partial indexes can apply.

    Keeps the earliest admitted row per (hospital, bed) and per (hospital,
    patient); any additional active rows that would violate the new uniqueness
    are marked discharged with an explanatory note.
    """
    if "admissions" not in inspector.get_table_names():
        logger.info("SKIP admissions sanitization: table does not exist")
        return
    active_sql = ", ".join(f"'{s}'" for s in ACTIVE_STATUSES)
    dup_sql = text(
        f"""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY hospital_id, bed_id
                       ORDER BY admitted_at ASC, id ASC
                   ) AS rn_bed,
                   ROW_NUMBER() OVER (
                       PARTITION BY hospital_id, patient_id
                       ORDER BY admitted_at ASC, id ASC
                   ) AS rn_patient
            FROM admissions
            WHERE status IN ({active_sql})
        )
        SELECT id FROM ranked WHERE rn_bed > 1 OR rn_patient > 1
        """
    )
    dup_ids = conn.execute(dup_sql).scalars().all()
    if not dup_ids:
        logger.info("No duplicate active admissions found")
        return
    if dry_run:
        logger.info("WOULD SANITIZE %d duplicate active admission(s): %s",
                    len(dup_ids), list(dup_ids))
        return
    conn.execute(
        text(
            "UPDATE admissions SET status = 'discharged', "
            "discharge_notes = COALESCE(discharge_notes || '; ', '') || "
            "'[FLAW-027] Auto-sanitized duplicate active admission' "
            "WHERE id = ANY(:ids)"
        ),
        {"ids": list(dup_ids)},
    )
    conn.commit()
    logger.info("SANITIZED %d duplicate active admission(s)", len(dup_ids))


def _create_partial_indexes(conn: Connection, inspector, dry_run: bool) -> None:
    if "admissions" not in inspector.get_table_names():
        logger.info("SKIP admissions partial indexes: table does not exist")
        return
    active_sql = ", ".join(f"'{s}'" for s in ACTIVE_STATUSES)
    specs = [
        ("uq_admissions_active_bed", "hospital_id, bed_id"),
        ("uq_admissions_active_patient", "hospital_id, patient_id"),
    ]
    existing = {i["name"] for i in inspector.get_indexes("admissions")}
    for name, cols in specs:
        if name in existing:
            logger.info("SKIP %s: already exists", name)
            continue
        sql = (
            f'CREATE UNIQUE INDEX "{name}" ON "admissions" ({cols}) '
            f'WHERE status IN ({active_sql})'
        )
        if dry_run:
            logger.info("WOULD CREATE %s: %s", name, sql)
            continue
        conn.execute(text(sql))
        conn.commit()
        logger.info("CREATED %s", name)


def _create_sequence_counters_and_backfill(conn: Connection, inspector, dry_run: bool) -> None:
    if "sequence_counters" not in inspector.get_table_names():
        if dry_run:
            logger.info("WOULD CREATE sequence_counters table")
        else:
            conn.execute(text(
                """
                CREATE TABLE sequence_counters (
                    hospital_id UUID NOT NULL,
                    counter_name VARCHAR(64) NOT NULL,
                    current_val INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (hospital_id, counter_name)
                )
                """
            ))
            conn.commit()
            logger.info("CREATED sequence_counters table")
    else:
        logger.info("SKIP sequence_counters: table already exists")

    # Backfill the per-hospital UHID counter from the highest existing numeric UHID.
    if "patients" in inspector.get_table_names():
        backfill_sql = text(
            """
            INSERT INTO sequence_counters (hospital_id, counter_name, current_val)
            SELECT hospital_id,
                   'uhid' AS counter_name,
                   MAX(CAST(SUBSTRING(uhid FROM 2) AS INTEGER)) AS current_val
            FROM patients
            WHERE uhid ~ '^P[0-9]+$'
            GROUP BY hospital_id
            ON CONFLICT (hospital_id, counter_name) DO NOTHING
            """
        )
        if dry_run:
            logger.info("WOULD backfill sequence_counters from existing patients")
            return
        conn.execute(backfill_sql)
        conn.commit()
        logger.info("BACKFILLED sequence_counters from existing patients")


def main(dry_run: bool = False) -> None:
    engine = get_transitional_sync_engine()
    inspector = inspect(engine)
    with engine.connect() as conn:
        _switch_patient_fk_to_restrict(conn, inspector, dry_run)
        _add_is_deleted_column(conn, inspector, dry_run)
        _sanitize_duplicate_active_admissions(conn, inspector, dry_run)
        _create_partial_indexes(conn, inspector, dry_run)
        _create_sequence_counters_and_backfill(conn, inspector, dry_run)
    logger.info("Done%s.", " (dry-run)" if dry_run else "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
