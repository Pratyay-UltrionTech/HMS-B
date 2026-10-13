"""
Migration: Add immutable financial snapshot columns to billing_receipts.

Freezes historical balances at receipt issuance time so subsequent charges
do not mutate the reprinted receipt balance.

Columns added:
- account_id_snapshot UUID
- amount_allocated_snapshot NUMERIC(14, 2)
- account_balance_before_snapshot NUMERIC(14, 2)
- account_balance_after_snapshot NUMERIC(14, 2)
- patient_outstanding_after_snapshot NUMERIC(14, 2)
- available_deposit_after_snapshot NUMERIC(14, 2)
- net_patient_balance_after_snapshot NUMERIC(14, 2)

Idempotent (IF NOT EXISTS everywhere). Non-destructive.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from infrastructure.postgres.session import get_transitional_sync_engine


DDL_POSTGRES = """
ALTER TABLE billing_receipts
    ADD COLUMN IF NOT EXISTS account_id_snapshot UUID;
ALTER TABLE billing_receipts
    ADD COLUMN IF NOT EXISTS amount_allocated_snapshot NUMERIC(14, 2);
ALTER TABLE billing_receipts
    ADD COLUMN IF NOT EXISTS account_balance_before_snapshot NUMERIC(14, 2);
ALTER TABLE billing_receipts
    ADD COLUMN IF NOT EXISTS account_balance_after_snapshot NUMERIC(14, 2);
ALTER TABLE billing_receipts
    ADD COLUMN IF NOT EXISTS patient_outstanding_after_snapshot NUMERIC(14, 2);
ALTER TABLE billing_receipts
    ADD COLUMN IF NOT EXISTS available_deposit_after_snapshot NUMERIC(14, 2);
ALTER TABLE billing_receipts
    ADD COLUMN IF NOT EXISTS net_patient_balance_after_snapshot NUMERIC(14, 2);
"""


def run_migration() -> None:
    try:
        engine = get_transitional_sync_engine()
    except Exception as e:
        print(f"Transitional sync engine unavailable in this environment: {e}")
        return

    is_sqlite = engine.dialect.name == "sqlite"

    with engine.begin() as conn:
        if is_sqlite:
            # SQLite does not support multiple ADD COLUMN IF NOT EXISTS in one statement or IF NOT EXISTS for ADD COLUMN
            cols = [
                ("account_id_snapshot", "TEXT"),
                ("amount_allocated_snapshot", "NUMERIC(14, 2)"),
                ("account_balance_before_snapshot", "NUMERIC(14, 2)"),
                ("account_balance_after_snapshot", "NUMERIC(14, 2)"),
                ("patient_outstanding_after_snapshot", "NUMERIC(14, 2)"),
                ("available_deposit_after_snapshot", "NUMERIC(14, 2)"),
                ("net_patient_balance_after_snapshot", "NUMERIC(14, 2)"),
            ]
            for col_name, col_type in cols:
                try:
                    conn.execute(text(f"ALTER TABLE billing_receipts ADD COLUMN {col_name} {col_type};"))
                except Exception:
                    # Column already exists
                    pass
        else:
            conn.execute(text(DDL_POSTGRES))
    print("Migration applied successfully: billing_receipts snapshot columns ready.")


if __name__ == "__main__":
    run_migration()
