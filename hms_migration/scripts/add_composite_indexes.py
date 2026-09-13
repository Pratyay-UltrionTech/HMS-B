"""
Add composite (hospital_id, sort_column) indexes for the hottest list
queries — patients, appointments, billing charges, lab orders — which
currently filter by hospital_id and sort by a separate column using two
single-column indexes (Postgres bitmap-ANDs them, then still sorts).

A composite index lets Postgres satisfy "WHERE hospital_id = X ORDER BY col
DESC LIMIT N" with a single ordered index scan instead of a filter + sort
step. Matters once tables have real row counts, not at today's scale.

Uses CREATE INDEX CONCURRENTLY (non-locking) and IF NOT EXISTS (safe to
re-run). The single-column indexes from add_hot_path_indexes.py are left in
place — they still help other query shapes (e.g. date-range filters without
a hospital_id predicate, or queries that only need hospital_id).

Run with: python -m hms_migration.scripts.add_composite_indexes
"""

import logging

from hms_migration.infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.add_composite_indexes")

# (index_name, table, "col1, col2 DESC")
INDEXES = [
    ("ix_patients_hospital_created", "patients", "hospital_id, created_at DESC"),
    ("ix_appointments_hospital_date", "appointments", "hospital_id, appointment_date DESC"),
    ("ix_billing_charges_hospital_created", "billing_charges", "hospital_id, created_at DESC"),
    ("ix_billing_payments_hospital_created", "billing_payments", "hospital_id, created_at DESC"),
    ("ix_billing_invoices_hospital_created", "billing_invoices", "hospital_id, created_at DESC"),
    ("ix_billing_receipts_hospital_created", "billing_receipts", "hospital_id, created_at DESC"),
    ("ix_lab_orders_hospital_ordered", "lab_orders", "hospital_id, ordered_at DESC"),
]


def main() -> None:
    engine = get_transitional_sync_engine()
    raw_conn = engine.raw_connection()
    try:
        raw_conn.set_session(autocommit=True)  # type: ignore[attr-defined]
        cursor = raw_conn.cursor()
        for index_name, table, columns in INDEXES:
            logger.info("Ensuring composite index %s on %s(%s)...", index_name, table, columns)
            cursor.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" ON "{table}" ({columns})'
            )
        cursor.close()
    finally:
        raw_conn.close()
    logger.info("Done.")


if __name__ == "__main__":
    main()
