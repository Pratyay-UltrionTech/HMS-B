"""
Add indexes on hot-path ORDER BY / range-filter columns that were missing
when the pagination work in this codebase added `.order_by(...).limit(...)`
to several list endpoints (patients, billing charges/payments/invoices/
receipts, lab orders, emergency encounters).

Without these, `ORDER BY <col> DESC LIMIT N` still has to sort every row
matching the hospital_id filter before taking the first N, even though
hospital_id itself is indexed. This matters once these tables have real
data volume, not at today's row counts.

Uses CREATE INDEX CONCURRENTLY so it does not lock the table against writes
while building (needed since these tables are live). Must run outside a
transaction block, hence the raw autocommit connection below rather than a
plain SQLAlchemy Session. Safe to re-run: every statement is IF NOT EXISTS.

Run with: python -m scripts.add_hot_path_indexes
"""

import logging

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.add_hot_path_indexes")

INDEXES = [
    ("ix_patients_created_at", "patients", "created_at"),
    ("ix_billing_charges_created_at", "billing_charges", "created_at"),
    ("ix_billing_payments_created_at", "billing_payments", "created_at"),
    ("ix_billing_invoices_created_at", "billing_invoices", "created_at"),
    ("ix_billing_receipts_created_at", "billing_receipts", "created_at"),
    ("ix_lab_orders_ordered_at", "lab_orders", "ordered_at"),
    ("ix_lab_orders_status", "lab_orders", "status"),
    ("ix_emergency_encounters_arrival_time", "emergency_encounters", "arrival_time"),
]


def main() -> None:
    engine = get_transitional_sync_engine()
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    raw_conn = engine.raw_connection()
    try:
        raw_conn.set_session(autocommit=True)  # type: ignore[attr-defined]
        cursor = raw_conn.cursor()
        for index_name, table, column in INDEXES:
            logger.info("Ensuring index %s on %s(%s)...", index_name, table, column)
            cursor.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" ON "{table}" ("{column}")'
            )
        cursor.close()
    finally:
        raw_conn.close()
    logger.info("Done.")


if __name__ == "__main__":
    main()
