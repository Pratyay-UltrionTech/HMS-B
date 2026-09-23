"""
Post-optimization performance measurement.
Run with: python -m scripts.after_measure
"""

import time
import sys

sys.path.insert(0, ".")

from sqlalchemy import text
from infrastructure.postgres.engine import get_transitional_sync_engine


def main() -> None:
    engine = get_transitional_sync_engine()
    conn = engine.connect()

    # Get sample hospital and order
    sample = conn.execute(text("SELECT hospital_id FROM radiology_orders LIMIT 1")).fetchone()
    if not sample:
        print("No radiology orders found")
        conn.close()
        return

    hosp_id = sample[0]

    sample_order = conn.execute(
        text("SELECT id FROM radiology_orders WHERE hospital_id = :h LIMIT 1"),
        {"h": hosp_id},
    ).fetchone()

    if sample_order:
        order_id = sample_order[0]
        print(f"\n--- EXPLAIN (ANALYZE, BUFFERS) after adding covering index ---")
        explain_sql = text("""
            EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
            SELECT *
            FROM billing_charges
            WHERE hospital_id = :h
              AND source_type = 'radiology'
              AND source_id = :s
            ORDER BY created_at DESC
            LIMIT 1
        """)
        rows = conn.execute(explain_sql, {"h": hosp_id, "s": order_id}).fetchall()
        for row in rows:
            print(row[0])

    # Verify the new index exists
    print("\n--- Indexes on billing_charges ---")
    index_rows = conn.execute(
        text("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'billing_charges'
              AND indexname LIKE '%source%'
            ORDER BY indexname
        """)
    ).fetchall()
    for row in index_rows:
        print(f"  {row[0]}:")
        print(f"    {row[1]}")

    # Simulate N+1 again (baseline for comparison)
    orders = conn.execute(
        text("SELECT id FROM radiology_orders WHERE hospital_id = :h ORDER BY ordered_at DESC LIMIT 50"),
        {"h": hosp_id},
    ).fetchall()
    print(f"\n--- After: Timing N+1 simulation for {len(orders)} orders ---")
    start = time.perf_counter()
    for row in orders:
        conn.execute(
            text("""
                SELECT id FROM billing_charges
                WHERE hospital_id = :h AND source_type = 'radiology' AND source_id = :s
                ORDER BY created_at DESC LIMIT 1
            """),
            {"h": hosp_id, "s": row[0]},
        ).fetchone()
    elapsed = time.perf_counter() - start
    print(f"  {len(orders)} individual queries: {elapsed*1000:.1f}ms ({elapsed*1000/max(len(orders),1):.2f}ms avg)")

    # Simulate bulk query
    order_ids = [str(row[0]) for row in orders]
    if order_ids:
        ids_literal = ", ".join(f"'{oid}'" for oid in order_ids)
        start2 = time.perf_counter()
        conn.execute(
            text(f"""
                SELECT source_id, id, status, net_amount, amount_paid, created_at
                FROM billing_charges
                WHERE hospital_id = :h
                  AND source_type = 'radiology'
                  AND source_id IN ({ids_literal})
                ORDER BY source_id, created_at DESC
            """),
            {"h": hosp_id},
        ).fetchall()
        elapsed2 = time.perf_counter() - start2
        print(f"  1 bulk query for same {len(orders)} orders: {elapsed2*1000:.1f}ms")
        print(f"  Speedup: N+1 to bulk = {elapsed/max(elapsed2,0.0001):.1f}x")

    # Test the actual bulk_check_service_financial_clearance function
    print("\n--- Testing bulk_check_service_financial_clearance function ---")
    conn.close()

    # Use SQLAlchemy session for ORM test
    from infrastructure.postgres.session import get_transitional_sync_session
    from modules.billing.entities.billing_entities import BillingSourceType
    from modules.billing.services.service_financial_clearance import (
        check_service_financial_clearance,
        bulk_check_service_financial_clearance,
    )
    import uuid

    db = next(get_transitional_sync_session())
    try:
        engine2 = get_transitional_sync_engine()
        conn2 = engine2.connect()
        order_rows = conn2.execute(
            text("SELECT id FROM radiology_orders WHERE hospital_id = :h ORDER BY ordered_at DESC LIMIT 50"),
            {"h": hosp_id},
        ).fetchall()
        conn2.close()

        order_uuids = [row[0] for row in order_rows]
        print(f"Testing with {len(order_uuids)} orders...")

        # Single-item calls (baseline)
        t0 = time.perf_counter()
        single_results = {}
        for oid in order_uuids:
            single_results[oid] = check_service_financial_clearance(
                db, hosp_id, BillingSourceType.radiology, oid
            )
        t_single = time.perf_counter() - t0

        # Bulk call
        t1 = time.perf_counter()
        bulk_results = bulk_check_service_financial_clearance(
            db, hosp_id, BillingSourceType.radiology, order_uuids
        )
        t_bulk = time.perf_counter() - t1

        print(f"\nSingle-item path ({len(order_uuids)} calls): {t_single*1000:.1f}ms total")
        print(f"Bulk path (1 call): {t_bulk*1000:.1f}ms total")
        print(f"Speedup: {t_single/max(t_bulk,0.0001):.1f}x")

        # Semantic validation: verify identical results
        mismatches = 0
        for oid in order_uuids:
            s = single_results[oid]
            b = bulk_results.get(oid)
            if b is None:
                print(f"  MISSING in bulk: {oid}")
                mismatches += 1
                continue
            if (s.is_cleared != b.is_cleared or s.status != b.status or
                    abs(s.net_amount - b.net_amount) > 0.001 or
                    abs(s.amount_paid - b.amount_paid) > 0.001 or
                    abs(s.outstanding_amount - b.outstanding_amount) > 0.001):
                print(f"  MISMATCH for {oid}:")
                print(f"    Single: is_cleared={s.is_cleared} status={s.status} net={s.net_amount} paid={s.amount_paid} outstanding={s.outstanding_amount}")
                print(f"    Bulk:   is_cleared={b.is_cleared} status={b.status} net={b.net_amount} paid={b.amount_paid} outstanding={b.outstanding_amount}")
                mismatches += 1

        if mismatches == 0:
            print(f"\n  VALIDATION PASSED: all {len(order_uuids)} orders produce identical results.")
        else:
            print(f"\n  VALIDATION FAILED: {mismatches} mismatches found!")

    finally:
        db.close()


if __name__ == "__main__":
    main()
