"""
Migration for Group 6 — Operational State Synchronization (TASK-005).

Adds the schema columns required by:
- FLAW-007: `hospitals.timezone` (tenant timezone for auto-cancel partitioning).
- FLAW-010: `admissions.patient_name`, `admissions.gender`, `admissions.age_at_admission`
  (immutable demographic snapshot captured at admission time).
- FLAW-019: `inventory_central_stock.in_transit_quantity` and
  `inventory_departmental_stock.in_transit_quantity` (in-transit transfer holding).

This repo has no Alembic; schema changes are applied through the entity metadata
(`Base.metadata`) and the `sync_missing_columns` helper in `scripts/create_schema.py`
which runs `ADD COLUMN IF NOT EXISTS` for any entity column missing from the live
table. This script is a documented, idempotent migration runner that:

1. Ensures the entity-level metadata is registered (imports app.main).
2. Adds any missing columns (via `sync_missing_columns`-style ALTER statements) so
   the change is explicit and auditable rather than silently deferred to boot-time
   create_all.

Run with:  python -m scripts.migrate_group6_operational_state
"""

import logging

from infrastructure.postgres.base import Base
from infrastructure.postgres.engine import get_transitional_sync_engine
from scripts.create_schema import sync_missing_columns

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hms.scripts.migrate_group6")

# Columns this migration is responsible for (documentation / audit trail only —
# the actual ALTERs are driven by the entity metadata below).
EXPECTED_COLUMNS = {
    "hospitals": ["timezone"],
    "admissions": ["patient_name", "gender", "age_at_admission"],
    "inventory_central_stock": ["in_transit_quantity"],
    "inventory_departmental_stock": ["in_transit_quantity"],
}


def main() -> None:
    import app.main  # noqa: F401  Ensure all model tables are registered.

    engine = get_transitional_sync_engine()

    # 1) Add any missing entity columns to existing tables (idempotent).
    sync_missing_columns(engine)

    # 2) Verify the target columns now exist and report.
    from sqlalchemy import text

    with engine.connect() as conn:
        for table_name, cols in EXPECTED_COLUMNS.items():
            for col in cols:
                row = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
                    ),
                    {"t": table_name, "c": col},
                ).fetchone()
                status = "OK" if row else "MISSING"
                logger.info("column %s.%s -> %s", table_name, col, status)

    logger.info("Group 6 operational-state migration complete.")


if __name__ == "__main__":
    main()
