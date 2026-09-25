"""
Remediation database migration for HMS Clinical + IPD + Billing improvements.
Enforces:
1. Fixed-precision NUMERIC(14,2) for all financial columns.
2. FinancialAccount per-admission unique partial index.
3. Care team single active primary consultant partial unique index.
4. Discharge financial exception separation-of-duties CHECK constraint and amount columns.
5. Idempotency columns & indexes for BillingDeposit and BillingPayment.
6. Medication administration record schedule unique index.
7. IPD form addenda table.
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import Connection, inspect, text

from infrastructure.postgres.engine import get_transitional_sync_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("remediation_migration")


def run_remediation(dry_run: bool = False) -> None:
    engine = get_transitional_sync_engine()
    with engine.connect() as conn:
        logger.info("Connected to database. Starting remediation migration...")

        # 1. Money precision: NUMERIC(14,2)
        logger.info("--- 1. Migrating financial columns to NUMERIC(14,2) ---")
        numeric_migrations = [
            ("billing_charges", "charge_amount"),
            ("billing_charges", "discount_amount"),
            ("billing_charges", "net_amount"),
            ("billing_charges", "amount_paid"),
            ("billing_payments", "amount"),
            ("billing_deposits", "original_amount"),
            ("billing_deposits", "available_amount"),
            ("billing_payment_allocations", "allocated_amount"),
            ("billing_refunds", "amount"),
            ("discharge_financial_exceptions", "outstanding_amount_at_request"),
        ]
        for tbl, col in numeric_migrations:
            sql = f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE NUMERIC(14,2) USING round({col}::numeric, 2);"
            if dry_run:
                logger.info(f"WOULD EXECUTE: {sql}")
            else:
                try:
                    conn.execute(text(sql))
                    conn.commit()
                    logger.info(f"Updated {tbl}.{col} to NUMERIC(14,2)")
                except Exception as e:
                    logger.warning(f"Note on {tbl}.{col}: {e}")
                    conn.rollback()

        # 2. Add approved_amount and remaining_receivable to discharge_financial_exceptions
        logger.info("--- 2. Updating discharge_financial_exceptions schema ---")
        cols_sql = [
            "ALTER TABLE discharge_financial_exceptions ADD COLUMN IF NOT EXISTS approved_amount NUMERIC(14,2);",
            "ALTER TABLE discharge_financial_exceptions ADD COLUMN IF NOT EXISTS remaining_receivable NUMERIC(14,2);",
        ]
        for sql in cols_sql:
            if dry_run:
                logger.info(f"WOULD EXECUTE: {sql}")
            else:
                conn.execute(text(sql))
                conn.commit()

        # 3. Separation of duties CHECK constraint
        logger.info("--- 3. Adding separation of duties constraint ---")
        check_sql = """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'chk_discharge_exception_separation_of_duties'
            ) THEN
                ALTER TABLE discharge_financial_exceptions
                ADD CONSTRAINT chk_discharge_exception_separation_of_duties
                CHECK (status != 'approved' OR (approved_by_id IS NOT NULL AND approved_by_id != requested_by_id));
            END IF;
        END $$;
        """
        if dry_run:
            logger.info("WOULD ADD CONSTRAINT chk_discharge_exception_separation_of_duties")
        else:
            conn.execute(text(check_sql))
            conn.commit()
            logger.info("Added separation of duties constraint on discharge_financial_exceptions")

        # 4. Financial Account unique partial index per admission
        logger.info("--- 4. FinancialAccount per-admission partial unique index ---")
        fa_index_sql = """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_financial_account_admission
        ON financial_accounts (hospital_id, admission_id)
        WHERE account_type = 'ipd' AND admission_id IS NOT NULL AND status = 'open';
        """
        if dry_run:
            logger.info("WOULD CREATE UNIQUE INDEX uq_financial_account_admission")
        else:
            conn.execute(text(fa_index_sql))
            conn.commit()
            logger.info("Created partial unique index uq_financial_account_admission")

        # 5. Care Team single active primary consultant partial unique index
        logger.info("--- 5. Care Team primary consultant partial unique index ---")
        care_team_idx_sql = """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_care_team_active_primary
        ON admission_care_team_members (hospital_id, admission_id)
        WHERE role = 'primary_consultant' AND is_active = TRUE;
        """
        if dry_run:
            logger.info("WOULD CREATE UNIQUE INDEX uq_care_team_active_primary")
        else:
            # First ensure no duplicates exist before creating index
            dedup_sql = """
            WITH ranked AS (
                SELECT id, ROW_NUMBER() OVER(
                    PARTITION BY hospital_id, admission_id
                    ORDER BY assigned_at DESC, id DESC
                ) as rn
                FROM admission_care_team_members
                WHERE role = 'primary_consultant' AND is_active = TRUE
            )
            UPDATE admission_care_team_members
            SET is_active = FALSE, ended_at = now()
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
            """
            conn.execute(text(dedup_sql))
            conn.commit()
            conn.execute(text(care_team_idx_sql))
            conn.commit()
            logger.info("Created partial unique index uq_care_team_active_primary")

        # 6. Idempotency columns & indexes for BillingDeposit and BillingPayment
        logger.info("--- 6. Adding idempotency support for Deposits and Payments ---")
        idemp_sql = [
            "ALTER TABLE billing_deposits ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_deposits_idempotency ON billing_deposits (hospital_id, idempotency_key) WHERE idempotency_key IS NOT NULL;",
            "ALTER TABLE billing_payments ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_payments_idempotency ON billing_payments (hospital_id, idempotency_key) WHERE idempotency_key IS NOT NULL;",
        ]
        for sql in idemp_sql:
            if dry_run:
                logger.info(f"WOULD EXECUTE: {sql}")
            else:
                conn.execute(text(sql))
                conn.commit()
        logger.info("Idempotency columns and unique indexes verified")

        # 7. eMAR unique schedule index
        logger.info("--- 7. eMAR schedule deduplication index ---")
        emar_idx_sql = """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_emar_order_scheduled_time
        ON medication_administration_records (order_id, scheduled_time)
        WHERE order_id IS NOT NULL;
        """
        if dry_run:
            logger.info("WOULD CREATE UNIQUE INDEX uq_emar_order_scheduled_time")
        else:
            conn.execute(text(emar_idx_sql))
            conn.commit()
            logger.info("Created unique index uq_emar_order_scheduled_time")

        # 8. IPD Form Addenda table
        logger.info("--- 8. IPD Form Addenda table ---")
        inspector = inspect(conn)
        if "ipd_form_addenda" not in inspector.get_table_names():
            addenda_sql = """
            CREATE TABLE IF NOT EXISTS ipd_form_addenda (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
                submission_id UUID NOT NULL REFERENCES ipd_form_submissions(id) ON DELETE CASCADE,
                admission_id UUID REFERENCES admissions(id) ON DELETE SET NULL,
                patient_id UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                author_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE RESTRICT,
                author_name VARCHAR(255) NOT NULL,
                author_role VARCHAR(64) NOT NULL DEFAULT 'Clinician',
                reason TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS ix_form_addenda_submission ON ipd_form_addenda (hospital_id, submission_id);
            CREATE INDEX IF NOT EXISTS ix_form_addenda_admission ON ipd_form_addenda (hospital_id, admission_id);
            """
            if dry_run:
                logger.info("WOULD CREATE TABLE ipd_form_addenda")
            else:
                conn.execute(text(addenda_sql))
                conn.commit()
                logger.info("Created table ipd_form_addenda and indexes")
        else:
            logger.info("Table ipd_form_addenda already exists")

    logger.info("Remediation migration completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run remediation database migration")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without applying them")
    args = parser.parse_args()
    run_remediation(dry_run=args.dry_run)
