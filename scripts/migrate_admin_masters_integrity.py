"""Forward, non-destructive Admin/Masters integrity upgrade.

Run ``python -m scripts.migrate_admin_masters_integrity [--dry-run]``.
Preflight rejects orphans, cross-hospital references and normalized code collisions;
it never deletes or rewrites historical data. Existing SQL migrations remain intact.
"""
from __future__ import annotations

import argparse
import logging

from sqlalchemy import inspect, text

from infrastructure.postgres.engine import get_transitional_sync_engine

logger = logging.getLogger(__name__)

REFERENCES = (
    ('shift_types', 'department_id', 'departments'),
    ('consultation_pricing', 'wing_id', 'wings'),
    ('consultation_pricing', 'department_id', 'departments'),
)


def preflight(conn) -> None:
    for table, column, parent in REFERENCES:
        count = conn.execute(text(
            f'SELECT count(*) FROM "{table}" child LEFT JOIN "{parent}" parent '
            f'ON child."{column}" = parent.id WHERE child."{column}" IS NOT NULL '
            'AND (parent.id IS NULL OR child.hospital_id != parent.hospital_id)'
        )).scalar_one()
        if count:
            raise RuntimeError(f'Upgrade blocked: {table}.{column} has {count} orphan or cross-hospital references. Resolve these records explicitly; no data was modified.')
    conflicts = conn.execute(text(
        'SELECT count(*) FROM (SELECT hospital_id, upper(trim(code)) FROM insurance_providers '
        'GROUP BY hospital_id, upper(trim(code)) HAVING count(*) > 1) collisions'
    )).scalar_one()
    if conflicts:
        raise RuntimeError(f'Upgrade blocked: {conflicts} normalized insurance code collisions. Resolve duplicate codes explicitly; no data was modified.')


def upgrade(engine, *, dry_run: bool = False) -> list[str]:
    if engine.dialect.name != 'postgresql':
        raise RuntimeError('This forward ALTER migration requires PostgreSQL.')
    changes = []
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        preflight(conn)
        inspector = inspect(conn)
        for table, column, parent in REFERENCES:
            existing = inspector.get_foreign_keys(table)
            if any(fk['constrained_columns'] == [column] and fk['referred_table'] == parent for fk in existing):
                continue
            name = f'fk_{table}_{column}_masters'
            changes.append(name)
            if not dry_run:
                conn.execute(text(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" FOREIGN KEY ("{column}") REFERENCES "{parent}"(id) ON DELETE RESTRICT NOT VALID'))
                conn.execute(text(f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"'))
        indexes = {idx['name'] for idx in inspector.get_indexes('insurance_providers')}
        name = 'uq_insurance_hospital_normalized_code'
        if name not in indexes:
            changes.append(name)
            if not dry_run:
                conn.execute(text(f'CREATE UNIQUE INDEX "{name}" ON insurance_providers (hospital_id, upper(trim(code)))'))
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    changes = upgrade(get_transitional_sync_engine(), dry_run=args.dry_run)
    logger.info('%s: %s', 'Planned changes' if args.dry_run else 'Applied changes', changes or 'Already current')


if __name__ == '__main__':
    main()
