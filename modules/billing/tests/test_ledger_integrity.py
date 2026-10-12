"""Ledger integrity (Phase 0): no fake paid, sourced refunds, canonical cancellation.

Money always enters through real payments (payment API / ``create_payment``) or
deposits; charge settlement is derived from the append-only allocation ledger.
"""

from __future__ import annotations

from datetime import date, time
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.orm import Session

from infrastructure.postgres.session import get_transitional_sync_session
from modules.appointments.actions.appointment_status_actions import CancelAppointmentAction
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.entities.enums import AppointmentStatus
from modules.billing.api.billing_api import router as billing_router
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPaymentAllocation,
    BillingPaymentMethod,
    BillingRefund,
    BillingSourceType,
    DepositStatus,
)
from modules.billing.services.billing_service import (
    create_deposit,
    create_payment,
    draw_down_deposit_for_charges,
    ensure_charge,
    patient_ledger_totals,
    payment_balances,
    process_refund,
)
from modules.billing.services.charge_reversal_service import (
    cancel_charge,
    cancel_charges_for_source,
)
from modules.billing.services.service_financial_clearance import check_service_financial_clearance
from shared.audit.entities.audit_log import AuditLog
from shared.exceptions import register_exception_handlers
from shared.exceptions.base import ValidationError
from tests.conftest import (  # noqa: F401  (pytest fixtures)
    admin_headers,
    appointment_factory,
    db_session,
    doctor,
    hospital,
    patient,
)

ADMIN = {"sub": "admin@apollocity.com", "name": "Hospital Administrator", "role": "hospital_admin"}


@pytest.fixture
def client(db_session: Session) -> TestClient:
    app = FastAPI(title="Ledger Integrity Test App")
    register_exception_handlers(app)
    app.include_router(billing_router, prefix="/api")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_transitional_sync_session] = _override_db
    return TestClient(app)


def _charge(db: Session, hospital, patient, amount: float, *, source_type=BillingSourceType.laboratory, source_id=None, account_id=None) -> BillingCharge:
    c = ensure_charge(
        db,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=source_type,
        source_id=source_id if source_id is not None else uuid4(),
        description=f"{source_type.value} service",
        charge_amount=amount,
        account_id=account_id,
    )
    db.commit()
    return c


def _pay(db: Session, hospital, patient, amount: float, plan: list[tuple[BillingCharge, float]] | None = None):
    pay = create_payment(
        db,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=amount,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes=None,
        received_by_name="Cashier",
        allocations_plan=[{"charge_id": c.id, "amount": a} for c, a in plan] if plan else None,
    )
    db.commit()
    return pay


def _alloc_sum(db: Session, charge_id) -> float:
    return round(
        float(
            db.query(func.coalesce(func.sum(BillingPaymentAllocation.allocated_amount), 0))
            .filter(BillingPaymentAllocation.charge_id == charge_id)
            .scalar()
        ),
        2,
    )


def _credit_deposits(db: Session, patient_id) -> list[BillingDeposit]:
    return (
        db.query(BillingDeposit)
        .filter(BillingDeposit.patient_id == patient_id, BillingDeposit.deposit_type == "cancellation_credit")
        .all()
    )


# ── P0.2: no fake paid ──────────────────────────────────────────────────────

def test_update_api_cannot_set_charge_paid(client, db_session, hospital, patient, admin_headers):
    charge = _charge(db_session, hospital, patient, 500.0)

    res = client.put(f"/api/billing/charges/{charge.id}", headers=admin_headers, json={"status": "paid"})
    assert res.status_code == 400

    db_session.refresh(charge)
    assert charge.status == BillingChargeStatus.pending
    assert float(charge.amount_paid) == 0.0
    assert check_service_financial_clearance(
        db_session, hospital.id, charge.source_type, charge.source_id
    ).is_cleared is False


def test_admin_discount_edit_rederives_status_and_releases_excess(client, db_session, hospital, patient, admin_headers):
    charge = _charge(db_session, hospital, patient, 500.0)
    _pay(db_session, hospital, patient, 500.0, [(charge, 500.0)])
    db_session.refresh(charge)
    assert charge.status == BillingChargeStatus.paid

    res = client.put(
        f"/api/billing/charges/{charge.id}",
        headers=admin_headers,
        json={"discount_amount": 100.0, "discount_reason": "Staff concession"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["net_amount"] == 400.0
    assert body["amount_paid"] == 400.0
    assert body["status"] == "paid"

    # Over-applied ₹100 became refundable credit (not silently dropped).
    assert _alloc_sum(db_session, charge.id) == 400.0
    credits = _credit_deposits(db_session, patient.id)
    assert len(credits) == 1 and float(credits[0].available_amount) == 100.0

    # Raising the price again re-derives partially_paid from allocations.
    res2 = client.put(
        f"/api/billing/charges/{charge.id}",
        headers=admin_headers,
        json={"unit_price": 600.0, "quantity": 1, "discount_amount": 0.0},
    )
    assert res2.status_code == 200, res2.text
    assert res2.json()["net_amount"] == 600.0
    assert res2.json()["status"] == "partially_paid"
    assert res2.json()["amount_paid"] == 400.0


# ── P0.3: refund correctness ────────────────────────────────────────────────

def test_refund_without_source_is_rejected(client, db_session, hospital, patient, admin_headers):
    charge = _charge(db_session, hospital, patient, 300.0)
    _pay(db_session, hospital, patient, 300.0, [(charge, 300.0)])

    res = client.post(
        "/api/billing/refunds",
        headers=admin_headers,
        json={"patient_id": str(patient.id), "amount": 100.0, "reason": "Goodwill"},
    )
    assert res.status_code == 400
    assert db_session.query(BillingRefund).count() == 0

    with pytest.raises(ValueError):
        process_refund(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            amount=10.0,
            reason="no source",
            refund_date=date.today(),
        )


def test_full_payment_refund_reverts_charge_clearance_and_invoice(client, db_session, hospital, patient, admin_headers):
    charge = _charge(db_session, hospital, patient, 1000.0)

    inv_res = client.post(
        "/api/billing/invoices",
        headers=admin_headers,
        json={"patient_id": str(patient.id), "charge_ids": [str(charge.id)]},
    )
    assert inv_res.status_code == 201, inv_res.text
    invoice_id = UUID(inv_res.json()["id"])

    pay_res = client.post(
        "/api/billing/payments",
        headers=admin_headers,
        json={
            "patient_id": str(patient.id),
            "amount": 1000.0,
            "payment_method": "cash",
            "allocations": [{"charge_id": str(charge.id), "amount": 1000.0}],
        },
    )
    assert pay_res.status_code == 201, pay_res.text
    payment_id = pay_res.json()["id"]

    db_session.expire_all()
    assert db_session.get(BillingCharge, charge.id).status == BillingChargeStatus.paid
    assert db_session.get(BillingInvoice, invoice_id).status == BillingInvoiceStatus.paid
    assert check_service_financial_clearance(db_session, hospital.id, charge.source_type, charge.source_id).is_cleared

    ref_res = client.post(
        "/api/billing/refunds",
        headers=admin_headers,
        json={"patient_id": str(patient.id), "payment_id": payment_id, "amount": 1000.0, "reason": "Service not rendered"},
    )
    assert ref_res.status_code == 201, ref_res.text

    db_session.expire_all()
    charge = db_session.get(BillingCharge, charge.id)
    assert charge.status == BillingChargeStatus.pending
    assert float(charge.amount_paid) == 0.0
    assert check_service_financial_clearance(
        db_session, hospital.id, charge.source_type, charge.source_id
    ).is_cleared is False
    assert db_session.get(BillingInvoice, invoice_id).status == BillingInvoiceStatus.generated

    # History preserved: original allocation + negative reversal linked to the refund.
    rows = db_session.query(BillingPaymentAllocation).filter(BillingPaymentAllocation.charge_id == charge.id).all()
    assert sorted(float(r.allocated_amount) for r in rows) == [-1000.0, 1000.0]
    reversal = next(r for r in rows if float(r.allocated_amount) < 0)
    assert reversal.reversal_of_id is not None
    assert str(reversal.refund_id) == ref_res.json()["id"]

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_type == "billing_refund", AuditLog.action == "refund")
        .one()
    )
    assert audit.details["patient_id"] == str(patient.id)
    assert "account_id" in audit.details
    assert audit.details["reversed_charge_ids"] == [str(charge.id)]

    # Nothing left to refund.
    again = client.post(
        "/api/billing/refunds",
        headers=admin_headers,
        json={"patient_id": str(patient.id), "payment_id": payment_id, "amount": 1.0, "reason": "again"},
    )
    assert again.status_code == 400


def test_partial_refund_math_uses_unallocated_first(db_session, hospital, patient):
    charge = _charge(db_session, hospital, patient, 1000.0)
    pay = _pay(db_session, hospital, patient, 1200.0, [(charge, 1000.0)])
    bal = payment_balances(db_session, pay)
    assert bal["allocated"] == 1000.0 and bal["unallocated"] == 200.0

    process_refund(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=300.0,
        reason="Partial refund",
        refund_date=date.today(),
        payment_id=pay.id,
    )
    db_session.commit()
    db_session.refresh(charge)

    # ₹200 came from the unallocated remainder, only ₹100 reversed off the charge.
    assert float(charge.amount_paid) == 900.0
    assert charge.status == BillingChargeStatus.partially_paid
    assert _alloc_sum(db_session, charge.id) == 900.0
    bal = payment_balances(db_session, pay)
    assert bal == {
        "amount": 1200.0,
        "allocated": 900.0,
        "refunded": 300.0,
        "transferred_to_credit": 0.0,
        "unallocated": 0.0,
        "refundable": 900.0,
    }

    with pytest.raises(ValueError):
        process_refund(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            amount=900.01,
            reason="Too much",
            refund_date=date.today(),
            payment_id=pay.id,
        )


def test_deposit_refund_only_from_available_balance(db_session, hospital, patient):
    dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=1000.0,
        deposit_date=date.today(),
        deposit_type="advance",
    )
    charge = _charge(db_session, hospital, patient, 400.0)
    draw_down_deposit_for_charges(
        db_session,
        hospital_id=hospital.id,
        deposit_id=dep.id,
        allocations_plan=[{"charge_id": charge.id, "amount": 400.0}],
    )
    db_session.commit()

    with pytest.raises(ValueError):
        process_refund(
            db_session, hospital_id=hospital.id, patient_id=patient.id, amount=600.01,
            reason="over", refund_date=date.today(), deposit_id=dep.id,
        )

    process_refund(
        db_session, hospital_id=hospital.id, patient_id=patient.id, amount=600.0,
        reason="balance", refund_date=date.today(), deposit_id=dep.id,
    )
    db_session.commit()
    db_session.refresh(dep)
    db_session.refresh(charge)
    assert float(dep.available_amount) == 0.0
    assert dep.status == DepositStatus.exhausted
    assert charge.status == BillingChargeStatus.paid  # deposit-funded service untouched


# ── P0.7: canonical cancellation ────────────────────────────────────────────

def test_cancel_paid_charge_via_module_path_reverses_and_credits_once(db_session, hospital, patient, appointment_factory):
    appt = appointment_factory(status=AppointmentStatus.scheduled, appointment_time=time(10, 0))
    charge = _charge(db_session, hospital, patient, 500.0, source_type=BillingSourceType.consultation, source_id=appt.id)
    pay = _pay(db_session, hospital, patient, 500.0, [(charge, 500.0)])
    db_session.refresh(charge)
    assert charge.status == BillingChargeStatus.paid

    CancelAppointmentAction(db_session, hospital.id, AppointmentsRepository(db_session, hospital.id)).execute(
        appt.id, "Patient request", ADMIN
    )
    db_session.expire_all()
    charge = db_session.get(BillingCharge, charge.id)
    assert charge.status == BillingChargeStatus.cancelled
    assert float(charge.amount_paid) == 0.0
    assert _alloc_sum(db_session, charge.id) == 0.0

    credits = _credit_deposits(db_session, patient.id)
    assert len(credits) == 1
    assert float(credits[0].available_amount) == 500.0

    # The payment is no longer independently refundable — the credit is (no double count).
    bal = payment_balances(db_session, pay)
    assert bal["transferred_to_credit"] == 500.0
    assert bal["refundable"] == 0.0
    assert bal["unallocated"] == 0.0

    totals = patient_ledger_totals(db_session, hospital.id, patient.id)
    assert totals["outstanding"] == 0.0
    assert totals["total_deposits_available"] == 500.0

    # Re-cancelling is idempotent: no second credit.
    cancel_charges_for_source(db_session, hospital.id, BillingSourceType.consultation, appt.id, reason="again")
    db_session.commit()
    assert len(_credit_deposits(db_session, patient.id)) == 1

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_type == "billing_charge", AuditLog.action == "cancel")
        .one()
    )
    for key in ("patient_id", "account_id", "admission_id", "source_type", "source_id", "amount_reversed", "credit_deposit_id"):
        assert key in audit.details
    assert audit.details["amount_reversed"] == 500.0
    assert audit.details["credit_deposit_id"] == str(credits[0].id)

    # The credit is refundable through the normal deposit path.
    process_refund(
        db_session, hospital_id=hospital.id, patient_id=patient.id, amount=500.0,
        reason="Return credit", refund_date=date.today(), deposit_id=credits[0].id,
    )
    db_session.commit()
    db_session.refresh(credits[0])
    assert credits[0].status == DepositStatus.refunded


def test_billing_desk_cancel_uses_canonical_service(client, db_session, hospital, patient, admin_headers):
    paid_charge = _charge(db_session, hospital, patient, 800.0, source_type=BillingSourceType.other)
    _pay(db_session, hospital, patient, 800.0, [(paid_charge, 800.0)])

    res = client.post(f"/api/billing/charges/{paid_charge.id}/cancel", headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "cancelled"
    assert res.json()["amount_paid"] == 0.0

    rows = db_session.query(BillingPaymentAllocation).filter(BillingPaymentAllocation.charge_id == paid_charge.id).all()
    reversal = next(r for r in rows if float(r.allocated_amount) < 0)
    credits = _credit_deposits(db_session, patient.id)
    assert len(credits) == 1 and float(credits[0].available_amount) == 800.0
    assert reversal.credit_deposit_id == credits[0].id

    # Deposit-funded charge: money returns to the same deposit, no credit deposit.
    dep = create_deposit(
        db_session, hospital_id=hospital.id, patient_id=patient.id, amount=1000.0,
        deposit_date=date.today(), deposit_type="advance",
    )
    dep_charge = _charge(db_session, hospital, patient, 300.0, source_type=BillingSourceType.other)
    draw_down_deposit_for_charges(
        db_session, hospital_id=hospital.id, deposit_id=dep.id,
        allocations_plan=[{"charge_id": dep_charge.id, "amount": 300.0}],
    )
    db_session.commit()
    db_session.refresh(dep)
    assert float(dep.available_amount) == 700.0

    res2 = client.post(f"/api/billing/charges/{dep_charge.id}/cancel", headers=admin_headers)
    assert res2.status_code == 200, res2.text
    db_session.expire_all()
    dep = db_session.get(BillingDeposit, dep.id)
    assert float(dep.available_amount) == 1000.0
    assert dep.status == DepositStatus.available
    assert len(_credit_deposits(db_session, patient.id)) == 1

    # Another hospital's / unknown charge stays 404.
    assert client.post(f"/api/billing/charges/{uuid4()}/cancel", headers=admin_headers).status_code == 404


def test_cancel_performed_service_is_refused(client, db_session, hospital, patient, admin_headers, appointment_factory):
    appt = appointment_factory(status=AppointmentStatus.completed, appointment_time=time(11, 0))
    charge = _charge(db_session, hospital, patient, 500.0, source_type=BillingSourceType.consultation, source_id=appt.id)
    _pay(db_session, hospital, patient, 500.0, [(charge, 500.0)])

    res = client.post(f"/api/billing/charges/{charge.id}/cancel", headers=admin_headers)
    assert res.status_code == 400

    db_session.expire_all()
    charge = db_session.get(BillingCharge, charge.id)
    assert charge.status == BillingChargeStatus.paid
    assert float(charge.amount_paid) == 500.0
    assert _credit_deposits(db_session, patient.id) == []

    with pytest.raises(ValidationError):
        cancel_charge(db_session, charge, reason="direct", performed=True)
