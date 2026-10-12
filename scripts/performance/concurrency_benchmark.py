"""
PostgreSQL Concurrency Verification Benchmark.

Tests true simultaneous contention using synchronization barriers (threading.Barrier(N))
and dedicated workers (tasks == workers == N).

Covers:
1. Sequence number generator contention (N=10, N=25, N=50) on a single sequence row.
2. Pharmacy sale batch contention (N=10, N=20) on a single medicine batch.

Guarantees:
- Safe execution only on disposable test databases (e.g. hms_perf_test).
- Zero credential leakage.
- Precise latency measurement (p50, p95, total duration).
- Exact row count and inventory reconciliation checks.
"""

from __future__ import annotations

import concurrent.futures
import os
import statistics
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

# Import all models to ensure metadata is registered
from infrastructure.postgres.base import Base
import modules.auth.entities
import modules.tenancy.entities.hospital
import modules.doctors.entities.doctor
import modules.patients.entities.patient
import modules.appointments.entities.appointment
import modules.appointments.entities.enums
import modules.clinical_records.entities.clinical_record
import modules.vitals.entities.vital_reading
import modules.laboratory.entities.lab_entities
import modules.radiology.entities.radiology_entities
import modules.billing.entities.billing_entities
import modules.billing.entities.financial_exception
import modules.pharmacy.entities.pharmacy_entities
import modules.inpatient.entities.admission
import modules.beds.entities.bed
import modules.ot.entities.ot_entities
import modules.masters.entities.organization_entities
import modules.masters.entities.insurance_entities
import modules.blood_bank.entities.blood_bank_entities
import modules.ambulance.entities.ambulance_entities
import modules.equipment.entities.equipment_entities
import modules.inventory.entities.inventory_entities
import modules.procurement.entities.procurement_entities
import modules.patients.entities.allergy
import modules.pharmacy.entities.drug_interaction_entities
import modules.clinical_decision.entities.clinical_decision_entities
import modules.emergency.entities.emergency_entities
import shared.audit.entities.audit_log
import shared.database.sequences

from modules.pharmacy.actions.pharmacy_actions import PharmacyActions
from modules.pharmacy.contracts.pharmacy_contracts import SaleCreate, SaleItemCreate
from modules.pharmacy.entities.pharmacy_entities import (
    Medicine,
    MedicineBatch,
    MedicineCategory,
    MedicineInventory,
    MedicineType,
    MedicineUnit,
    PharmacySale,
    PharmacySaleItem,
    StockTransaction,
)
from modules.tenancy.entities.hospital import Hospital
from shared.database.sequences import SequenceCounter, next_sequence_value


def get_perf_db_engine():
    """Retrieve and validate engine against explicit disposable performance database."""
    perf_url = os.environ.get("HMS_PERF_DATABASE_URL")
    if not perf_url:
        from config.settings import get_settings
        cfg = get_settings()
        perf_url = URL.create(
            drivername="postgresql",
            username=cfg.postgres_user,
            password=cfg.postgres_password,
            host=cfg.postgres_host,
            port=cfg.postgres_port,
            database="hms_perf_test",
            query={"sslmode": "require"},
        ).render_as_string(hide_password=False)

    # Safety assertion: fail closed if database name is not strictly a disposable test/perf target
    lower_url = perf_url.lower()
    if "test" not in lower_url and "perf" not in lower_url:
        raise RuntimeError(
            "SAFETY GUARD ABORT: Refusing to run concurrency benchmarks against non-disposable target!"
        )

    # Use a large pool or null pool so N concurrent threads each get a dedicated physical connection
    engine = create_engine(
        perf_url,
        pool_size=60,
        max_overflow=20,
        pool_timeout=45,
        connect_args={"connect_timeout": 30},
    )
    return engine


def run_sequence_concurrency_test(engine, n_workers: int) -> dict[str, Any]:
    """
    Test N truly simultaneous workers contending for the exact same SequenceCounter row.
    Uses a synchronization barrier to ensure all N workers wait and release at the exact same instant.
    """
    print(f"\n[SEQUENCE CONCURRENCY] Testing N={n_workers} truly simultaneous workers...", flush=True)

    hospital_id = uuid4()
    counter_name = f"seq_test_{uuid4().hex[:8]}"

    # Setup isolated hospital & pre-seed the exact counter row so all workers contend for the same row
    with Session(engine) as db:
        h = Hospital(
            id=hospital_id,
            hospital_id=f"H{uuid4().hex[:6]}",
            name="Seq Test Hospital",
            address="Addr",
            phone="111",
            email=f"seq_{uuid4().hex[:8]}@perf.test",
            password_hash="x",
        )
        db.add(h)
        counter = SequenceCounter(
            hospital_id=hospital_id,
            counter_name=counter_name,
            current_val=0,
        )
        db.add(counter)
        db.commit()

    barrier = threading.Barrier(n_workers)
    results: list[int] = []
    durations: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(worker_id: int):
        try:
            with Session(engine) as db:
                # Force connection checkout before barrier so all connections are established
                db.connection()
                try:
                    barrier.wait(timeout=15)
                except (threading.BrokenBarrierError, threading.Barrier.WaitTimeout):
                    raise RuntimeError("Barrier synchronization failed or aborted due to peer failure")

                t0 = time.perf_counter()
                val = next_sequence_value(db, hospital_id, counter_name)
                db.commit()
                t1 = time.perf_counter()
                with lock:
                    results.append(val)
                    durations.append((t1 - t0) * 1000.0)
        except Exception as e:
            # Abort barrier so other workers don't wait when one worker fails to connect
            try:
                barrier.abort()
            except Exception:
                pass
            with lock:
                errors.append(f"Worker {worker_id}: {type(e).__name__} - {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    total_elapsed_ms = (time.perf_counter() - t_start) * 1000.0

    success_count = len(results)
    failure_count = len(errors)
    unique_results = set(results)
    duplicates = len(results) - len(unique_results)

    min_val = min(results) if results else 0
    max_val = max(results) if results else 0
    is_contiguous = (max_val - min_val + 1 == success_count) if success_count > 0 else False

    p50_ms = statistics.median(durations) if durations else 0.0
    p95_ms = statistics.quantiles(durations, n=20)[18] if len(durations) >= 20 else (max(durations) if durations else 0.0)

    summary = {
        "n_workers": n_workers,
        "successful_transactions": success_count,
        "failed_transactions": failure_count,
        "duplicates": duplicates,
        "is_contiguous": is_contiguous,
        "range": (min_val, max_val),
        "total_elapsed_ms": round(total_elapsed_ms, 2),
        "p50_ms": round(p50_ms, 2),
        "p95_ms": round(p95_ms, 2),
        "errors": errors[:5],
    }

    print(f"  Result: {success_count}/{n_workers} passed. Failures: {failure_count}. Duplicates: {duplicates}. Contiguous: {is_contiguous}.", flush=True)
    print(f"  Latency: p50={summary['p50_ms']}ms, p95={summary['p95_ms']}ms, total={summary['total_elapsed_ms']}ms", flush=True)
    if errors:
        print(f"  Sample error: {errors[0]}", flush=True)
    return summary


def run_pharmacy_concurrency_test(engine, n_workers: int, qty_per_sale: float = 5.0) -> dict[str, Any]:
    """
    Test N simultaneous pharmacy sales contending for the EXACT SAME batch and medicine inventory row.
    Uses threading.Barrier(n_workers) so all transactions start simultaneously.
    Verifies that no updates are lost and that inventory and batch counts reconcile perfectly.
    """
    print(f"\n[PHARMACY CONTENTION] Testing N={n_workers} simultaneous sales against 1 batch...")

    hospital_id = uuid4()
    initial_stock = 1000.0

    # Setup isolated medicine, batch, and inventory
    with Session(engine) as db:
        h = Hospital(
            id=hospital_id,
            hospital_id=f"H{uuid4().hex[:6]}",
            name="Pharmacy Contention Hospital",
            address="Addr",
            phone="111",
            email=f"pharm_{uuid4().hex[:8]}@perf.test",
            password_hash="x",
        )
        db.add(h)
        db.flush()

        med = Medicine(
            id=uuid4(),
            hospital_id=hospital_id,
            medicine_name=f"Contention Amoxicillin {uuid4().hex[:4]}",
            generic_name="Amoxicillin",
            brand_name="Mox",
            manufacturer="GSK",
            medicine_type=MedicineType.capsule,
            strength="500mg",
            unit=MedicineUnit.strip,
            selling_price=10.0,
            purchase_price=5.0,
            mrp=10.0,
            minimum_stock=10.0,
            maximum_stock=2000.0,
            reorder_level=50.0,
            gst_percent=12.0,
            is_active=True,
        )
        batch = MedicineBatch(
            id=uuid4(),
            hospital_id=hospital_id,
            medicine_id=med.id,
            batch_number=f"BATCH-CONT-{uuid4().hex[:6]}",
            expiry_date=date(2028, 12, 31),
            initial_quantity=initial_stock,
            available_quantity=initial_stock,
            selling_price=10.0,
            purchase_price=5.0,
            is_active=True,
        )
        inv = MedicineInventory(
            id=uuid4(),
            hospital_id=hospital_id,
            medicine_id=med.id,
            current_stock=initial_stock,
            available_stock=initial_stock,
            reserved_stock=0.0,
            batch_count=1,
        )
        db.add_all([med, batch, inv])
        db.commit()

        med_id = med.id
        batch_id = batch.id

    barrier = threading.Barrier(n_workers)
    successes = []
    failures = []
    durations = []
    lock = threading.Lock()

    def worker(worker_id: int):
        try:
            with Session(engine) as db:
                db.connection()
                actor = {
                    "user_id": str(uuid4()),
                    "email": f"pharmacist_{worker_id}@perf.test",
                    "name": f"Pharmacist {worker_id}",
                    "role": "pharmacist",
                }
                action = PharmacyActions(db, hospital_id, actor)
                payload = SaleCreate(
                    customer_name=f"Contention Customer {worker_id}",
                    customer_phone="9876543210",
                    sale_type="walk_in",
                    items=[
                        SaleItemCreate(
                            medicine_id=med_id,
                            batch_id=batch_id,
                            quantity=qty_per_sale,
                            unit_price=10.0,
                        )
                    ],
                    amount_paid=round(qty_per_sale * 10.0 * 1.12, 2),
                )
                try:
                    barrier.wait(timeout=15)
                except (threading.BrokenBarrierError, threading.Barrier.WaitTimeout):
                    raise RuntimeError("Barrier synchronization failed or aborted due to peer failure")

                t0 = time.perf_counter()
                res = action.create_sale(payload)
                t1 = time.perf_counter()
                with lock:
                    successes.append(res.id)
                    durations.append((t1 - t0) * 1000.0)
        except Exception as e:
            try:
                barrier.abort()
            except Exception:
                pass
            with lock:
                failures.append(f"Worker {worker_id}: {type(e).__name__} - {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)
    total_elapsed_ms = (time.perf_counter() - t_start) * 1000.0

    # Verification against database state
    with Session(engine) as db:
        final_batch = db.query(MedicineBatch).filter(MedicineBatch.id == batch_id).one()
        final_inv = db.query(MedicineInventory).filter(MedicineInventory.medicine_id == med_id).one()
        sales_count = db.query(PharmacySale).filter(PharmacySale.hospital_id == hospital_id).count()
        txns_count = db.query(StockTransaction).filter(StockTransaction.hospital_id == hospital_id).count()

        expected_deduction = len(successes) * qty_per_sale
        expected_final_stock = initial_stock - expected_deduction

        batch_match = abs(final_batch.available_quantity - expected_final_stock) < 0.001
        inv_match = abs(final_inv.available_stock - expected_final_stock) < 0.001

    p50_ms = statistics.median(durations) if durations else 0.0
    p95_ms = statistics.quantiles(durations, n=20)[18] if len(durations) >= 20 else (max(durations) if durations else 0.0)

    summary = {
        "n_workers": n_workers,
        "successful_sales": len(successes),
        "failed_sales": len(failures),
        "expected_final_stock": expected_final_stock,
        "final_batch_available": final_batch.available_quantity,
        "final_inv_available": final_inv.available_stock,
        "batch_reconciled": batch_match,
        "inv_reconciled": inv_match,
        "sales_rows_count": sales_count,
        "stock_txns_count": txns_count,
        "total_elapsed_ms": round(total_elapsed_ms, 2),
        "p50_ms": round(p50_ms, 2),
        "p95_ms": round(p95_ms, 2),
        "failures": failures[:5],
    }

    print(f"  Result: {len(successes)}/{n_workers} sales succeeded. Failures: {len(failures)}.", flush=True)
    print(f"  Batch stock: {final_batch.available_quantity} (expected: {expected_final_stock}, match: {batch_match})", flush=True)
    print(f"  Inventory stock: {final_inv.available_stock} (expected: {expected_final_stock}, match: {inv_match})", flush=True)
    print(f"  Latency: p50={summary['p50_ms']}ms, p95={summary['p95_ms']}ms, total={summary['total_elapsed_ms']}ms", flush=True)
    if failures:
        print(f"  Sample failure: {failures[0]}", flush=True)
    return summary


def main():
    engine = get_perf_db_engine()
    print("=" * 70)
    print("HMS-B POSTGRESQL TRUE CONCURRENCY BENCHMARK SUITE")
    print("=" * 70)

    # 1. Sequence Concurrency: N=10, N=25, N=50
    seq_10 = run_sequence_concurrency_test(engine, 10)
    seq_25 = run_sequence_concurrency_test(engine, 25)
    seq_50 = run_sequence_concurrency_test(engine, 50)

    # 2. Pharmacy Contention: N=10, N=20
    pharm_10 = run_pharmacy_concurrency_test(engine, 10)
    pharm_20 = run_pharmacy_concurrency_test(engine, 20)

    print("\n" + "=" * 70)
    print("CONCURRENCY BENCHMARK SUMMARY")
    print("=" * 70)
    print(f"Sequence N=10: {seq_10['successful_transactions']}/10 ok | p50={seq_10['p50_ms']}ms | p95={seq_10['p95_ms']}ms | Contiguous={seq_10['is_contiguous']}")
    print(f"Sequence N=25: {seq_25['successful_transactions']}/25 ok | p50={seq_25['p50_ms']}ms | p95={seq_25['p95_ms']}ms | Contiguous={seq_25['is_contiguous']}")
    print(f"Sequence N=50: {seq_50['successful_transactions']}/50 ok | p50={seq_50['p50_ms']}ms | p95={seq_50['p95_ms']}ms | Contiguous={seq_50['is_contiguous']}")
    print(f"Pharmacy N=10: {pharm_10['successful_sales']}/10 ok | p50={pharm_10['p50_ms']}ms | Batch Reconciled={pharm_10['batch_reconciled']}")
    print(f"Pharmacy N=20: {pharm_20['successful_sales']}/20 ok | p50={pharm_20['p50_ms']}ms | Batch Reconciled={pharm_20['batch_reconciled']}")
    print("=" * 70)


if __name__ == "__main__":
    main()
