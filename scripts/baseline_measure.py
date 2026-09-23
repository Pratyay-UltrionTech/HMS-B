"""
Baseline measurement script for Radiology performance audit.
Run with: python -m scripts.baseline_measure
"""

import time
import sys

sys.path.insert(0, ".")

from sqlalchemy import text
from infrastructure.postgres.engine import get_transitional_sync_engine


def main() -> None:
    engine = get_transitional_sync_engine()
    conn = engine.connect()

    # 1. Count radiology orders
    result = conn.execute(text("SELECT COUNT(*) FROM radiology_orders")).scalar()
    print(f"Total radiology orders: {result}")

    # 2. Count billing charges for radiology
    result2 = conn.execute(
        text("SELECT COUNT(*) FROM billing_charges WHERE source_type = 'radiology'")
    ).scalar()
    print(f"Radiology billing charges: {result2}")

    # 3. Get sample hospital_id
    sample = conn.execute(text("SELECT hospital_id FROM radiology_orders LIMIT 1")).fetchone()
    if not sample:
        print("No radiology orders found - cannot benchmark")
        conn.close()
        return

    hosp_id = sample[0]
    print(f"Sample hospital_id: {hosp_id}")

    order_count = conn.execute(
        text("SELECT COUNT(*) FROM radiology_orders WHERE hospital_id = :h"),
        {"h": hosp_id},
    ).scalar()
    print(f"Orders for that hospital: {order_count}")

    charge_count = conn.execute(
        text(
            "SELECT COUNT(*) FROM billing_charges WHERE hospital_id = :h AND source_type = 'radiology'"
        ),
        {"h": hosp_id},
    ).scalar()
    print(f"Radiology charges for that hospital: {charge_count}")

    # 4. Get a sample order ID for EXPLAIN
    sample_order = conn.execute(
        text("SELECT id FROM radiology_orders WHERE hospital_id = :h LIMIT 1"),
        {"h": hosp_id},
    ).fetchone()

    if sample_order:
        order_id = sample_order[0]
        print(f"\nSample order_id: {order_id}")

        # 5. EXPLAIN ANALYZE the financial clearance query (current approach)
        print("\n--- EXPLAIN (ANALYZE, BUFFERS) for financial clearance lookup ---")
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

    # 6. List existing indexes on billing_charges
    print("\n--- Existing indexes on billing_charges ---")
    index_rows = conn.execute(
        text("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'billing_charges'
            ORDER BY indexname
        """)
    ).fetchall()
    for row in index_rows:
        print(f"  {row[0]}: {row[1]}")

    # 7. Simulate the N+1: measure time to run individual clearance queries for all orders
    orders = conn.execute(
        text("SELECT id FROM radiology_orders WHERE hospital_id = :h ORDER BY ordered_at DESC LIMIT 50"),
        {"h": hosp_id},
    ).fetchall()
    print(f"\n--- Timing N+1 simulation for {len(orders)} orders ---")
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
    print(f"  {len(orders)} individual clearance queries: {elapsed*1000:.1f}ms total ({elapsed*1000/max(len(orders),1):.2f}ms avg/query)")

    # 8. Simulate bulk approach: measure time to run one IN(...) query
    order_ids = [str(row[0]) for row in orders]
    if order_ids:
        ids_literal = ", ".join(f"'{oid}'" for oid in order_ids)
        start2 = time.perf_counter()
        conn.execute(
            text(f"""
                SELECT DISTINCT ON (source_id) source_id, id, status, net_amount, amount_paid, created_at
                FROM billing_charges
                WHERE hospital_id = :h
                  AND source_type = 'radiology'
                  AND source_id IN ({ids_literal})
                ORDER BY source_id, created_at DESC
            """),
            {"h": hosp_id},
        ).fetchall()
        elapsed2 = time.perf_counter() - start2
        print(f"  1 bulk IN(...) query for same {len(orders)} orders: {elapsed2*1000:.1f}ms")
        print(f"  Speedup ratio: {elapsed/elapsed2:.1f}x")

    conn.close()


if __name__ == "__main__":
    main()
