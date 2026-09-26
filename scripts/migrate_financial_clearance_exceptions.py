"""
Migration script for Financial Clearance Exceptions.
Creates financial_clearance_exceptions table with separation of duties check constraint.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from infrastructure.postgres.session import get_transitional_sync_engine


def run_migration() -> None:
    engine = get_transitional_sync_engine()
    print(f"[*] Targeting database: {engine.url.database} on host {engine.url.host}")

    with engine.begin() as conn:
        print("[1/2] Creating financial_clearance_exceptions table...")
        conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS financial_clearance_exceptions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                hospital_id UUID NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
                patient_id UUID NOT NULL REFERENCES patients(id) ON DELETE RESTRICT,
                admission_id UUID REFERENCES admissions(id) ON DELETE CASCADE,
                financial_account_id UUID REFERENCES financial_accounts(id) ON DELETE CASCADE,
                service_type VARCHAR(64) NOT NULL,
                action_type VARCHAR(64) NOT NULL,
                source_id UUID,
                is_emergency BOOLEAN NOT NULL DEFAULT FALSE,
                required_amount NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
                amount_covered NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
                shortfall_amount NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
                reason_category VARCHAR(64) NOT NULL,
                reason TEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                requested_by_id UUID NOT NULL REFERENCES hospital_users(id) ON DELETE RESTRICT,
                requested_by_name VARCHAR(255) NOT NULL,
                requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                approved_by_id UUID REFERENCES hospital_users(id) ON DELETE RESTRICT,
                approved_by_name VARCHAR(255),
                approved_at TIMESTAMPTZ,
                approval_remarks TEXT,
                is_consumed BOOLEAN NOT NULL DEFAULT FALSE,
                consumed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT chk_fin_clearance_separation_of_duties
                    CHECK (status != 'approved' OR is_emergency = TRUE OR (approved_by_id IS NOT NULL AND approved_by_id != requested_by_id))
            );
            """)
        )

        print("[2/2] Ensuring indexes on financial_clearance_exceptions...")
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS ix_fin_exc_hospital_id
                ON financial_clearance_exceptions(hospital_id);
            CREATE INDEX IF NOT EXISTS ix_fin_exc_admission
                ON financial_clearance_exceptions(hospital_id, admission_id)
                WHERE admission_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_fin_exc_patient
                ON financial_clearance_exceptions(hospital_id, patient_id);
            CREATE INDEX IF NOT EXISTS ix_fin_exc_status
                ON financial_clearance_exceptions(hospital_id, status);
            CREATE INDEX IF NOT EXISTS ix_fin_exc_service
                ON financial_clearance_exceptions(hospital_id, service_type, action_type);
            """)
        )

    print("[OK] Financial Clearance Exceptions migration completed successfully!")


if __name__ == "__main__":
    run_migration()
