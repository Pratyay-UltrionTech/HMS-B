"""
Add security / forensic columns for FLAW-018 and FLAW-024.

Group 2 (Security, RBAC & Forensic Infrastructure) introduces:

  FLAW-018 (DMS soft delete + archival retention)
    patient_documents.is_deleted  BOOLEAN  NOT NULL DEFAULT FALSE
    patient_documents.deleted_by  VARCHAR(255) NULL
    patient_documents.deleted_at  TIMESTAMPTZ NULL

  FLAW-024 (server-driven session revocation)
    hospital_users.token_version  INTEGER  NOT NULL DEFAULT 1

The ORM entities already declare these columns; this script guarantees the
columns exist on the live database (idempotent via ADD COLUMN IF NOT EXISTS)
and backfills sensible defaults for pre-existing rows.

Run with: python -m scripts.add_security_columns
"""

import logging

from sqlalchemy import text

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.add_security_columns")

STATEMENTS = [
    # FLAW-018: soft-delete columns on patient_documents
    (
        "ALTER TABLE patient_documents "
        "ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
    ),
    (
        "ALTER TABLE patient_documents "
        "ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255) NULL"
    ),
    (
        "ALTER TABLE patient_documents "
        "ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL"
    ),
    # FLAW-024: session revocation version on hospital_users
    (
        "ALTER TABLE hospital_users "
        "ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1"
    ),
    # FLAW-007: tenant timezone column on hospitals
    (
        "ALTER TABLE hospitals "
        "ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata'"
    ),
    # FLAW-010: demographic snapshot columns on admissions
    (
        "ALTER TABLE admissions "
        "ADD COLUMN IF NOT EXISTS patient_name VARCHAR(255) NULL"
    ),
    (
        "ALTER TABLE admissions "
        "ADD COLUMN IF NOT EXISTS gender VARCHAR(32) NULL"
    ),
    (
        "ALTER TABLE admissions "
        "ADD COLUMN IF NOT EXISTS age_at_admission INTEGER NULL"
    ),
    # FLAW-020: in_transit_quantity columns on inventory stock tables
    (
        "ALTER TABLE inventory_central_stock "
        "ADD COLUMN IF NOT EXISTS in_transit_quantity DOUBLE PRECISION NOT NULL DEFAULT 0.0"
    ),
    (
        "ALTER TABLE inventory_departmental_stock "
        "ADD COLUMN IF NOT EXISTS in_transit_quantity DOUBLE PRECISION NOT NULL DEFAULT 0.0"
    ),
]


def main() -> None:
    engine = get_transitional_sync_engine()
    with engine.begin() as conn:
        for stmt in STATEMENTS:
            logger.info("Executing: %s", stmt)
            conn.execute(text(stmt))
        # Backfill: any rows that predate the column should already be non-deleted
        # and version 1; ensure that invariant holds for safety.
        conn.execute(
            text(
                "UPDATE patient_documents SET is_deleted = FALSE "
                "WHERE is_deleted IS NULL"
            )
        )
        conn.execute(
            text(
                "UPDATE hospital_users SET token_version = 1 "
                "WHERE token_version IS NULL OR token_version < 1"
            )
        )
    logger.info("Done. Security columns ensured on patient_documents and hospital_users.")


if __name__ == "__main__":
    main()
