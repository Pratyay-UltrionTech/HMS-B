"""
Real Concurrent PostgreSQL Idempotency Tests.

Guarantees:
1. Connects directly to the live PostgreSQL development/test database.
2. Uses TWO completely independent SQLAlchemy Sessions and OS threads.
3. Synchronizes thread entry via threading.Barrier(2) to produce a genuine simultaneous race.
4. Validates that winning transaction executes business mutations and losing transaction
   seamlessly replays canonical appointment and receipt identifiers without slot conflict errors.
5. Verifies exact database counts:
   - Appointments: 1
   - Billing Charges: 1
   - Billing Payments: 1
   - Payment Allocations: 1
   - Billing Receipts: 1
   - Idempotency Records: 1
6. Verifies conflicting payload race returns IDEMPOTENCY_CONFLICT (HTTP 409) with 0 duplicate transactions.
7. Verifies rollback race allows unblocking and successful acquisition.
"""

from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
import threading
import time
from uuid import UUID, uuid4

import pytest
from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.main  # Ensure all SQLAlchemy models and tables are registered
from modules.appointments.actions.book_appointment_action import BookAppointmentAction
from modules.appointments.contracts.appointments_contracts import BookAppointmentRequest
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.db.availability_reader import AvailabilityReader
from modules.appointments.db.pricing_reader import PricingReader
from modules.appointments.entities.appointment import Appointment
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingPayment,
    BillingPaymentAllocation,
    BillingReceipt,
)
from modules.billing.entities.idempotency import IdempotencyRecord
from modules.billing.services.idempotency_service import IdempotencyService
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from shared.exceptions.base import ConflictError


def get_pg_engine():
    """Create dedicated PostgreSQL engine directly from .env configuration."""
    env_path = Path(__file__).resolve().parents[3] / ".env"
    env_vals = dotenv_values(env_path) if env_path.exists() else {}
    raw_url = (
        env_vals.get("DATABASE_URL")
        or "postgresql+psycopg://carwash_admin_dev:Postgre%402026@postgresql-db-dev-1.postgres.database.azure.com:5432/HMSstage2?sslmode=require"
    )

    # Ensure psycopg (psycopg3) dialect is used
    if raw_url.startswith("postgresql://"):
        pg_url = raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif raw_url.startswith("postgresql+psycopg2://"):
        pg_url = raw_url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    else:
        pg_url = raw_url

    return create_engine(pg_url, pool_pre_ping=True)


@pytest.fixture(scope="module")
def pg_session_factory():
    """Module-level sessionmaker bound to real PostgreSQL database."""
    from sqlalchemy import text

    engine = get_pg_engine()
    # Test connection
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL database unavailable: {exc}")
    return sessionmaker(bind=engine)


@pytest.fixture(scope="module")
def pg_fixtures(pg_session_factory):
    """Ensure hospital, active doctor with consultation fee, and valid patient exist in PostgreSQL."""
    session = pg_session_factory()
    try:
        # 1. Hospital
        hosp = session.query(Hospital).first()
        if not hosp:
            hosp = Hospital(name="PG Concurrency Test Hosp", domain_subdomain=f"pg{uuid4().hex[:6]}")
            session.add(hosp)
            session.commit()
        hospital_id = hosp.id

        # 2. Existing Doctor or Staff in hospital with consultation fee
        doc = session.query(HospitalUser).filter_by(hospital_id=hospital_id, is_active=True).first()
        if not doc:
            role = session.query(StaffRole).filter_by(hospital_id=hospital_id).first()
            if not role:
                role = StaffRole(hospital_id=hospital_id, name="Doctor", code="DOC")
                session.add(role)
                session.commit()
            doc = HospitalUser(
                hospital_id=hospital_id,
                role_id=role.id,
                name=f"Dr. PG Race {uuid4().hex[:6]}",
                email=f"pgdoc_{uuid4().hex[:8]}@test.com",
                phone="9876543210",
                password_hash="mock_hash",
                is_active=True,
                custom_values={"consultation_fee": 500.0},
            )
            session.add(doc)
            session.commit()
        else:
            cv = dict(doc.custom_values or {})
            cv["consultation_fee"] = 500.0
            doc.custom_values = cv
            session.commit()
        doctor_id = doc.id

        # 3. Patient with non-null UHID (fresh patient per test session)
        pat = Patient(
            hospital_id=hospital_id,
            uhid=f"UHID-{uuid4().hex[:8].upper()}",
            first_name="Postgres",
            last_name="Concurrent",
            name="Postgres Concurrent",
            mobile=f"9{uuid4().int % 1000000000:09d}",
            gender="male",
        )
        session.add(pat)
        session.commit()
        patient_id = pat.id

        return hospital_id, doctor_id, patient_id
    finally:
        session.close()


def test_real_concurrent_idempotency_claim_or_replay_race(pg_session_factory, pg_fixtures):
    """
    True simultaneous concurrency race test:
    Two threads, two independent sessions, synchronized by a barrier.
    Both submit the same booking payload with the same Idempotency-Key.
    Asserts:
    1. Winning thread commits and losing thread replays canonical response.
    2. Both callers receive identical appointment_id, receipt_id, and receipt_number.
    3. Neither receives slot conflict errors.
    4. Exact database counts:
       - Appointments: 1
       - Billing Charges: 1
       - Billing Payments: 1
       - Payment Allocations: 1
       - Billing Receipts: 1
       - Idempotency Records: 1
    """
    hospital_id, doctor_id, patient_id = pg_fixtures
    appt_date = date.today() + timedelta(days=500 + (uuid4().int % 200))
    appt_time = dt_time(10, 30)
    idemp_key = f"pg_true_race_{uuid4().hex[:12]}"

    user = {
        "sub": str(uuid4()),
        "name": "Race Tester",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital_id),
    }

    # Clean any pre-existing appointments for this doctor on appt_date
    clean_s = pg_session_factory()
    try:
        clean_s.query(Appointment).filter_by(
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            appointment_date=appt_date,
        ).delete()
        clean_s.commit()
    finally:
        clean_s.close()

    payload = BookAppointmentRequest(
        patient_id=patient_id,
        doctor_id=doctor_id,
        appointment_date=appt_date,
        appointment_time=appt_time,
        booking_kind="walk_in",
        purpose="Concurrent Idempotency Verification",
        payment_method="cash",
    )

    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def worker(worker_name: str):
        session: Session = pg_session_factory()
        try:
            repo = AppointmentsRepository(session, hospital_id)
            avail = AvailabilityReader(session, hospital_id)
            pricing = PricingReader(session, hospital_id)
            action = BookAppointmentAction(session, hospital_id, repo, avail, pricing)

            # Synchronize both threads right before execute()
            barrier.wait()
            res = action.execute(payload, user, idempotency_key=idemp_key)
            results[worker_name] = res
        except Exception as exc:
            errors[worker_name] = exc
            session.rollback()
        finally:
            session.close()

    t1 = threading.Thread(target=worker, args=("thread_a",), name="Thread-A")
    t2 = threading.Thread(target=worker, args=("thread_b",), name="Thread-B")

    t1.start()
    t2.start()
    t1.join(timeout=60.0)
    t2.join(timeout=60.0)

    # 1. Assert no unexpected errors occurred
    assert not errors, f"Unexpected errors in concurrent execution: {errors}"
    assert "thread_a" in results, "Thread A did not complete"
    assert "thread_b" in results, "Thread B did not complete"

    res_a = results["thread_a"]
    res_b = results["thread_b"]

    # 2. Both callers receive the exact same canonical IDs
    assert res_a.id == res_b.id, f"Appointment IDs differ: {res_a.id} vs {res_b.id}"
    assert res_a.op_id == res_b.op_id, f"OP IDs differ: {res_a.op_id} vs {res_b.op_id}"
    assert res_a.receipt_id == res_b.receipt_id, f"Receipt IDs differ: {res_a.receipt_id} vs {res_b.receipt_id}"
    assert res_a.receipt_number == res_b.receipt_number, f"Receipt numbers differ: {res_a.receipt_number} vs {res_b.receipt_number}"
    assert res_a.receipt_number is not None, "Receipt number was not generated"

    # 3. Query using a completely independent 3rd session to verify exact database counts
    verification_session: Session = pg_session_factory()
    try:
        # Appointments
        appts = (
            verification_session.query(Appointment)
            .filter_by(
                hospital_id=hospital_id,
                doctor_id=doctor_id,
                appointment_date=appt_date,
                appointment_time=appt_time,
            )
            .all()
        )
        assert len(appts) == 1, f"Expected exactly 1 Appointment, found {len(appts)}"
        the_appt = appts[0]

        # Billing Charges
        charges = (
            verification_session.query(BillingCharge)
            .filter_by(hospital_id=hospital_id, source_id=the_appt.id)
            .all()
        )
        assert len(charges) == 1, f"Expected exactly 1 BillingCharge, found {len(charges)}"
        the_charge = charges[0]

        # Payment Allocations
        allocations = (
            verification_session.query(BillingPaymentAllocation)
            .filter_by(charge_id=the_charge.id)
            .all()
        )
        assert len(allocations) == 1, f"Expected exactly 1 PaymentAllocation, found {len(allocations)}"

        # Billing Payments
        payments = (
            verification_session.query(BillingPayment)
            .filter_by(hospital_id=hospital_id)
            .filter(BillingPayment.notes.like(f"%{the_appt.op_id}%"))
            .all()
        )
        assert len(payments) == 1, f"Expected exactly 1 BillingPayment, found {len(payments)}"
        the_payment = payments[0]

        # Billing Receipts
        receipts = (
            verification_session.query(BillingReceipt)
            .filter_by(hospital_id=hospital_id, receipt_number=res_a.receipt_number)
            .all()
        )
        assert len(receipts) == 1, f"Expected exactly 1 BillingReceipt, found {len(receipts)}"
        the_receipt = receipts[0]

        # Idempotency Records
        idemp_recs = (
            verification_session.query(IdempotencyRecord)
            .filter_by(hospital_id=hospital_id, idempotency_key=idemp_key)
            .all()
        )
        assert len(idemp_recs) == 1, f"Expected exactly 1 IdempotencyRecord, found {len(idemp_recs)}"

        # Verify cross-entity linkage: charge, payment, receipt belong to the same appointment
        assert the_charge.source_id == the_appt.id
        assert the_receipt.payment_id == the_payment.id
        assert the_receipt.id == res_a.receipt_id
    finally:
        verification_session.close()


def test_concurrent_idempotency_differing_payload_conflict(pg_session_factory, pg_fixtures):
    """
    Race with same Idempotency-Key but differing payload hashes.
    Expected:
    - Exactly one request wins and performs booking.
    - The other request resolves to ConflictError (HTTP 409 / IDEMPOTENCY_CONFLICT).
    - Database contains exactly 1 appointment and 1 charge; never a second financial transaction.
    """
    hospital_id, doctor_id, patient_id = pg_fixtures
    appt_date = date.today() + timedelta(days=700 + (uuid4().int % 200))
    idemp_key = f"pg_mismatch_race_{uuid4().hex[:12]}"

    user = {
        "sub": str(uuid4()),
        "name": "Race Tester",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital_id),
    }

    # Clean any pre-existing appointments for this doctor on appt_date
    clean_s = pg_session_factory()
    try:
        clean_s.query(Appointment).filter_by(
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            appointment_date=appt_date,
        ).delete()
        clean_s.commit()
    finally:
        clean_s.close()

    # Two different payloads (14:00 vs 14:30)
    payload_1 = BookAppointmentRequest(
        patient_id=patient_id,
        doctor_id=doctor_id,
        appointment_date=appt_date,
        appointment_time=dt_time(14, 0),
        booking_kind="walk_in",
        purpose="Payload 1",
        payment_method="cash",
    )
    payload_2 = BookAppointmentRequest(
        patient_id=patient_id,
        doctor_id=doctor_id,
        appointment_date=appt_date,
        appointment_time=dt_time(14, 30),
        booking_kind="walk_in",
        purpose="Payload 2",
        payment_method="cash",
    )

    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def worker(worker_name: str, p: BookAppointmentRequest):
        session: Session = pg_session_factory()
        try:
            repo = AppointmentsRepository(session, hospital_id)
            avail = AvailabilityReader(session, hospital_id)
            pricing = PricingReader(session, hospital_id)
            action = BookAppointmentAction(session, hospital_id, repo, avail, pricing)

            barrier.wait()
            res = action.execute(p, user, idempotency_key=idemp_key)
            results[worker_name] = res
        except Exception as exc:
            errors[worker_name] = exc
            session.rollback()
        finally:
            session.close()

    t1 = threading.Thread(target=worker, args=("worker_1", payload_1))
    t2 = threading.Thread(target=worker, args=("worker_2", payload_2))

    t1.start()
    t2.start()
    t1.join(timeout=60.0)
    t2.join(timeout=60.0)

    # Exactly one succeeded and one failed with ConflictError
    assert len(results) == 1, f"Expected exactly 1 success, got {len(results)}"
    assert len(errors) == 1, f"Expected exactly 1 error, got {len(errors)}"

    err = list(errors.values())[0]
    assert isinstance(err, ConflictError), f"Expected ConflictError, got {type(err)}: {err}"
    assert getattr(err, "code", None) == "IDEMPOTENCY_CONFLICT" or "Idempotency conflict" in str(err)

    # In 3rd session, verify only 1 appointment and 1 charge created
    verification_session: Session = pg_session_factory()
    try:
        winning_res = list(results.values())[0]
        appts = (
            verification_session.query(Appointment)
            .filter_by(hospital_id=hospital_id, doctor_id=doctor_id, appointment_date=appt_date)
            .all()
        )
        assert len(appts) == 1, f"Expected exactly 1 appointment for date, found {len(appts)}"
        charges = (
            verification_session.query(BillingCharge)
            .filter_by(hospital_id=hospital_id, source_id=winning_res.id)
            .all()
        )
        assert len(charges) == 1, f"Expected exactly 1 charge, found {len(charges)}"
    finally:
        verification_session.close()


def test_concurrent_idempotency_rollback_race(pg_session_factory, pg_fixtures):
    """
    Rollback race:
    Worker A claims idempotency key, then aborts/rolls back.
    Worker B waits on PostgreSQL row lock / unique constraint.
    When Worker A rolls back, Worker B immediately unblocks, claims the key, and commits.
    """
    hospital_id, doctor_id, patient_id = pg_fixtures
    idemp_key = f"pg_rollback_race_{uuid4().hex[:12]}"
    a_flushed_event = threading.Event()
    events = []

    clean_s = pg_session_factory()
    try:
        clean_s.query(IdempotencyRecord).filter_by(
            hospital_id=hospital_id,
            scope="rollback_scope",
            idempotency_key=idemp_key,
        ).delete()
        clean_s.commit()
    finally:
        clean_s.close()

    def worker_a():
        session: Session = pg_session_factory()
        try:
            rec = IdempotencyRecord(
                hospital_id=hospital_id,
                scope="rollback_scope",
                idempotency_key=idemp_key,
                request_hash="hash_a",
                response_code=0,
                response_payload={},
            )
            session.add(rec)
            session.flush()
            events.append("A_flushed")
            a_flushed_event.set()
            time.sleep(1.0)
            session.rollback()
            events.append("A_rolled_back")
        except Exception as e:
            events.append(f"A_error: {e}")
            session.rollback()
        finally:
            session.close()

    def worker_b():
        session: Session = pg_session_factory()
        try:
            # Wait until Worker A has successfully flushed and holds the row lock in PostgreSQL
            assert a_flushed_event.wait(timeout=15.0), "Worker A did not flush in time"
            rec = IdempotencyRecord(
                hospital_id=hospital_id,
                scope="rollback_scope",
                idempotency_key=idemp_key,
                request_hash="hash_b",
                response_code=201,
                response_payload={"success": True},
            )
            session.add(rec)
            # This flush will block in PostgreSQL on the unique index until Worker A rolls back
            session.flush()
            events.append("B_flushed")
            session.commit()
            events.append("B_committed")
        except Exception as e:
            events.append(f"B_error: {e}")
            session.rollback()
        finally:
            session.close()

    t1 = threading.Thread(target=worker_a, name="Worker-A")
    t2 = threading.Thread(target=worker_b, name="Worker-B")
    t1.start()
    t2.start()
    t1.join(timeout=30.0)
    t2.join(timeout=30.0)

    assert "A_rolled_back" in events, f"Events were: {events}"
    assert "B_committed" in events, f"Events were: {events}"

    verification_session: Session = pg_session_factory()
    try:
        final_rec = (
            verification_session.query(IdempotencyRecord)
            .filter_by(hospital_id=hospital_id, idempotency_key=idemp_key)
            .first()
        )
        assert final_rec is not None
        assert final_rec.request_hash == "hash_b"
        assert final_rec.response_code == 201
    finally:
        verification_session.close()
