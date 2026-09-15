"""
Add the foreign-key constraints introduced by the cross-module integration
audit (loose UUID columns that should have been real FKs — see the entity
changes in vitals, beds, pharmacy, emergency, patients, appointments,
doctors, billing, inpatient, clinical_decision, clinical_records, ambulance,
critical_care, cssd).

This script is derived directly from `infrastructure.postgres.base.Base`
metadata rather than a hand-written list, so it stays correct as entities
change. For every ForeignKeyConstraint declared on a mapped table it:

  1. Skips it if an equivalent constraint already exists in the live DB
     (safe to re-run).
  2. Checks for orphaned rows (a non-null FK column value with no matching
     parent row) and SKIPS adding that specific constraint if any are
     found, printing the offending row count instead of failing or
     silently deleting/altering data. Nothing destructive ever happens.
  3. Adds the constraint with `ALTER TABLE ... ADD CONSTRAINT ... NOT VALID`
     then `VALIDATE CONSTRAINT` as a separate step, so an already-heavily
     written table is never held under a long exclusive lock while every
     existing row is checked in one go (NOT VALID takes a brief lock to
     register the constraint; VALIDATE only needs a lock at the very end).

Run with: python -m scripts.add_missing_foreign_keys
Add --dry-run to only report what would change without altering anything.
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import inspect, text

import app.router  # noqa: F401  (imports every module so Base.metadata is fully populated)
from infrastructure.postgres.base import Base
from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hms.scripts.add_missing_foreign_keys")


def _constraint_name(table_name: str, columns: list[str]) -> str:
    return f"fk_{table_name}_{'_'.join(columns)}"[:63]  # Postgres identifier limit


def main(dry_run: bool = False) -> None:
    engine = get_transitional_sync_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    added, skipped_existing, skipped_orphans, skipped_no_table = 0, 0, 0, 0

    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                # Table doesn't exist yet in the live DB (e.g. brand new
                # module never deployed here) — nothing to alter.
                skipped_no_table += len(table.foreign_key_constraints)
                continue

            existing_fks = inspector.get_foreign_keys(table.name)
            existing_fk_columns = {
                tuple(sorted(fk["constrained_columns"])) for fk in existing_fks
            }

            for fk_constraint in table.foreign_key_constraints:
                local_cols = [fk.parent.name for fk in fk_constraint.elements]
                if tuple(sorted(local_cols)) in existing_fk_columns:
                    skipped_existing += 1
                    continue

                referred_table = fk_constraint.elements[0].column.table.name
                referred_cols = [fk.column.name for fk in fk_constraint.elements]
                ondelete = fk_constraint.ondelete or "NO ACTION"
                name = _constraint_name(table.name, local_cols)

                if referred_table not in existing_tables:
                    logger.warning(
                        "SKIP %s.%s -> %s.%s : referenced table does not exist in live DB",
                        table.name, local_cols, referred_table, referred_cols,
                    )
                    skipped_no_table += 1
                    continue

                col_list = ", ".join(local_cols)
                orphan_sql = text(
                    f'SELECT COUNT(*) FROM "{table.name}" t '
                    f'WHERE {" AND ".join(f"t.{c} IS NOT NULL" for c in local_cols)} '
                    f'AND NOT EXISTS ('
                    f'  SELECT 1 FROM "{referred_table}" r '
                    f'  WHERE {" AND ".join(f"r.{rc} = t.{lc}" for rc, lc in zip(referred_cols, local_cols))}'
                    f')'
                )
                orphan_count = conn.execute(orphan_sql).scalar_one()
                conn.commit()
                if orphan_count:
                    logger.warning(
                        "SKIP %s(%s) -> %s(%s): %d orphaned row(s) reference a missing parent; "
                        "fix the data before adding this constraint",
                        table.name, col_list, referred_table, ", ".join(referred_cols), orphan_count,
                    )
                    skipped_orphans += 1
                    continue

                add_sql = (
                    f'ALTER TABLE "{table.name}" ADD CONSTRAINT "{name}" '
                    f'FOREIGN KEY ({col_list}) REFERENCES "{referred_table}" ({", ".join(referred_cols)}) '
                    f'ON DELETE {ondelete} NOT VALID'
                )
                validate_sql = f'ALTER TABLE "{table.name}" VALIDATE CONSTRAINT "{name}"'

                if dry_run:
                    logger.info("WOULD ADD %s: %s", name, add_sql)
                    added += 1
                    continue

                try:
                    conn.execute(text(add_sql))
                    conn.execute(text(validate_sql))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    logger.exception("FAILED adding %s on %s(%s) -> %s(%s); skipped, no changes made for this constraint",
                                      name, table.name, col_list, referred_table, ", ".join(referred_cols))
                    continue
                logger.info("ADDED %s on %s(%s) -> %s(%s)", name, table.name, col_list, referred_table, ", ".join(referred_cols))
                added += 1

    logger.info(
        "Done. added=%d already_present=%d skipped_orphans=%d skipped_missing_table=%d",
        added, skipped_existing, skipped_orphans, skipped_no_table,
    )
    if skipped_orphans:
        logger.warning(
            "%d constraint(s) were skipped due to orphaned data — re-run this script "
            "after cleaning up those rows.", skipped_orphans,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
