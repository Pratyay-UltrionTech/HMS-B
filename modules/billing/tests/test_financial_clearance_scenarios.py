"""
Explicit unit tests for financial clearance scenarios (Fix 11).

Verifies authoritative evaluation in FinancialClearanceService:
Scenario 1: 1000 billed + 1000 deposit -> cleared
Scenario 2: 1000 billed + 400 deposit -> 600 due
Scenario 3: 1000 billed + 600 unallocated + 400 deposit -> cleared
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingPayment,
    BillingPaymentMethod,
    BillingSourceType,
    DepositStatus,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
)
from modules.billing.services.financial_clearance_service import FinancialClearanceService
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.patients.entities.patient import Patient, PatientStatus
from tests.conftest import db_session, hospital


def _setup_admission_and_account(db_session: Session, hospital):
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid=f"UHID-{uuid4().hex[:6].upper()}",
        first_name="Clearance",
        last_name="Tester",
        name="Clearance Tester",
        gender="female",
        mobile="9876543211",
        status=PatientStatus.admitted,
    )
    db_session.add(patient)

    admission = Admission(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)

    account = FinancialAccount(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        admission_id=admission.id,
        account_number=f"IPD-ACC-{uuid4().hex[:6].upper()}",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        created_by_name="System",
    )
    db_session.add(account)
    db_session.commit()
    return patient, admission, account


def test_scenario_1_1000_billed_and_1000_deposit_cleared(db_session: Session, hospital):
    """Scenario 1: 1000 billed + 1000 deposit -> cleared."""
    patient, admission, account = _setup_admission_and_account(db_session, hospital)

    # 1000 billed
    charge = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        source_type=BillingSourceType.bed,
        source_id=admission.id,
        description="Bed charges",
        quantity=1.0,
        unit_price=1000.0,
        charge_amount=Decimal("1000.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        net_amount=Decimal("1000.00"),
        amount_paid=Decimal("0.00"),
        status=BillingChargeStatus.pending,
        created_by_name="Admin",
    )
    db_session.add(charge)

    # 1000 deposit
    deposit = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        admission_id=admission.id,
        deposit_number=f"DEP-{uuid4().hex[:6].upper()}",
        deposit_type="admission",
        original_amount=Decimal("1000.00"),
        available_amount=Decimal("1000.00"),
        status=DepositStatus.available,
        payment_method=BillingPaymentMethod.cash,
        deposit_date=date.today(),
        received_by_name="Admin",
    )
    db_session.add(deposit)
    db_session.commit()

    service = FinancialClearanceService(db_session)
    result = service.evaluate_discharge_clearance(hospital.id, admission.id, patient.id)

    assert result.can_discharge is True
    assert result.status == "cleared"
    assert result.outstanding_amount == 0.0
    assert result.total_charges == 1000.0
    assert result.total_deposits_available == 1000.0


def test_scenario_2_1000_billed_and_400_deposit_600_due(db_session: Session, hospital):
    """Scenario 2: 1000 billed + 400 deposit -> 600 due."""
    patient, admission, account = _setup_admission_and_account(db_session, hospital)

    # 1000 billed
    charge = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        source_type=BillingSourceType.bed,
        source_id=admission.id,
        description="Bed charges",
        quantity=1.0,
        unit_price=1000.0,
        charge_amount=Decimal("1000.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        net_amount=Decimal("1000.00"),
        amount_paid=Decimal("0.00"),
        status=BillingChargeStatus.pending,
        created_by_name="Admin",
    )
    db_session.add(charge)

    # 400 deposit
    deposit = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        admission_id=admission.id,
        deposit_number=f"DEP-{uuid4().hex[:6].upper()}",
        deposit_type="admission",
        original_amount=Decimal("400.00"),
        available_amount=Decimal("400.00"),
        status=DepositStatus.available,
        payment_method=BillingPaymentMethod.cash,
        deposit_date=date.today(),
        received_by_name="Admin",
    )
    db_session.add(deposit)
    db_session.commit()

    service = FinancialClearanceService(db_session)
    result = service.evaluate_discharge_clearance(hospital.id, admission.id, patient.id)

    assert result.can_discharge is False
    assert result.status == "outstanding_due"
    assert result.outstanding_amount == 600.0
    assert result.total_charges == 1000.0
    assert result.total_deposits_available == 400.0
    assert "outstanding balance ₹600.00" in result.reason


def test_scenario_3_1000_billed_and_600_unallocated_and_400_deposit_cleared(db_session: Session, hospital):
    """Scenario 3: 1000 billed + 600 unallocated + 400 deposit -> cleared."""
    patient, admission, account = _setup_admission_and_account(db_session, hospital)

    # 1000 billed
    charge = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        source_type=BillingSourceType.bed,
        source_id=admission.id,
        description="Bed charges",
        quantity=1.0,
        unit_price=1000.0,
        charge_amount=Decimal("1000.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        net_amount=Decimal("1000.00"),
        amount_paid=Decimal("0.00"),
        status=BillingChargeStatus.pending,
        created_by_name="Admin",
    )
    db_session.add(charge)

    # 600 unallocated payment tender
    payment = BillingPayment(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        amount=Decimal("600.00"),
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        received_by_name="Admin",
    )
    db_session.add(payment)

    # 400 deposit
    deposit = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        admission_id=admission.id,
        deposit_number=f"DEP-{uuid4().hex[:6].upper()}",
        deposit_type="admission",
        original_amount=Decimal("400.00"),
        available_amount=Decimal("400.00"),
        status=DepositStatus.available,
        payment_method=BillingPaymentMethod.cash,
        deposit_date=date.today(),
        received_by_name="Admin",
    )
    db_session.add(deposit)
    db_session.commit()

    service = FinancialClearanceService(db_session)
    result = service.evaluate_discharge_clearance(hospital.id, admission.id, patient.id)

    assert result.can_discharge is True
    assert result.status == "cleared"
    assert result.outstanding_amount == 0.0
    assert result.total_charges == 1000.0
    assert result.unallocated_payments == 600.0
    assert result.total_deposits_available == 400.0
