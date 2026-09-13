"""
Add pg_trgm GIN indexes to support the leading-wildcard `ILIKE '%term%'`
searches used by patients/appointments/billing list endpoints (e.g.
patients_repository.list_patients, appointments_repository.list_appointments,
billing_repository.list_charges/list_invoices).

A leading `%` in ILIKE prevents any plain B-tree index from being used, so
Postgres falls back to a sequential scan of every hospital-matching row on
every search keystroke. A GIN trigram index lets Postgres use an index scan
for `column ILIKE '%term%'` instead.

Uses CREATE INDEX CONCURRENTLY (non-locking) and IF NOT EXISTS (safe to
re-run). Requires the pg_trgm extension, created here if missing.

Run with: python -m hms_migration.scripts.add_trigram_search_indexes
"""

import logging

from hms_migration.infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.add_trigram_search_indexes")

TRIGRAM_INDEXES = [
    ("ix_patients_name_trgm", "patients", "name"),
    ("ix_patients_uhid_trgm", "patients", "uhid"),
    ("ix_patients_mobile_trgm", "patients", "mobile"),
    ("ix_patients_first_name_trgm", "patients", "first_name"),
    ("ix_patients_last_name_trgm", "patients", "last_name"),
    ("ix_billing_charges_description_trgm", "billing_charges", "description"),
    ("ix_billing_invoices_invoice_number_trgm", "billing_invoices", "invoice_number"),
]


def main() -> None:
    engine = get_transitional_sync_engine()
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    raw_conn = engine.raw_connection()
    try:
        raw_conn.set_session(autocommit=True)  # type: ignore[attr-defined]
        cursor = raw_conn.cursor()
        logger.info("Ensuring pg_trgm extension...")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for index_name, table, column in TRIGRAM_INDEXES:
            logger.info("Ensuring trigram index %s on %s(%s)...", index_name, table, column)
            cursor.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" '
                f'ON "{table}" USING GIN ("{column}" gin_trgm_ops)'
            )
        cursor.close()
    finally:
        raw_conn.close()
    logger.info("Done.")


if __name__ == "__main__":
    main()
