"""
Add a composite covering index on billing_charges for the financial clearance
hot-path query: (hospital_id, source_type, source_id, created_at DESC).

Background
----------
The financial clearance lookup query is:

    SELECT * FROM billing_charges
    WHERE hospital_id = :h
      AND source_type = :t
      AND source_id = :s
    ORDER BY created_at DESC
    LIMIT 1

The existing composite index ix_billing_charges_source covers
(hospital_id, source_type, source_id) but does NOT include created_at.
PostgreSQL can use that index to find matching rows, but must then sort them
in a separate step before applying LIMIT 1.

By adding created_at DESC as the fourth column in the index, PostgreSQL can
return the first matching row in index order (Index Scan Backward), eliminating
the Sort step entirely.

EXPLAIN (ANALYZE, BUFFERS) confirmed the Sort step with the existing index.
The existing ix_billing_charges_source index is left in place — it still serves
other query shapes (e.g. COUNT, existence checks) that don't ORDER BY created_at.

Transaction requirements
------------------------
CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
This script uses engine.raw_connection() with autocommit=True — the same
pattern used by all existing index migration scripts in this project.

Run with: python -m scripts.add_billing_clearance_covering_index
Safe to re-run: IF NOT EXISTS prevents duplicate creation.
"""

import logging

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.add_billing_clearance_covering_index")

# (index_name, table, columns_expression)
INDEXES = [
    (
        "ix_billing_charges_source_clearance",
        "billing_charges",
        "hospital_id, source_type, source_id, created_at DESC",
    ),
]


def main() -> None:
    engine = get_transitional_sync_engine()
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    raw_conn = engine.raw_connection()
    try:
        raw_conn.set_session(autocommit=True)  # type: ignore[attr-defined]
        cursor = raw_conn.cursor()
        for index_name, table, columns in INDEXES:
            logger.info(
                "Ensuring covering index %s on %s(%s)...", index_name, table, columns
            )
            cursor.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" ON "{table}" ({columns})'
            )
            logger.info("  Done: %s", index_name)
        cursor.close()
    finally:
        raw_conn.close()
    logger.info("All billing clearance covering indexes created successfully.")


if __name__ == "__main__":
    main()
