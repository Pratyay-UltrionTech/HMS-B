"""Actual PostgreSQL upgrade verification against an explicitly supplied local test DB."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url

from scripts.migrate_admin_masters_integrity import preflight, upgrade


@pytest.fixture
def migration_engine():
    url = os.getenv('HMS_MASTERS_MIGRATION_TEST_URL')
    if not url:
        pytest.skip('Set HMS_MASTERS_MIGRATION_TEST_URL to a disposable local PostgreSQL database')
    assert make_url(url).host in ('127.0.0.1', 'localhost'), 'Migration tests require a disposable local server'
    engine = create_engine(url)
    schema = 'masters_test_' + uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    @event.listens_for(engine, 'connect')
    def select_schema(dbapi_conn, _):
        with dbapi_conn.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
    engine.dispose()
    with engine.begin() as conn:
        for name in ('wings', 'departments'):
            conn.execute(text(f'CREATE TABLE {name} (id uuid PRIMARY KEY, hospital_id uuid NOT NULL)'))
        conn.execute(text('CREATE TABLE shift_types (id uuid PRIMARY KEY, hospital_id uuid NOT NULL, department_id uuid NOT NULL)'))
        conn.execute(text('CREATE TABLE consultation_pricing (id uuid PRIMARY KEY, hospital_id uuid NOT NULL, wing_id uuid NOT NULL, department_id uuid NOT NULL)'))
        conn.execute(text('CREATE TABLE insurance_providers (id uuid PRIMARY KEY, hospital_id uuid NOT NULL, code varchar(32) NOT NULL)'))
    yield engine
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


def test_postgres_forward_upgrade_and_idempotence(migration_engine):
    assert len(upgrade(migration_engine, dry_run=True)) == 4
    assert inspect(migration_engine).get_foreign_keys('shift_types') == []
    assert len(upgrade(migration_engine)) == 4
    assert upgrade(migration_engine) == []
    for table, expected in (('shift_types', 1), ('consultation_pricing', 2)):
        fks = inspect(migration_engine).get_foreign_keys(table)
        assert len(fks) == expected
        assert all(fk['options']['ondelete'] == 'RESTRICT' for fk in fks)
    with migration_engine.connect() as conn:
        assert conn.execute(text('SELECT bool_and(convalidated) FROM pg_constraint WHERE connamespace = current_schema()::regnamespace AND contype = \'f\'' )).scalar_one() is True


@pytest.mark.parametrize('kind', ['orphan', 'tenant', 'insurance'])
def test_migration_preflight_preserves_invalid_legacy_data(migration_engine, kind):
    hospital, other, parent, child = [uuid4() for _ in range(4)]
    with migration_engine.begin() as conn:
        if kind == 'insurance':
            conn.execute(text('INSERT INTO insurance_providers VALUES (:id, :hospital, :code)'), [{'id': child, 'hospital': hospital, 'code': 'ins-1'}, {'id': parent, 'hospital': hospital, 'code': ' INS-1 '}])
        else:
            if kind == 'tenant':
                conn.execute(text('INSERT INTO departments VALUES (:id, :hospital)'), {'id': parent, 'hospital': other})
            conn.execute(text('INSERT INTO shift_types VALUES (:id, :hospital, :department)'), {'id': child, 'hospital': hospital, 'department': parent})
    with pytest.raises(RuntimeError, match='Upgrade blocked'):
        upgrade(migration_engine)
    assert inspect(migration_engine).get_foreign_keys('shift_types') == []
    with migration_engine.connect() as conn:
        table = 'insurance_providers' if kind == 'insurance' else 'shift_types'
        assert conn.execute(text(f'SELECT count(*) FROM {table}')).scalar_one() == (2 if kind == 'insurance' else 1)
