"""
PostgreSQL schema migration and backfill for HMS Clinical + IPD + Billing Workflow Improvements.

Adds:
  1. Enums:
     - admission_care_team_role ('primary_consultant', 'admitting_doctor', 'consulting_doctor', 'resident')
     - medication_order_status ('active', 'modified', 'discontinued', 'completed', 'cancelled')
     - discharge_exception_status ('pending', 'approved', 'rejected')
  2. Tables:
     - admission_care_team_members
     - ipd_medication_orders
     - discharge_financial_exceptions
  3. Columns:
     - admissions.department_id, admissions.department_name
     - billing_deposits.admission_id
     - medication_administration_records.order_id
  4. Backfill:
     - Backfills care team primary_consultant from admissions.doctor_id
     - Ensures open admissions have an IPD FinancialAccount

Idempotent, non-blocking, zero data loss.
Run with: python -m scripts.migrate_ipd_workflow_improvements [--dry-run]
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hms.scripts.migrate_ipd_workflow_improvements")


def _migrate_enums(conn: Connection, dry_run: bool) -> None:
    enums = [
        ("admission_care_team_role", "('primary_consultant', 'admitting_doctor', 'consulting_doctor', 'resident')"),
        ("medication_order_status", "('active', 'modified', 'discontinued', 'completed', 'cancelled')"),
        ("discharge_exception_status", "('pending', 'approved', 'rejected')"),
    ]
    for name, values in enums:
        logger.info(f"--- Checking {name} enum ---")
        res = conn.execute(text(f"SELECT typname FROM pg_type WHERE typname = '{name}'")).fetchone()
        if res:
            logger.info(f"SKIP {name} enum: already exists")
        else:
            if dry_run:
                logger.info(f"WOULD CREATE TYPE {name} AS ENUM {values}")
            else:
                conn.commit()
                conn.execute(text(f"CREATE TYPE {name} AS ENUM {values}"))
                conn.commit()
                logger.info(f"CREATED TYPE {name} AS ENUM {values}")


def _migrate_tables(conn: Connection, inspector, dry_run: bool) -> None:
    table_names = inspector.get_table_names()

    # 1. admission_care_team_members
    logger.info("--- Checking admission_care_team_members table ---")
    if "admission_care_team_members" in table_names:
        logger.info("SKIP admission_care_team_members: table already exists")
    else:
        if dry_run:
            logger.info("WOULD CREATE TABLE admission_care_team_members")
        else:
            sql = """
            CREATE TABLE IF NOT EXISTS admission_care_team_members (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
                admission_id UUID NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
                doctor_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE CASCADE,
                role admission_care_team_role NOT NULL DEFAULT 'consulting_doctor',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                assigned_by_id UUID REFERENCES hospital_users(id) ON DELETE SET NULL,
                assigned_by_name VARCHAR(255),
                ended_at TIMESTAMPTZ,
                notes TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_care_team_hospital_id ON admission_care_team_members (hospital_id);
            CREATE INDEX IF NOT EXISTS ix_care_team_admission_active ON admission_care_team_members (hospital_id, admission_id, is_active);
            CREATE INDEX IF NOT EXISTS ix_care_team_doctor ON admission_care_team_members (hospital_id, doctor_id);
            """
            conn.execute(text(sql))
            conn.commit()
            logger.info("CREATED TABLE admission_care_team_members and indexes")

    # 2. ipd_medication_orders
    logger.info("--- Checking ipd_medication_orders table ---")
    if "ipd_medication_orders" in table_names:
        logger.info("SKIP ipd_medication_orders: table already exists")
    else:
        if dry_run:
            logger.info("WOULD CREATE TABLE ipd_medication_orders")
        else:
            sql = """
            CREATE TABLE IF NOT EXISTS ipd_medication_orders (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
                admission_id UUID NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
                patient_id UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                doctor_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE RESTRICT,
                doctor_name VARCHAR(255) NOT NULL,
                medicine_id UUID,
                medicine_name VARCHAR(255) NOT NULL,
                dose VARCHAR(64) NOT NULL,
                dosage_unit VARCHAR(32) NOT NULL DEFAULT 'mg',
                route VARCHAR(64) NOT NULL DEFAULT 'oral',
                frequency VARCHAR(64) NOT NULL DEFAULT 'od',
                schedule_timing VARCHAR(128),
                start_time TIMESTAMPTZ NOT NULL,
                end_time TIMESTAMPTZ,
                duration_days INTEGER,
                instructions TEXT,
                is_prn BOOLEAN NOT NULL DEFAULT FALSE,
                prn_indication VARCHAR(255),
                status medication_order_status NOT NULL DEFAULT 'active',
                discontinued_at TIMESTAMPTZ,
                discontinued_by_id UUID,
                discontinued_by_name VARCHAR(255),
                discontinued_reason TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS ix_med_order_hospital_id ON ipd_medication_orders (hospital_id);
            CREATE INDEX IF NOT EXISTS ix_med_order_admission_status ON ipd_medication_orders (hospital_id, admission_id, status);
            CREATE INDEX IF NOT EXISTS ix_med_order_patient ON ipd_medication_orders (hospital_id, patient_id);
            """
            conn.execute(text(sql))
            conn.commit()
            logger.info("CREATED TABLE ipd_medication_orders and indexes")

    # 3. discharge_financial_exceptions
    logger.info("--- Checking discharge_financial_exceptions table ---")
    if "discharge_financial_exceptions" in table_names:
        logger.info("SKIP discharge_financial_exceptions: table already exists")
    else:
        if dry_run:
            logger.info("WOULD CREATE TABLE discharge_financial_exceptions")
        else:
            sql = """
            CREATE TABLE IF NOT EXISTS discharge_financial_exceptions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
                admission_id UUID NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
                financial_account_id UUID NOT NULL REFERENCES financial_accounts(id) ON DELETE CASCADE,
                patient_id UUID NOT NULL REFERENCES patients(id) ON DELETE RESTRICT,
                outstanding_amount_at_request DOUBLE PRECISION NOT NULL,
                reason TEXT NOT NULL,
                recommendation TEXT,
                requested_by_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE RESTRICT,
                requested_by_name VARCHAR(255) NOT NULL,
                requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                status discharge_exception_status NOT NULL DEFAULT 'pending',
                approved_by_id UUID REFERENCES hospital_users(id) ON DELETE RESTRICT,
                approved_by_name VARCHAR(255),
                approved_at TIMESTAMPTZ,
                approval_remarks TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS ix_discharge_exc_hospital_id ON discharge_financial_exceptions (hospital_id);
            CREATE INDEX IF NOT EXISTS ix_discharge_exc_admission ON discharge_financial_exceptions (hospital_id, admission_id);
            CREATE INDEX IF NOT EXISTS ix_discharge_exc_status ON discharge_financial_exceptions (hospital_id, status);
            """
            conn.execute(text(sql))
            conn.commit()
            logger.info("CREATED TABLE discharge_financial_exceptions and indexes")


def _migrate_columns(conn: Connection, inspector, dry_run: bool) -> None:
    logger.info("--- Checking table columns ---")

    # admissions.department_id, admissions.department_name
    admission_cols = [c["name"] for c in inspector.get_columns("admissions")]
    if "department_id" not in admission_cols:
        if dry_run:
            logger.info("WOULD ADD COLUMN admissions.department_id")
        else:
            conn.execute(text("ALTER TABLE admissions ADD COLUMN IF NOT EXISTS department_id UUID REFERENCES departments(id) ON DELETE SET NULL"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_admissions_department_id ON admissions (department_id)"))
            conn.commit()
            logger.info("ADDED COLUMN admissions.department_id")

    if "department_name" not in admission_cols:
        if dry_run:
            logger.info("WOULD ADD COLUMN admissions.department_name")
        else:
            conn.execute(text("ALTER TABLE admissions ADD COLUMN IF NOT EXISTS department_name VARCHAR(128)"))
            conn.commit()
            logger.info("ADDED COLUMN admissions.department_name")

    # billing_deposits.admission_id
    deposit_cols = [c["name"] for c in inspector.get_columns("billing_deposits")]
    if "admission_id" not in deposit_cols:
        if dry_run:
            logger.info("WOULD ADD COLUMN billing_deposits.admission_id")
        else:
            conn.execute(text("ALTER TABLE billing_deposits ADD COLUMN IF NOT EXISTS admission_id UUID REFERENCES admissions(id) ON DELETE SET NULL"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_billing_deposits_admission_id ON billing_deposits (admission_id)"))
            conn.commit()
            logger.info("ADDED COLUMN billing_deposits.admission_id")

    # medication_administration_records.order_id
    emar_cols = [c["name"] for c in inspector.get_columns("medication_administration_records")]
    if "order_id" not in emar_cols:
        if dry_run:
            logger.info("WOULD ADD COLUMN medication_administration_records.order_id")
        else:
            conn.execute(text("ALTER TABLE medication_administration_records ADD COLUMN IF NOT EXISTS order_id UUID REFERENCES ipd_medication_orders(id) ON DELETE SET NULL"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_emar_order_id ON medication_administration_records (order_id)"))
            conn.commit()
            logger.info("ADDED COLUMN medication_administration_records.order_id")


def _backfill_data(conn: Connection, dry_run: bool) -> None:
    logger.info("--- Checking backfill requirements ---")
    # 1. Backfill care team members for admissions that have doctor_id but no active primary_consultant
    backfill_care_team_sql = """
    INSERT INTO admission_care_team_members (
        id, hospital_id, admission_id, doctor_id, role, is_active, assigned_at, assigned_by_name, notes
    )
    SELECT
        gen_random_uuid(),
        a.hospital_id,
        a.id,
        a.doctor_id,
        'primary_consultant'::admission_care_team_role,
        TRUE,
        COALESCE(a.admitted_at, now()),
        'Legacy Migration Backfill',
        'Auto-backfilled from admission.doctor_id'
    FROM admissions a
    WHERE a.doctor_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM admission_care_team_members ctm
          WHERE ctm.admission_id = a.id
            AND ctm.role = 'primary_consultant'
            AND ctm.is_active = TRUE
      );
    """
    if dry_run:
        logger.info("WOULD BACKFILL care team primary_consultants from admissions.doctor_id")
    else:
        res = conn.execute(text(backfill_care_team_sql))
        conn.commit()
        logger.info(f"BACKFILLED {res.rowcount} care team member records from legacy doctor_id")

    # Ensure every existing admission has an IPD account. Keep unrelated account
    # types and all financial history intact; only admissions without a matching
    # IPD account receive a new empty account.
    logger.info("--- Checking IPD financial account backfill ---")
    account_backfill_sql = text("""
        WITH missing AS (
            SELECT a.id, a.hospital_id, a.patient_id,
                   COALESCE(MAX(substring(fa.account_number
                       FROM '^ACC-[0-9]{4}-([0-9]+)$')::bigint), 0) AS max_seq
            FROM admissions a
            LEFT JOIN financial_accounts fa
              ON fa.hospital_id = a.hospital_id
             AND fa.account_type = 'ipd'
             AND fa.account_number LIKE 'ACC-' || EXTRACT(YEAR FROM CURRENT_DATE)::text || '-%'
            WHERE NOT EXISTS (
                SELECT 1 FROM financial_accounts existing
                WHERE existing.hospital_id = a.hospital_id
                  AND existing.patient_id = a.patient_id
                  AND existing.admission_id = a.id
                  AND existing.account_type = 'ipd'
            )
            GROUP BY a.id, a.hospital_id, a.patient_id
        ), numbered AS (
            SELECT id, hospital_id, patient_id, max_seq,
                   row_number() OVER (PARTITION BY hospital_id ORDER BY id) AS seq_offset
            FROM missing
        )
        INSERT INTO financial_accounts (
            id, hospital_id, patient_id, account_number, account_type, status,
            admission_id, created_by_name
        )
        SELECT gen_random_uuid(), hospital_id, patient_id,
               'ACC-' || EXTRACT(YEAR FROM CURRENT_DATE)::text || '-' ||
                    lpad((max_seq + seq_offset)::text, 5, '0'),
               'ipd', 'open', id, 'IPD account migration backfill'
        FROM numbered
        ON CONFLICT (hospital_id, account_number) DO NOTHING
    """)
    if dry_run:
        logger.info("WOULD BACKFILL missing admission-scoped IPD financial accounts")
    else:
        res = conn.execute(account_backfill_sql)
        conn.commit()
        logger.info("BACKFILLED %s IPD financial accounts", res.rowcount)

    duplicates = conn.execute(text("""
        SELECT hospital_id, admission_id, count(*) AS account_count
        FROM financial_accounts
        WHERE account_type = 'ipd' AND status = 'open' AND admission_id IS NOT NULL
        GROUP BY hospital_id, admission_id
        HAVING count(*) > 1
    """)).fetchall()
    if duplicates:
        raise RuntimeError(
            "Cannot enforce one open IPD financial account per admission; "
            f"resolve existing duplicate account rows first: {duplicates[:10]}"
        )
    if dry_run:
        logger.info("WOULD CREATE unique open IPD account index")
    else:
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_financial_accounts_open_ipd_admission
            ON financial_accounts (hospital_id, admission_id)
            WHERE admission_id IS NOT NULL AND account_type = 'ipd' AND status = 'open'
        """))
        conn.commit()
        logger.info("VERIFIED unique open IPD account index")

    primary_duplicates = conn.execute(text("""
        SELECT hospital_id, admission_id, count(*) AS member_count
        FROM admission_care_team_members
        WHERE is_active IS TRUE AND role = 'primary_consultant'
        GROUP BY hospital_id, admission_id
        HAVING count(*) > 1
    """)).fetchall()
    if primary_duplicates:
        raise RuntimeError(
            "Cannot enforce one active primary consultant per admission; "
            f"resolve existing duplicate assignments first: {primary_duplicates[:10]}"
        )
    if dry_run:
        logger.info("WOULD CREATE unique active primary consultant index")
    else:
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_admission_care_team_active_primary
            ON admission_care_team_members (hospital_id, admission_id)
            WHERE is_active IS TRUE AND role = 'primary_consultant'
        """))
        conn.commit()
        logger.info("VERIFIED unique active primary consultant index")


def _verify_data(conn: Connection) -> None:
    """Check episode/account ownership after schema changes and backfills."""
    missing = conn.execute(text("""
        SELECT count(*) FROM admissions a
        WHERE NOT EXISTS (
            SELECT 1 FROM financial_accounts fa
            WHERE fa.hospital_id = a.hospital_id
              AND fa.patient_id = a.patient_id
              AND fa.admission_id = a.id
              AND fa.account_type = 'ipd'
        )
    """)).scalar_one()
    mismatched = conn.execute(text("""
        SELECT count(*)
        FROM financial_accounts fa
        LEFT JOIN admissions a
          ON a.id = fa.admission_id AND a.hospital_id = fa.hospital_id
        WHERE fa.account_type = 'ipd' AND fa.admission_id IS NOT NULL
          AND (a.id IS NULL OR fa.patient_id <> a.patient_id)
    """)).scalar_one()
    duplicates = conn.execute(text("""
        SELECT count(*) FROM (
            SELECT hospital_id, admission_id
            FROM financial_accounts
            WHERE account_type = 'ipd' AND status = 'open' AND admission_id IS NOT NULL
            GROUP BY hospital_id, admission_id HAVING count(*) > 1
        ) duplicate_accounts
    """)).scalar_one()
    duplicate_primaries = conn.execute(text("""
        SELECT count(*) FROM (
            SELECT hospital_id, admission_id
            FROM admission_care_team_members
            WHERE is_active IS TRUE AND role = 'primary_consultant'
            GROUP BY hospital_id, admission_id HAVING count(*) > 1
        ) duplicate_primaries
    """)).scalar_one()
    logger.info(
        "Integrity check: admissions without matching IPD account=%s, "
        "IPD account ownership mismatches=%s, duplicate open IPD accounts=%s, "
        "admissions with duplicate active primary consultants=%s",
        missing,
        mismatched,
        duplicates,
        duplicate_primaries,
    )
    if missing or mismatched or duplicates or duplicate_primaries:
        raise RuntimeError("IPD financial account integrity check failed")


def run_migration(dry_run: bool = False) -> None:
    engine = get_transitional_sync_engine()
    with engine.connect() as conn:
        inspector = inspect(conn)
        _migrate_enums(conn, dry_run=dry_run)
        _migrate_tables(conn, inspector, dry_run=dry_run)
        # Re-inspect columns
        inspector = inspect(conn)
        _migrate_columns(conn, inspector, dry_run=dry_run)
        _backfill_data(conn, dry_run=dry_run)
        if not dry_run:
            _verify_data(conn)

    if dry_run:
        logger.info("\nDry run completed successfully. No changes were applied.")
    else:
        logger.info("\nMigration and backfill completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate IPD workflow improvements schema and backfill data")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without applying them")
    args = parser.parse_args()
    run_migration(dry_run=args.dry_run)
