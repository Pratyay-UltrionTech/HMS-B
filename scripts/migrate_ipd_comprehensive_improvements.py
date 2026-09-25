"""
PostgreSQL schema migration for IPD Comprehensive Improvements.

Adds:
  1. intake_output_type enum ('intake', 'output')
  2. ipd_vitals table for admission-scoped structured vital observations
  3. ipd_intake_outputs table for admission-scoped fluid balance tracking
  4. Corresponding indexes on hospital_id, admission_id, patient_id, recorded_at

Idempotent, non-blocking, zero data loss.
Run with: python -m scripts.migrate_ipd_comprehensive_improvements [--dry-run]
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hms.scripts.migrate_ipd_comprehensive_improvements")


def _migrate_enums(conn: Connection, dry_run: bool) -> None:
    logger.info("--- Checking intake_output_type enum ---")
    res = conn.execute(text("SELECT typname FROM pg_type WHERE typname = 'intake_output_type'")).fetchone()
    if res:
        logger.info("SKIP intake_output_type enum: already exists")
    else:
        if dry_run:
            logger.info("WOULD CREATE TYPE intake_output_type AS ENUM ('intake', 'output')")
        else:
            conn.commit()
            conn.execute(text("CREATE TYPE intake_output_type AS ENUM ('intake', 'output')"))
            conn.commit()
            logger.info("CREATED TYPE intake_output_type AS ENUM ('intake', 'output')")


def _migrate_ipd_vitals(conn: Connection, inspector, dry_run: bool) -> None:
    logger.info("--- Checking ipd_vitals table ---")
    table_names = inspector.get_table_names()
    if "ipd_vitals" in table_names:
        logger.info("SKIP ipd_vitals: table already exists")
    else:
        if dry_run:
            logger.info("WOULD CREATE TABLE ipd_vitals")
        else:
            create_vitals_sql = """
            CREATE TABLE IF NOT EXISTS ipd_vitals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
                admission_id UUID NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
                patient_id UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                temperature_c DOUBLE PRECISION,
                pulse_rate_bpm INTEGER,
                respiratory_rate_bpm INTEGER,
                systolic_bp INTEGER,
                diastolic_bp INTEGER,
                spo2_percent DOUBLE PRECISION,
                pain_score INTEGER,
                consciousness VARCHAR(64),
                weight_kg DOUBLE PRECISION,
                notes TEXT,
                recorded_by_id UUID,
                recorded_by_name VARCHAR(255) NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS ix_ipd_vitals_hospital_id ON ipd_vitals (hospital_id);
            CREATE INDEX IF NOT EXISTS ix_ipd_vitals_admission_id ON ipd_vitals (admission_id);
            CREATE INDEX IF NOT EXISTS ix_ipd_vitals_patient_id ON ipd_vitals (patient_id);
            CREATE INDEX IF NOT EXISTS ix_ipd_vitals_recorded_at ON ipd_vitals (recorded_at);
            """
            conn.execute(text(create_vitals_sql))
            conn.commit()
            logger.info("CREATED TABLE ipd_vitals and associated indexes")


def _migrate_ipd_intake_outputs(conn: Connection, inspector, dry_run: bool) -> None:
    logger.info("--- Checking ipd_intake_outputs table ---")
    table_names = inspector.get_table_names()
    if "ipd_intake_outputs" in table_names:
        logger.info("SKIP ipd_intake_outputs: table already exists")
    else:
        if dry_run:
            logger.info("WOULD CREATE TABLE ipd_intake_outputs")
        else:
            create_io_sql = """
            CREATE TABLE IF NOT EXISTS ipd_intake_outputs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
                admission_id UUID NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
                patient_id UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                entry_type intake_output_type NOT NULL,
                category VARCHAR(64) NOT NULL,
                volume_ml DOUBLE PRECISION NOT NULL,
                unit VARCHAR(32) NOT NULL DEFAULT 'mL',
                route_or_site VARCHAR(128),
                notes TEXT,
                recorded_by_id UUID,
                recorded_by_name VARCHAR(255) NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS ix_ipd_intake_outputs_hospital_id ON ipd_intake_outputs (hospital_id);
            CREATE INDEX IF NOT EXISTS ix_ipd_intake_outputs_admission_id ON ipd_intake_outputs (admission_id);
            CREATE INDEX IF NOT EXISTS ix_ipd_intake_outputs_patient_id ON ipd_intake_outputs (patient_id);
            CREATE INDEX IF NOT EXISTS ix_ipd_intake_outputs_entry_type ON ipd_intake_outputs (entry_type);
            CREATE INDEX IF NOT EXISTS ix_ipd_intake_outputs_category ON ipd_intake_outputs (category);
            CREATE INDEX IF NOT EXISTS ix_ipd_intake_outputs_recorded_at ON ipd_intake_outputs (recorded_at);
            """
            conn.execute(text(create_io_sql))
            conn.commit()
            logger.info("CREATED TABLE ipd_intake_outputs and associated indexes")


def run_migration(dry_run: bool = False) -> None:
    engine = get_transitional_sync_engine()
    with engine.connect() as conn:
        inspector = inspect(conn)
        _migrate_enums(conn, dry_run=dry_run)
        _migrate_ipd_vitals(conn, inspector, dry_run=dry_run)
        _migrate_ipd_intake_outputs(conn, inspector, dry_run=dry_run)

    if dry_run:
        logger.info("\nDry run completed successfully. No changes were applied.")
    else:
        logger.info("\nMigration completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate IPD comprehensive improvements schema")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without applying them")
    args = parser.parse_args()
    run_migration(dry_run=args.dry_run)
