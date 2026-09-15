"""
Add the composite (hospital_id, scheduled_at) index on ot_surgeries.

ot_surgeries currently has single-column indexes on hospital_id and every FK
(patient_id, surgeon_id, department_id, ot_room_id) but none on
scheduled_at or status, even though every OT list/calendar/dashboard query
filters by hospital_id and either sorts or range-filters on scheduled_at:

  - list_surgeries(): ORDER BY scheduled_at DESC
  - list_calendar_surgeries(): WHERE scheduled_at <= end, ORDER BY scheduled_at ASC
  - get_dashboard_metrics(): WHERE scheduled_at BETWEEN <today start> AND <today end>

A composite index lets Postgres satisfy "WHERE hospital_id = X [AND
scheduled_at ...] ORDER BY scheduled_at" with a single ordered index scan
instead of a bitmap-AND of two single-column indexes followed by a sort.

A separate (hospital_id, status) index is deliberately NOT added here —
status is low-cardinality (5 values) and every status-filtered query already
carries the hospital_id predicate too, so the marginal benefit over this
index is expected to be small. Add it separately later only if measurement
(EXPLAIN ANALYZE on the dashboard's per-status COUNT queries) shows it's
still needed after this index is in place.

Uses CREATE INDEX CONCURRENTLY so it does not lock the table against writes
while building (ot_surgeries is a live table). Must run outside a
transaction block, hence the raw autocommit connection below rather than a
plain SQLAlchemy Session. Safe to re-run: IF NOT EXISTS.

Run with: python -m scripts.add_ot_schedule_index
"""

import logging

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.add_ot_schedule_index")

INDEX_NAME = "ix_ot_surgeries_hospital_scheduled"
TABLE = "ot_surgeries"
COLUMNS = "hospital_id, scheduled_at"


def main() -> None:
    engine = get_transitional_sync_engine()
    raw_conn = engine.raw_connection()
    try:
        raw_conn.set_session(autocommit=True)  # type: ignore[attr-defined]
        cursor = raw_conn.cursor()
        logger.info("Ensuring composite index %s on %s(%s)...", INDEX_NAME, TABLE, COLUMNS)
        cursor.execute(
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{INDEX_NAME}" ON "{TABLE}" ({COLUMNS})'
        )
        cursor.close()
    finally:
        raw_conn.close()
    logger.info("Done.")


if __name__ == "__main__":
    main()
