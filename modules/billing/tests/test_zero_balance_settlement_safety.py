"""
Tests for Billing zero-balance settlement safety and overpayment protection (XMOD-007).

Verifies:
1. Collecting payment on a fully settled invoice (balance_due == 0) is rejected with 400.
2. Attempting to pay an amount greater than the invoice's remaining balance is rejected with 400.
"""

from datetime import date
from uuid import uuid4
import pytest
from fastapi import HTTPException

from tests.conftest import db_session, doctor, hospital, patient
from modules.billing.actions.billing_actions import CreateInvoiceAction, CreatePaymentAction
from modules.billing.contracts.billing_contracts import (
    BillingInvoiceCreate,
    BillingPaymentCreate,
)
from modules.billing.entities.billing_entities import (
    BillingInvoice,
    BillingPayment,
    BillingPaymentMethod,
    BillingSourceType,
)
from modules.billing.services.billing_service import ensure_charge


def test_payment_on_zero_balance_invoice_rejected(db_session, hospital, doctor, patient):
    """An invoice with zero balance due must reject further payment attempts with HTTP 400."""
    # 1. Create a charge of 500
    charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid4(),
        description="Consultation Fee",
        charge_amount=500.0,
    )
    db_session.flush()

    # 2. Create invoice for the charge
    inv_action = CreateInvoiceAction(db_session, hospital.id)
    actor = {"user_id": str(doctor.id), "name": "Cashier", "role": "receptionist"}
    inv = inv_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=charge.account_id,
            charge_ids=[charge.id],
        ),
        user=actor,
    )
    db_session.commit()

    # 3. Pay full 500
    pay_action = CreatePaymentAction(db_session, hospital.id)
    pay_payload = BillingPaymentCreate(
        patient_id=patient.id,
        amount=500.0,
        payment_method=BillingPaymentMethod.cash,
        linked_invoice_id=inv.id,
    )
    resp = pay_action.execute(pay_payload, actor)
    assert resp.amount == 500.0

    # 4. Attempt second payment against the same invoice -> must be rejected
    with pytest.raises(HTTPException) as exc_info:
        pay_action.execute(pay_payload, actor)

    assert exc_info.value.status_code == 400
    assert "Invoice is already fully settled; balance due is zero" in str(exc_info.value.detail)


def test_overpayment_on_invoice_rejected(db_session, hospital, doctor, patient):
    """Paying more than the balance due on an invoice must be rejected with HTTP 400."""
    charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid4(),
        description="Consultation Fee",
        charge_amount=300.0,
    )
    db_session.flush()

    inv_action = CreateInvoiceAction(db_session, hospital.id)
    actor = {"user_id": str(doctor.id), "name": "Cashier", "role": "receptionist"}
    inv = inv_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=charge.account_id,
            charge_ids=[charge.id],
        ),
        user=actor,
    )
    db_session.commit()

    pay_action = CreatePaymentAction(db_session, hospital.id)

    # Attempt to pay 500 on a 300 invoice
    pay_payload = BillingPaymentCreate(
        patient_id=patient.id,
        amount=500.0,
        payment_method=BillingPaymentMethod.cash,
        linked_invoice_id=inv.id,
    )

    with pytest.raises(HTTPException) as exc_info:
        pay_action.execute(pay_payload, actor)

    assert exc_info.value.status_code == 400
    assert "exceeds invoice balance due" in str(exc_info.value.detail)
