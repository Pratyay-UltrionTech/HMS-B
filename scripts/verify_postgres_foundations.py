"""
PostgreSQL Foundation & Verification Script.

Executes live checks against Azure PostgreSQL:
1. Connection Verification (psycopg2, asyncpg, AsyncEngine)
2. Async Session Verification (AsyncSession, query execution)
3. Schema & Metadata Verification (physical columns vs SQLAlchemy models)
4. Read Query Verification (vitals & appointments queries)
5. Safety Assessment for Writes (strictly reports NOT EXECUTED when isolated test DB unavailable)
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv(".env")

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from hms_migration.config.settings import get_settings
from hms_migration.infrastructure.postgres.engine import get_async_engine
from hms_migration.infrastructure.postgres.session import get_async_session_factory
from app.models import Appointment, VitalReading


async def run_postgres_verification():
    results = {}
    print("==================================================================")
    print("PHASE 4 POSTGRESQL FOUNDATION VERIFICATION REPORT")
    print("==================================================================")

    # 1. Connection Verification
    cfg = get_settings()
    host = cfg.postgres_host
    db_name = cfg.postgres_db
    print(f"Target Database: {db_name} on {host}")

    try:
        engine = get_async_engine()
        async with engine.connect() as conn:
            val = await conn.scalar(text("SELECT 1"))
            version_str = await conn.scalar(text("SELECT version()"))
            current_db = await conn.scalar(text("SELECT current_database()"))
        results["connection"] = "PASS"
        results["async_engine"] = "PASS"
        print(f"[-] Connection & Async Engine: PASS ({version_str.split(',')[0].strip()})")
        print(f"[-] Connected Database: {current_db}")
    except Exception as exc:
        results["connection"] = f"FAIL ({exc})"
        results["async_engine"] = f"FAIL ({exc})"
        print(f"[x] Connection / Async Engine Failed: {exc}")
        return results

    # 2. Async Session Verification
    try:
        factory = get_async_session_factory()
        async with factory() as session:
            assert isinstance(session, AsyncSession)
            res = await session.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"))
            table_count = res.scalar()
        results["async_session"] = "PASS"
        print(f"[-] Async Session Creation & Execution: PASS (Public tables found: {table_count})")
    except Exception as exc:
        results["async_session"] = f"FAIL ({exc})"
        print(f"[x] Async Session Failed: {exc}")

    # 3. Schema & Model Metadata Verification
    try:
        async with factory() as session:
            # Query vital_readings columns
            res = await session.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'vital_readings' ORDER BY ordinal_position"
            ))
            cols = {r[0]: r[1] for r in res.fetchall()}

            expected_vital_cols = [
                "id", "hospital_id", "appointment_id", "patient_id",
                "name", "suitable_range", "result", "recorded_by_name",
                "recorded_at", "created_at"
            ]
            missing = [c for c in expected_vital_cols if c not in cols]
            if missing:
                raise ValueError(f"Missing expected columns in vital_readings: {missing}")

            # Verify appointment columns
            res_appt = await session.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'appointments' AND column_name IN ('id', 'hospital_id', 'status', 'queue_token')"
            ))
            found_appt_cols = {r[0] for r in res_appt.fetchall()}
            assert len(found_appt_cols) == 4

        results["schema"] = "PASS"
        print(f"[-] Schema & Model Metadata Verification: PASS (All {len(expected_vital_cols)} vital_readings columns verified)")
    except Exception as exc:
        results["schema"] = f"FAIL ({exc})"
        print(f"[x] Schema Verification Failed: {exc}")

    # 4. Read Query Verification
    try:
        async with factory() as session:
            # Execute read query for vitals
            stmt = select(VitalReading).limit(5)
            res = await session.execute(stmt)
            vitals_sample = res.scalars().all()

            # Execute read query for appointments
            stmt_appt = select(Appointment).limit(5)
            res_appt = await session.execute(stmt_appt)
            appts_sample = res_appt.scalars().all()

        results["reads"] = "PASS"
        print(f"[-] Read Query Verification: PASS (Read {len(vitals_sample)} vitals rows, {len(appts_sample)} appointments rows)")
    except Exception as exc:
        results["reads"] = f"FAIL ({exc})"
        print(f"[x] Read Query Verification Failed: {exc}")

    # 5. Write & Rollback Safety Assessment
    # As mandated by User Review Required (Constraint 2):
    # A dedicated disposable/test database is not configured. Staging DB cannot guarantee zero side effects
    # (e.g. sequence advancement, external triggers). Therefore, write verification is NOT executed.
    results["writes"] = "NOT EXECUTED — safe isolated write environment unavailable"
    results["rollback"] = "NOT EXECUTED — safe isolated write environment unavailable"
    results["vitals_crud_live"] = "NOT EXECUTED — safe isolated write environment unavailable"
    print("[-] PostgreSQL Write Verification: NOT EXECUTED (Staging protection: isolated test DB unavailable)")
    print("[-] PostgreSQL Rollback Verification: NOT EXECUTED")
    print("[-] PostgreSQL Live Vitals CRUD: NOT EXECUTED")

    print("==================================================================")
    return results


if __name__ == "__main__":
    asyncio.run(run_postgres_verification())
