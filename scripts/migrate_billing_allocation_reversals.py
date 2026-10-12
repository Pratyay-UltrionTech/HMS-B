"""
Migration: append-only allocation reversals for billing_payment_allocations.

Adds reversal columns so refunds / cancellations reverse allocations by inserting
negative rows instead of mutating or deleting history:

- reversal_of_id     UUID  -> billing_payment_allocations(id)  (original allocation)
- refund_id          UUID  -> billing_refunds(id)              (reversal caused by a refund)
- credit_deposit_id  UUID  -> billing_deposits(id)             (money moved to cancellation credit)
- reversal_reason    VARCHAR(512)

Idempotent (IF NOT EXISTS everywhere). Non-destructive. After the DDL it prints a
read-only reconciliation report of charges whose stored ``amount_paid`` differs
from the sum of their allocation rows (e.g. charges that were marked paid by a
status edit). Those rows are NOT modified; review them manually.

Usage:  python scripts/migrate_billing_allocation_reversals.py [--report-only]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from infrastructure.postgres.session import get_transitional_sync_engine


DDL = """
ALTER TABLE billing_payment_allocations
    ADD COLUMN IF NOT EXISTS reversal_of_id UUID
        REFERENCES billing_payment_allocations(id) ON DELETE SET NULL;
ALTER TABLE billing_payment_allocations
    ADD COLUMN IF NOT EXISTS refund_id UUID
        REFERENCES billing_refunds(id) ON DELETE SET NULL;
ALTER TABLE billing_payment_allocations
    ADD COLUMN IF NOT EXISTS credit_deposit_id UUID
        REFERENCES billing_deposits(id) ON DELETE SET NULL;
ALTER TABLE billing_payment_allocations
    ADD COLUMN IF NOT EXISTS reversal_reason VARCHAR(512);

CREATE INDEX IF NOT EXISTS ix_allocations_reversal_of
    ON billing_payment_allocations(reversal_of_id)
    WHERE reversal_of_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_billing_payment_allocations_refund_id
    ON billing_payment_allocations(refund_id)
    WHERE refund_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_billing_payment_allocations_credit_deposit_id
    ON billing_payment_allocations(credit_deposit_id)
    WHERE credit_deposit_id IS NOT NULL;
"""

RECONCILIATION_REPORT = """
SELECT c.id, c.hospital_id, c.patient_id, c.source_type, c.status,
       c.net_amount, c.amount_paid, COALESCE(a.alloc_sum, 0) AS alloc_sum
FROM billing_charges c
LEFT JOIN (
    SELECT charge_id, SUM(allocated_amount) AS alloc_sum
    FROM billing_payment_allocations
    GROUP BY charge_id
) a ON a.charge_id = c.id
WHERE c.status IN ('paid', 'partially_paid', 'pending')
  AND c.net_amount > 0
  AND ABS(c.amount_paid - COALESCE(a.alloc_sum, 0)) > 0.009
ORDER BY c.created_at
LIMIT 500;
"""

CANCELLED_WITH_MONEY_REPORT = """
SELECT c.id, c.hospital_id, c.patient_id, c.source_type, c.amount_paid,
       COALESCE(a.alloc_sum, 0) AS alloc_sum
FROM billing_charges c
LEFT JOIN (
    SELECT charge_id, SUM(allocated_amount) AS alloc_sum
    FROM billing_payment_allocations
    GROUP BY charge_id
) a ON a.charge_id = c.id
WHERE c.status = 'cancelled'
  AND (c.amount_paid > 0.009 OR COALESCE(a.alloc_sum, 0) > 0.009)
ORDER BY c.created_at
LIMIT 500;
"""


def run_migration(report_only: bool = False) -> None:
    engine = get_transitional_sync_engine()
    print(f"[*] Targeting database: {engine.url.database} on host {engine.url.host}")

    if not report_only:
        with engine.begin() as conn:
            print("[1/2] Adding allocation reversal columns + indexes (idempotent)...")
            conn.execute(text(DDL))

    with engine.connect() as conn:
        print("[2/2] Read-only reconciliation report")
        rows = conn.execute(text(RECONCILIATION_REPORT)).fetchall()
        print(f"  - Active charges whose amount_paid != SUM(allocations): {len(rows)} (max 500 shown)")
        for r in rows:
            print(
                f"    charge={r.id} hospital={r.hospital_id} patient={r.patient_id} "
                f"type={r.source_type} status={r.status} net={r.net_amount} "
                f"amount_paid={r.amount_paid} alloc_sum={r.alloc_sum}"
            )
        rows = conn.execute(text(CANCELLED_WITH_MONEY_REPORT)).fetchall()
        print(f"  - Cancelled charges still holding money (stranded): {len(rows)} (max 500 shown)")
        for r in rows:
            print(
                f"    charge={r.id} hospital={r.hospital_id} patient={r.patient_id} "
                f"type={r.source_type} amount_paid={r.amount_paid} alloc_sum={r.alloc_sum}"
            )

    print("[OK] Billing allocation reversal migration completed.")


if __name__ == "__main__":
    run_migration(report_only="--report-only" in sys.argv)
