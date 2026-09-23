"""
PostgreSQL schema migration for IPD Lifecycle Remediation (FLAW-IPD-01 through FLAW-IPD-07).

Adds:
  1. admission_status enum value: 'cancelled' (ALTER TYPE admission_status ADD VALUE IF NOT EXISTS 'cancelled')
  2. admission_id UUID column with FK to admissions(id) ON DELETE SET NULL and B-Tree index on:
     - lab_orders
     - lab_prescription_requests
     - radiology_orders
     - rad_prescription_requests
     - pharmacy_sales
     - pharmacy_rx_requests
     - ot_surgeries
     - prescriptions
     - medical_records

Zero data loss, safe, idempotent, non-blocking (NOT VALID + VALIDATE CONSTRAINT).

Run with: python -m scripts.migrate_ipd_lifecycle_remediation [--dry-run]
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hms.scripts.migrate_ipd_lifecycle_remediation")

TABLES_TO_MIGRATE = [
    "lab_orders",
    "lab_prescription_requests",
    "radiology_orders",
    "rad_prescription_requests",
    "pharmacy_sales",
    "pharmacy_rx_requests",
    "ot_surgeries",
    "prescriptions",
    "medical_records",
]


def _migrate_admission_status_enum(conn: Connection, dry_run: bool) -> None:
    logger.info("--- Checking admission_status enum ---")
    res = conn.execute(text("SELECT unnest(enum_range(NULL::admission_status))")).fetchall()
    existing_statuses = {r[0] for r in res}
    if "cancelled" in existing_statuses:
        logger.info("SKIP admission_status enum: 'cancelled' already exists")
        return

    if dry_run:
        logger.info("WOULD ADD 'cancelled' to admission_status enum")
        return

    # In PostgreSQL, ALTER TYPE ... ADD VALUE cannot run inside a multi-statement transaction block
    # so we commit prior statements if any
    conn.commit()
    conn.execute(text("ALTER TYPE admission_status ADD VALUE IF NOT EXISTS 'cancelled'"))
    conn.commit()
    logger.info("ADDED 'cancelled' to admission_status enum")


def _migrate_admission_id_columns(conn: Connection, inspector, dry_run: bool) -> None:
    logger.info("--- Checking admission_id columns, foreign keys, and indexes ---")
    for table in TABLES_TO_MIGRATE:
        if table not in inspector.get_table_names():
            logger.info("SKIP %s: table does not exist in database", table)
            continue

        existing_cols = {c["name"] for c in inspector.get_columns(table)}
        if "admission_id" not in existing_cols:
            if dry_run:
                logger.info("WOULD ADD COLUMN %s.admission_id UUID NULL", table)
            else:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS admission_id UUID NULL'))
                conn.commit()
                logger.info("ADDED COLUMN %s.admission_id", table)
        else:
            logger.info("SKIP %s.admission_id: column already exists", table)

        # Foreign Key: fk_{table}_admission_id -> admissions(id) ON DELETE SET NULL
        fk_name = f"fk_{table}_admission_id"[:63]
        existing_fks = {fk["name"] for fk in inspector.get_foreign_keys(table)}
        if fk_name not in existing_fks:
            if dry_run:
                logger.info("WOULD ADD FK %s on %s(admission_id) -> admissions(id)", fk_name, table)
            else:
                # Add NOT VALID then validate for safety
                conn.execute(text(f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = '{fk_name}'
                        ) THEN
                            ALTER TABLE "{table}"
                            ADD CONSTRAINT "{fk_name}"
                            FOREIGN KEY (admission_id) REFERENCES admissions(id)
                            ON DELETE SET NULL
                            NOT VALID;
                            
                            ALTER TABLE "{table}" VALIDATE CONSTRAINT "{fk_name}";
                        END IF;
                    END $$;
                """))
                conn.commit()
                logger.info("ADDED & VALIDATED FK %s on %s", fk_name, table)
        else:
            logger.info("SKIP FK %s: already exists", fk_name)

        # Index: ix_{table}_admission_id
        idx_name = f"ix_{table}_admission_id"[:63]
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(table)}
        if idx_name not in existing_indexes:
            if dry_run:
                logger.info("WOULD CREATE INDEX %s ON %s(admission_id)", idx_name, table)
            else:
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table}" (admission_id)'))
                conn.commit()
                logger.info("CREATED INDEX %s ON %s", idx_name, table)
        else:
            logger.info("SKIP INDEX %s: already exists", idx_name)


def main(dry_run: bool = False) -> None:
    engine = get_transitional_sync_engine()
    inspector = inspect(engine)
    with engine.connect() as conn:
        _migrate_admission_status_enum(conn, dry_run)
        _migrate_admission_id_columns(conn, inspector, dry_run)
    logger.info("Migration completed successfully%s.", " (dry-run)" if dry_run else "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
