"""Tests for centralized automatic eligible deposit application across hospital departments."""

from datetime import datetime, timezone
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingPaymentMethod,
    BillingSourceType,
    DepositStatus,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
)
from modules.billing.services.billing_service import (
    auto_apply_eligible_deposits_to_charge,
    draw_down_account_deposits_for_charge,
    ensure_charge,
)
from modules.billing.services.service_financial_clearance import (
    assert_service_financially_cleared,
)
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderStatus,
    LabOrderSource,
    LabSampleType,
)
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from tests.conftest import db_session, hospital, patient


@pytest.fixture
def admission_account_with_deposit(db_session: Session, hospital: Hospital, patient: Patient):
    """Sets up an IPD admission, financial account, and ₹10,000 advance deposit."""
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-DEP-001",
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-DEP001",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    dep = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        deposit_number="DEP-0001",
        deposit_date=datetime.now(timezone.utc).date(),
        deposit_type="ipd_advance",
        payment_method=BillingPaymentMethod.cash,
        original_amount=10000.0,
        available_amount=10000.0,
        status=DepositStatus.available,
        received_by_name="Cashier",
    )
    db_session.add(dep)
    db_session.commit()
    return adm, acc, dep


def test_lab_charge_auto_uses_eligible_deposit(
    db_session: Session, hospital: Hospital, patient: Patient, admission_account_with_deposit
):
    adm, acc, dep = admission_account_with_deposit

    charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="IPD Complete Blood Count",
        charge_amount=2000.0,
    )
    db_session.commit()

    allocated = auto_apply_eligible_deposits_to_charge(
        db_session, hospital_id=hospital.id, charge=charge
    )
    db_session.commit()

    assert allocated == 2000.0
    db_session.refresh(charge)
    db_session.refresh(dep)
    assert charge.amount_paid == 2000.0
    assert charge.status == BillingChargeStatus.paid
    assert dep.available_amount == 8000.0
    assert round(dep.original_amount - dep.available_amount, 2) == 2000.0

    # Clearance assertion succeeds
    state = assert_service_financially_cleared(
        db_session,
        hospital.id,
        BillingSourceType.laboratory,
        charge.source_id,
        admission_id=adm.id,
        patient_id=patient.id,
    )
    assert state.is_cleared is True


def test_radiology_charge_auto_uses_eligible_deposit_at_gate(
    db_session: Session, hospital: Hospital, patient: Patient, admission_account_with_deposit
):
    adm, acc, dep = admission_account_with_deposit

    charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.radiology,
        source_id=uuid4(),
        description="IPD Chest X-Ray",
        charge_amount=3000.0,
    )
    db_session.commit()

    # Even without explicit auto_apply before the gate, assert_service_financially_cleared
    # automatically draws down eligible account deposits before blocking
    state = assert_service_financially_cleared(
        db_session,
        hospital.id,
        BillingSourceType.radiology,
        charge.source_id,
        admission_id=adm.id,
        patient_id=patient.id,
    )
    assert state.is_cleared is True

    db_session.refresh(charge)
    db_session.refresh(dep)
    assert charge.amount_paid == 3000.0
    assert charge.status == BillingChargeStatus.paid
    assert dep.available_amount == 7000.0


def test_ot_charge_auto_uses_eligible_deposit(
    db_session: Session, hospital: Hospital, patient: Patient, admission_account_with_deposit
):
    adm, acc, dep = admission_account_with_deposit

    charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.ot,
        source_id=uuid4(),
        description="IPD Appendectomy Procedure",
        charge_amount=5000.0,
    )
    db_session.commit()

    state = assert_service_financially_cleared(
        db_session,
        hospital.id,
        BillingSourceType.ot,
        charge.source_id,
        admission_id=adm.id,
        patient_id=patient.id,
    )
    assert state.is_cleared is True

    db_session.refresh(charge)
    db_session.refresh(dep)
    assert charge.amount_paid == 5000.0
    assert charge.status == BillingChargeStatus.paid
    assert dep.available_amount == 5000.0


def test_insufficient_deposit_partially_applies_and_blocks_gate(
    db_session: Session, hospital: Hospital, patient: Patient, admission_account_with_deposit
):
    adm, acc, dep = admission_account_with_deposit

    # Large charge exceeding deposit balance of 10,000
    charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Complex Genome Sequencing",
        charge_amount=15000.0,
    )
    db_session.commit()

    # assert_service_financially_cleared must draw down the 10,000 available
    # but still block with 402 because 5,000 remains outstanding
    with pytest.raises(HTTPException) as exc_info:
        assert_service_financially_cleared(
            db_session,
            hospital.id,
            BillingSourceType.laboratory,
            charge.source_id,
            admission_id=adm.id,
            patient_id=patient.id,
        )
    assert exc_info.value.status_code == 402

    db_session.refresh(charge)
    db_session.refresh(dep)
    assert charge.amount_paid == 10000.0
    assert charge.status == BillingChargeStatus.partially_paid
    assert dep.available_amount == 0.0
    assert dep.status == DepositStatus.exhausted
