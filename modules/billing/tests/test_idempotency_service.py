"""Tests for DB-backed IdempotencyService (Phase 7, XMOD-015)."""

from uuid import uuid4
import pytest
from sqlalchemy.orm import Session

from modules.billing.services.idempotency_service import IdempotencyService
from shared.exceptions.base import ConflictError
from tests.conftest import db_session, hospital


def test_idempotent_execution_executes_once_and_caches_result(db_session: Session, hospital):
    svc = IdempotencyService(db_session)
    call_count = 0

    def business_op():
        nonlocal call_count
        call_count += 1
        return 201, {"payment_id": "PAY-100", "status": "collected"}

    key = f"IDEMP-{uuid4().hex[:8]}"
    payload = {"amount": 5000, "patient_id": "PAT-1"}

    # First call
    code1, res1 = svc.execute_idempotent(hospital.id, "payments", key, payload, business_op)
    assert code1 == 201
    assert res1["payment_id"] == "PAY-100"
    assert call_count == 1
    db_session.commit()

    # Second call with same key and same payload
    code2, res2 = svc.execute_idempotent(hospital.id, "payments", key, payload, business_op)
    assert code2 == 201
    assert res2["payment_id"] == "PAY-100"
    # Business logic must NOT have been called a second time
    assert call_count == 1


def test_idempotent_execution_rejects_payload_mismatch(db_session: Session, hospital):
    svc = IdempotencyService(db_session)

    key = f"IDEMP-{uuid4().hex[:8]}"
    payload1 = {"amount": 5000, "method": "cash"}
    payload2 = {"amount": 9000, "method": "card"}  # Tampered or conflicting

    # Record first
    svc.execute_idempotent(
        hospital.id,
        "payments",
        key,
        payload1,
        lambda: (200, {"status": "ok"}),
    )
    db_session.commit()

    # Second call with changed parameters must raise ConflictError
    with pytest.raises(ConflictError, match="Idempotency conflict"):
        svc.execute_idempotent(
            hospital.id,
            "payments",
            key,
            payload2,
            lambda: (200, {"status": "tampered"}),
        )


def test_missing_or_blank_idempotency_key_runs_directly(db_session: Session, hospital):
    svc = IdempotencyService(db_session)
    call_count = 0

    def business_op():
        nonlocal call_count
        call_count += 1
        return 200, {"exec_count": call_count}

    code1, res1 = svc.execute_idempotent(hospital.id, "payments", None, {"a": 1}, business_op)
    assert res1["exec_count"] == 1

    code2, res2 = svc.execute_idempotent(hospital.id, "payments", "   ", {"a": 1}, business_op)
    assert res2["exec_count"] == 2
    assert call_count == 2
