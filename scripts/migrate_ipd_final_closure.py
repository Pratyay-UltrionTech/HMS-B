"""
Zero-Alembic schema migration script for IPD Final Closure.
Enhances admissions table for discharge medication tracking and updates
billing invoices/lines/receipts to NUMERIC(14, 2) on Azure PostgreSQL.
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
        print("[1/5] Adding discharge prescription tracking columns to admissions...")
        conn.execute(
            text("""
            ALTER TABLE admissions
            ADD COLUMN IF NOT EXISTS no_discharge_meds BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS no_discharge_meds_reason TEXT,
            ADD COLUMN IF NOT EXISTS no_discharge_meds_doctor_id UUID REFERENCES hospital_users(id) ON DELETE SET NULL;
            """)
        )

        print("[2/5] Updating billing_invoices monetary columns to NUMERIC(14, 2)...")
        conn.execute(
            text("""
            ALTER TABLE billing_invoices
            ALTER COLUMN subtotal TYPE NUMERIC(14, 2) USING subtotal::numeric(14, 2),
            ALTER COLUMN discount_amount TYPE NUMERIC(14, 2) USING discount_amount::numeric(14, 2),
            ALTER COLUMN taxable_amount TYPE NUMERIC(14, 2) USING taxable_amount::numeric(14, 2),
            ALTER COLUMN tax_amount TYPE NUMERIC(14, 2) USING tax_amount::numeric(14, 2),
            ALTER COLUMN cgst_amount TYPE NUMERIC(14, 2) USING cgst_amount::numeric(14, 2),
            ALTER COLUMN sgst_amount TYPE NUMERIC(14, 2) USING sgst_amount::numeric(14, 2),
            ALTER COLUMN igst_amount TYPE NUMERIC(14, 2) USING igst_amount::numeric(14, 2),
            ALTER COLUMN grand_total TYPE NUMERIC(14, 2) USING grand_total::numeric(14, 2);
            """)
        )

        print("[3/5] Updating billing_invoice_lines monetary columns to NUMERIC(14, 2)...")
        conn.execute(
            text("""
            ALTER TABLE billing_invoice_lines
            ALTER COLUMN quantity TYPE NUMERIC(14, 2) USING quantity::numeric(14, 2),
            ALTER COLUMN rate TYPE NUMERIC(14, 2) USING rate::numeric(14, 2),
            ALTER COLUMN amount TYPE NUMERIC(14, 2) USING amount::numeric(14, 2),
            ALTER COLUMN cgst_amount TYPE NUMERIC(14, 2) USING cgst_amount::numeric(14, 2),
            ALTER COLUMN sgst_amount TYPE NUMERIC(14, 2) USING sgst_amount::numeric(14, 2);
            """)
        )

        print("[4/5] Updating billing_receipts amount column to NUMERIC(14, 2)...")
        conn.execute(
            text("""
            ALTER TABLE billing_receipts
            ALTER COLUMN amount TYPE NUMERIC(14, 2) USING amount::numeric(14, 2);
            """)
        )

        print("[5/5] Ensuring index on prescriptions(hospital_id, admission_id)...")
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS ix_prescriptions_admission
            ON prescriptions(hospital_id, admission_id)
            WHERE admission_id IS NOT NULL;
            """)
        )

    print("[OK] IPD Final Closure migration executed successfully!")


if __name__ == "__main__":
    run_migration()
