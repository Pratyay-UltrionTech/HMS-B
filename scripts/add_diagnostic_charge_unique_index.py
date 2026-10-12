"""
Migration: one active charge per diagnostic service key.

Creates the partial unique index

    uq_billing_charges_active_diagnostic_source
        ON billing_charges (hospital_id, source_type, source_id)
        WHERE source_id IS NOT NULL AND status <> 'cancelled'
          AND source_type IN ('laboratory', 'radiology')

matching ``BillingCharge.__table_args__``. The application already ensures charges by
their stable key (``service_charge_resolver.ensure_service_charge``); the index is the
database backstop against double billing.

Idempotent and non-destructive:
- If any key already has more than one active charge, the duplicates are reported and
  the index is NOT created. Resolve them manually (cancel the extra charge through the
  billing UI / ``charge_reversal_service.cancel_charge`` so money is reversed with a
  trace), then re-run.
- Also reports (read-only) prescription requests whose charge is split between the
  request id and one of its orders (legacy re-pointed charges): both active means the
  request is billed twice.

Usage:  python scripts/add_diagnostic_charge_unique_index.py [--report-only]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from infrastructure.postgres.session import get_transitional_sync_engine

INDEX_NAME = "uq_billing_charges_active_diagnostic_source"

INDEX_DDL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
    ON billing_charges (hospital_id, source_type, source_id)
    WHERE source_id IS NOT NULL
      AND status <> 'cancelled'
      AND source_type IN ('laboratory', 'radiology');
"""

DUPLICATE_KEYS = """
SELECT hospital_id, source_type, source_id, COUNT(*) AS active_count,
       STRING_AGG(id::text, ', ' ORDER BY created_at) AS charge_ids
FROM billing_charges
WHERE source_id IS NOT NULL
  AND status <> 'cancelled'
  AND source_type IN ('laboratory', 'radiology')
GROUP BY hospital_id, source_type, source_id
HAVING COUNT(*) > 1
ORDER BY hospital_id, source_type
LIMIT 500;
"""

SPLIT_REQUEST_CHARGES = """
SELECT 'laboratory' AS kind, o.prescription_request_id AS request_id, o.id AS order_id,
       rc.id AS request_charge_id, oc.id AS order_charge_id
FROM lab_orders o
JOIN billing_charges oc ON oc.source_id = o.id AND oc.source_type = 'laboratory' AND oc.status <> 'cancelled'
JOIN billing_charges rc ON rc.source_id = o.prescription_request_id AND rc.source_type = 'laboratory'
     AND rc.status <> 'cancelled'
WHERE o.prescription_request_id IS NOT NULL
UNION ALL
SELECT 'radiology', o.prescription_request_id, o.id, rc.id, oc.id
FROM radiology_orders o
JOIN billing_charges oc ON oc.source_id = o.id AND oc.source_type = 'radiology' AND oc.status <> 'cancelled'
JOIN billing_charges rc ON rc.source_id = o.prescription_request_id AND rc.source_type = 'radiology'
     AND rc.status <> 'cancelled'
WHERE o.prescription_request_id IS NOT NULL
LIMIT 500;
"""


def run_migration(report_only: bool = False) -> int:
    engine = get_transitional_sync_engine()
    print(f"[*] Targeting database: {engine.url.database} on host {engine.url.host}")

    with engine.connect() as conn:
        dupes = conn.execute(text(DUPLICATE_KEYS)).fetchall()
        print(f"[1/3] Diagnostic keys with more than one active charge: {len(dupes)} (max 500 shown)")
        for r in dupes:
            print(
                f"    hospital={r.hospital_id} type={r.source_type} source={r.source_id} "
                f"active={r.active_count} charges=[{r.charge_ids}]"
            )
        split = conn.execute(text(SPLIT_REQUEST_CHARGES)).fetchall()
        print(f"[2/3] Requests billed on both the request and an order (legacy re-point): {len(split)}")
        for r in split:
            print(
                f"    {r.kind} request={r.request_id} order={r.order_id} "
                f"request_charge={r.request_charge_id} order_charge={r.order_charge_id}"
            )

    if report_only:
        print("[3/3] --report-only: index not created.")
        return 0
    if dupes:
        print(f"[3/3] SKIPPED creating {INDEX_NAME}: resolve the duplicate active charges above first.")
        return 1

    with engine.begin() as conn:
        conn.execute(text(INDEX_DDL))
    print(f"[3/3] Ensured unique index {INDEX_NAME}.")
    print("[OK] Diagnostic charge unique index migration completed.")
    return 0


if __name__ == "__main__":
    sys.exit(run_migration(report_only="--report-only" in sys.argv))
