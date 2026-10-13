from datetime import date
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.billing.actions.billing_actions import (
    CreateChargeAction,
    CreatePaymentAction,
)
from modules.billing.contracts.billing_contracts import (
    BillingChargeCreate,
    BillingPaymentCreate,
    ChargeAllocationItem,
)
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
    allocate_specific_charges,
    draw_down_deposit_for_charges,
)
from modules.billing.services.invoice_service import create_invoice_from_charges
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b


def test_create_charge_action_persists_valid_account_id(
    db_session, hospital, patient
):
    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-001",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.commit()

    payload = BillingChargeCreate(
        patient_id=patient.id,
        account_id=acc.id,
        description="Bed charge for Room 101",
        charge_amount=2500.0,
        source_type=BillingSourceType.bed,
    )
    action = CreateChargeAction(db_session, hospital.id)
    user = {"sub": "admin", "name": "Admin", "role": "admin"}
    res = action.execute(payload, user)

    assert res.account_id == acc.id
    persisted = db_session.query(BillingCharge).filter(BillingCharge.id == res.id).first()
    assert persisted.account_id == acc.id


def test_create_charge_action_rejects_cross_patient_or_invalid_account(
    db_session, hospital, patient, patient_b
):
    acc_b = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient_b.id,
        account_number="IPD-ACC-002",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        created_by_name="Admin",
    )
    db_session.add(acc_b)
    db_session.commit()

    action = CreateChargeAction(db_session, hospital.id)
    user = {"sub": "admin", "name": "Admin", "role": "admin"}

    # Cross-patient account
    payload_cross = BillingChargeCreate(
        patient_id=patient.id,
        account_id=acc_b.id,
        description="Cross-patient charge attempt",
        charge_amount=1000.0,
    )
    with pytest.raises(HTTPException) as exc_info:
        action.execute(payload_cross, user)
    assert exc_info.value.status_code == 400
    assert "patient" in exc_info.value.detail.lower()

    # Non-existent account
    payload_missing = BillingChargeCreate(
        patient_id=patient.id,
        account_id=uuid4(),
        description="Missing account attempt",
        charge_amount=1000.0,
    )
    with pytest.raises(HTTPException) as exc_info2:
        action.execute(payload_missing, user)
    assert exc_info2.value.status_code in (400, 404)


def test_create_charge_action_rejects_closed_account(
    db_session, hospital, patient
):
    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-CLOSED",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.closed,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.commit()

    action = CreateChargeAction(db_session, hospital.id)
    user = {"sub": "admin", "name": "Admin", "role": "admin"}
    payload = BillingChargeCreate(
        patient_id=patient.id,
        account_id=acc.id,
        description="Charge on closed account",
        charge_amount=1000.0,
    )
    with pytest.raises(HTTPException) as exc_info:
        action.execute(payload, user)
    assert exc_info.value.status_code == 400
    assert "closed" in exc_info.value.detail.lower()


def test_create_invoice_rejects_mixed_accounts(
    db_session, hospital, patient
):
    acc_1 = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    acc_2 = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-02",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    db_session.add_all([acc_1, acc_2])
    db_session.flush()

    c1 = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_1.id,
        source_type=BillingSourceType.bed,
        description="Stay 1",
        charge_amount=5000.0,
        net_amount=5000.0,
        status=BillingChargeStatus.pending,
    )
    c2 = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_2.id,
        source_type=BillingSourceType.bed,
        description="Stay 2",
        charge_amount=4000.0,
        net_amount=4000.0,
        status=BillingChargeStatus.pending,
    )
    c_unscoped = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        source_type=BillingSourceType.other,
        description="Unscoped OPD",
        charge_amount=500.0,
        net_amount=500.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add_all([c1, c2, c_unscoped])
    db_session.commit()

    # Two distinct IPD accounts cannot be in one invoice
    with pytest.raises(ValueError) as exc1:
        create_invoice_from_charges(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            charge_ids=[c1.id, c2.id],
        )
    assert "multiple" in str(exc1.value).lower() or "account" in str(exc1.value).lower()

    # Account-scoped and unscoped cannot be in one invoice
    with pytest.raises(ValueError) as exc2:
        create_invoice_from_charges(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            charge_ids=[c1.id, c_unscoped.id],
        )
    assert "account" in str(exc2.value).lower()


def test_deposit_allocation_strictly_respects_account_boundaries(
    db_session, hospital, patient
):
    acc_a = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-A",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    acc_b = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-B",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    db_session.add_all([acc_a, acc_b])
    db_session.flush()

    dep_a = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_a.id,
        deposit_number="DEP-A",
        deposit_date=date(2026, 10, 1),
        deposit_type="ipd_advance",
        payment_method=BillingPaymentMethod.cash,
        original_amount=10000.0,
        available_amount=10000.0,
        status=DepositStatus.available,
    )
    charge_b = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_b.id,
        source_type=BillingSourceType.other,
        description="Charge for Admission B",
        charge_amount=3000.0,
        net_amount=3000.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add_all([dep_a, charge_b])
    db_session.commit()

    # Allocating Deposit A to Charge B must fail
    with pytest.raises(ValueError) as exc:
        draw_down_deposit_for_charges(
            db_session,
            hospital_id=hospital.id,
            deposit_id=dep_a.id,
            allocations_plan=[{"charge_id": charge_b.id, "amount": 3000.0}],
        )
    assert "account" in str(exc.value).lower()


def test_general_deposit_cannot_settle_ipd_charge_manual(db_session, hospital, patient):
    """General/unscoped deposit (account_id=None) must NOT settle an IPD charge (account_id=A)."""
    acc_a = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-GEN-1",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    db_session.add(acc_a)
    db_session.flush()

    gen_dep = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        deposit_number="DEP-GEN-1",
        deposit_date=date(2026, 10, 1),
        deposit_type="general",
        payment_method=BillingPaymentMethod.cash,
        original_amount=5000.0,
        available_amount=5000.0,
        status=DepositStatus.available,
    )
    charge_a = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_a.id,
        source_type=BillingSourceType.bed,
        description="Bed stay in Account A",
        charge_amount=2000.0,
        net_amount=2000.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add_all([gen_dep, charge_a])
    db_session.commit()

    # Manual allocation from general deposit to IPD charge must be rejected
    with pytest.raises(ValueError) as exc:
        draw_down_deposit_for_charges(
            db_session,
            hospital_id=hospital.id,
            deposit_id=gen_dep.id,
            allocations_plan=[{"charge_id": charge_a.id, "amount": 2000.0}],
        )
    assert "account" in str(exc.value).lower() or "cross-account" in str(exc.value).lower()
    # Deposit remains untouched
    db_session.refresh(gen_dep)
    assert float(gen_dep.available_amount) == 5000.0


def test_general_deposit_settles_unscoped_charge_successfully(db_session, hospital, patient):
    """General/unscoped deposit (account_id=None) settles unscoped charge (account_id=None)."""
    gen_dep = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        deposit_number="DEP-GEN-2",
        deposit_date=date(2026, 10, 1),
        deposit_type="general",
        payment_method=BillingPaymentMethod.cash,
        original_amount=5000.0,
        available_amount=5000.0,
        status=DepositStatus.available,
    )
    charge_unscoped = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        source_type=BillingSourceType.other,
        description="General Clinic Consultation",
        charge_amount=1500.0,
        net_amount=1500.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add_all([gen_dep, charge_unscoped])
    db_session.commit()

    allocated = draw_down_deposit_for_charges(
        db_session,
        hospital_id=hospital.id,
        deposit_id=gen_dep.id,
        allocations_plan=[{"charge_id": charge_unscoped.id, "amount": 1500.0}],
    )
    assert allocated == 1500.0
    db_session.refresh(gen_dep)
    assert float(gen_dep.available_amount) == 3500.0


def test_ipd_deposit_cannot_settle_unscoped_charge(db_session, hospital, patient):
    """IPD Deposit A (account_id=A) must NOT settle unscoped charge (account_id=None)."""
    acc_a = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-GEN-3",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    db_session.add(acc_a)
    db_session.flush()

    dep_a = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_a.id,
        deposit_number="DEP-IPD-3",
        deposit_date=date(2026, 10, 1),
        deposit_type="ipd_advance",
        payment_method=BillingPaymentMethod.cash,
        original_amount=5000.0,
        available_amount=5000.0,
        status=DepositStatus.available,
    )
    charge_unscoped = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        source_type=BillingSourceType.other,
        description="Unscoped OPD test",
        charge_amount=1000.0,
        net_amount=1000.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add_all([dep_a, charge_unscoped])
    db_session.commit()

    with pytest.raises(ValueError) as exc:
        draw_down_deposit_for_charges(
            db_session,
            hospital_id=hospital.id,
            deposit_id=dep_a.id,
            allocations_plan=[{"charge_id": charge_unscoped.id, "amount": 1000.0}],
        )
    assert "account" in str(exc.value).lower() or "cross-account" in str(exc.value).lower()


def test_bulk_drawdown_strictly_respects_account_boundaries(db_session, hospital, patient):
    """Bulk settlement for Account A must use ONLY Account A deposit, leaving general deposit untouched."""
    from modules.billing.services.billing_service import draw_down_deposits_bulk_for_account

    acc_a = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-BULK-1",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    db_session.add(acc_a)
    db_session.flush()

    dep_a = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_a.id,
        deposit_number="DEP-BULK-A",
        deposit_date=date(2026, 10, 1),
        deposit_type="ipd_advance",
        payment_method=BillingPaymentMethod.cash,
        original_amount=1000.0,
        available_amount=1000.0,
        status=DepositStatus.available,
    )
    dep_gen = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        deposit_number="DEP-BULK-GEN",
        deposit_date=date(2026, 10, 1),
        deposit_type="general",
        payment_method=BillingPaymentMethod.cash,
        original_amount=10000.0,
        available_amount=10000.0,
        status=DepositStatus.available,
    )
    charge_a = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_a.id,
        source_type=BillingSourceType.bed,
        description="Stay A",
        charge_amount=5000.0,
        net_amount=5000.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add_all([dep_a, dep_gen, charge_a])
    db_session.commit()

    allocated = draw_down_deposits_bulk_for_account(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_a.id,
    )
    # Only 1000 from dep_a may be drawn down; general deposit of 10000 must NOT cross
    assert allocated == 1000.0
    db_session.refresh(dep_a)
    db_session.refresh(dep_gen)
    assert float(dep_a.available_amount) == 0.0
    assert float(dep_gen.available_amount) == 10000.0


def test_deposit_type_scope_validation(db_session, hospital, patient, patient_b):
    """Validates deposit type rules: general rejects admission/account; ipd_advance requires valid admission."""
    from modules.inpatient.entities.admission import Admission
    from modules.billing.services.billing_service import create_deposit

    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        admitted_at=date(2026, 10, 1),
        status="admitted",
        bed_id=uuid4(),
    )
    db_session.add(adm)
    db_session.commit()

    # 1. general + admission_id must reject
    with pytest.raises(ValueError) as exc1:
        create_deposit(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            amount=5000.0,
            deposit_date=date(2026, 10, 1),
            deposit_type="general",
            admission_id=adm.id,
        )
    assert "general" in str(exc1.value).lower()

    # 2. ipd_advance without admission or account must reject
    with pytest.raises(ValueError) as exc2:
        create_deposit(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            amount=5000.0,
            deposit_date=date(2026, 10, 1),
            deposit_type="ipd_advance",
            admission_id=None,
            account_id=None,
        )
    assert "ipd_advance" in str(exc2.value).lower() or "admission" in str(exc2.value).lower()

    # 3. ipd_advance with valid admission succeeds and scopes to IPD account
    dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=5000.0,
        deposit_date=date(2026, 10, 1),
        deposit_type="ipd_advance",
        admission_id=adm.id,
    )
    assert dep.account_id is not None
    assert dep.admission_id == adm.id

    # 4. cross-patient admission must reject
    with pytest.raises(ValueError) as exc3:
        create_deposit(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient_b.id,
            amount=5000.0,
            deposit_date=date(2026, 10, 1),
            deposit_type="ipd_advance",
            admission_id=adm.id,
        )
    assert "patient" in str(exc3.value).lower()


def test_manual_charge_account_inference_and_multi_admission_guard(
    db_session, hospital, patient
):
    """Scenario (Requirements 14, 15, 25):
    - 1 open IPD account: auto-infers account_id.
    - 1 open IPD account with explicit_unscoped=True: leaves account_id None.
    - Multiple open IPD accounts: raises 400 requiring explicit selection.
    """
    user = {"sub": "admin", "name": "Admin", "role": "admin"}
    action = CreateChargeAction(db_session, hospital.id)

    # Patient has 0 open IPD accounts: charge is unscoped
    res0 = action.execute(
        BillingChargeCreate(
            patient_id=patient.id,
            description="OPD Consultation",
            charge_amount=500.0,
            source_type=BillingSourceType.other,
        ),
        user=user,
    )
    assert res0.account_id is None

    # Now open 1 IPD account
    acc1 = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-MAN-1",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    db_session.add(acc1)
    db_session.commit()

    # With 1 open account, omit account_id -> auto-infers acc1.id
    res1 = action.execute(
        BillingChargeCreate(
            patient_id=patient.id,
            description="Syringe Fee",
            charge_amount=50.0,
            source_type=BillingSourceType.other,
        ),
        user=user,
    )
    assert res1.account_id == acc1.id

    # With 1 open account, but explicit_unscoped=True -> stays None
    res_unscoped = action.execute(
        BillingChargeCreate(
            patient_id=patient.id,
            description="Cafeteria Charge",
            charge_amount=120.0,
            source_type=BillingSourceType.other,
            explicit_unscoped=True,
        ),
        user=user,
    )
    assert res_unscoped.account_id is None

    # Now open a second IPD account (exceptional clinical state)
    acc2 = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-MAN-2",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    db_session.add(acc2)
    db_session.commit()

    # Multi-admission guard: omitting account_id MUST reject with 400
    with pytest.raises(HTTPException) as exc:
        action.execute(
            BillingChargeCreate(
                patient_id=patient.id,
                description="Nursing Fee",
                charge_amount=200.0,
                source_type=BillingSourceType.other,
            ),
            user=user,
        )
    assert exc.value.status_code == 400
    assert "multiple open ipd admissions" in str(exc.value.detail).lower()

    # Explicit selection works when multiple accounts exist
    res_explicit = action.execute(
        BillingChargeCreate(
            patient_id=patient.id,
            account_id=acc2.id,
            description="Nursing Fee Episode 2",
            charge_amount=200.0,
            source_type=BillingSourceType.other,
        ),
        user=user,
    )
    assert res_explicit.account_id == acc2.id


def test_payment_account_inference_and_mixed_account_rejection(
    db_session, hospital, patient
):
    """Scenario (Requirements 16, 17, 26):
    - Selected charges all Account A -> payment.account_id == Account A.
    - Mixed Account A + Account B -> reject 400.
    - Account A + unscoped -> reject 400.
    """
    user = {"sub": "admin", "name": "Admin", "role": "admin"}
    pay_action = CreatePaymentAction(db_session, hospital.id)

    acc_a = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-PAY-A",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    acc_b = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-PAY-B",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    db_session.add_all([acc_a, acc_b])
    db_session.flush()

    c_a = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_a.id,
        source_type=BillingSourceType.other,
        description="Med A",
        charge_amount=1000.0,
        net_amount=1000.0,
        status=BillingChargeStatus.pending,
    )
    c_b = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_b.id,
        source_type=BillingSourceType.other,
        description="Med B",
        charge_amount=2000.0,
        net_amount=2000.0,
        status=BillingChargeStatus.pending,
    )
    c_unscoped = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        source_type=BillingSourceType.other,
        description="Unscoped OPD",
        charge_amount=500.0,
        net_amount=500.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add_all([c_a, c_b, c_unscoped])
    db_session.commit()

    # 1. Selected charges all Account A -> infers account_id == acc_a.id
    pay_res = pay_action.execute(
        BillingPaymentCreate(
            patient_id=patient.id,
            amount=1000.0,
            payment_date=date.today(),
            allocations=[ChargeAllocationItem(charge_id=c_a.id, amount=1000.0)],
        ),
        user=user,
    )
    assert pay_res.account_id == acc_a.id

    # 2. Mixed Account A + Account B -> rejects 400
    with pytest.raises(HTTPException) as exc_mixed:
        pay_action.execute(
            BillingPaymentCreate(
                patient_id=patient.id,
                amount=3000.0,
                payment_date=date.today(),
                allocations=[
                    ChargeAllocationItem(charge_id=c_a.id, amount=1000.0),
                    ChargeAllocationItem(charge_id=c_b.id, amount=2000.0),
                ],
            ),
            user=user,
        )
    assert exc_mixed.value.status_code == 400
    assert "multiple or mixed financial accounts" in str(exc_mixed.value.detail).lower()

    # 3. Mixed Account A + unscoped -> rejects 400
    with pytest.raises(HTTPException) as exc_mixed_unscoped:
        pay_action.execute(
            BillingPaymentCreate(
                patient_id=patient.id,
                amount=1500.0,
                payment_date=date.today(),
                allocations=[
                    ChargeAllocationItem(charge_id=c_a.id, amount=1000.0),
                    ChargeAllocationItem(charge_id=c_unscoped.id, amount=500.0),
                ],
            ),
            user=user,
        )
    assert exc_mixed_unscoped.value.status_code == 400
    assert "multiple or mixed financial accounts" in str(exc_mixed_unscoped.value.detail).lower()


def test_payment_fifo_respects_account_boundaries(db_session, hospital, patient):
    """Scenario (Requirements 18, 26):
    - Unscoped payment FIFO does not pay account-scoped charges.
    - Account A payment FIFO does not pay Account B or unscoped charges.
    """
    from modules.billing.services.billing_service import create_payment

    acc_a = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-FIFO-A",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    acc_b = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-FIFO-B",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
    )
    db_session.add_all([acc_a, acc_b])
    db_session.flush()

    c_a = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_a.id,
        source_type=BillingSourceType.other,
        description="Charge A",
        charge_amount=1000.0,
        net_amount=1000.0,
        status=BillingChargeStatus.pending,
    )
    c_b = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc_b.id,
        source_type=BillingSourceType.other,
        description="Charge B",
        charge_amount=2000.0,
        net_amount=2000.0,
        status=BillingChargeStatus.pending,
    )
    c_unscoped = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        source_type=BillingSourceType.other,
        description="Charge Unscoped",
        charge_amount=3000.0,
        net_amount=3000.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add_all([c_a, c_b, c_unscoped])
    db_session.commit()

    # 1. Unscoped payment FIFO (amount 1500)
    pay_unscoped = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=1500.0,
        account_id=None,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes="Unscoped FIFO",
        received_by_name="Cashier",
        allocate=True,
    )
    db_session.commit()
    db_session.refresh(c_a)
    db_session.refresh(c_b)
    db_session.refresh(c_unscoped)

    # Unscoped payment MUST ONLY pay c_unscoped, NOT c_a or c_b
    assert float(c_unscoped.amount_paid) == 1500.0
    assert float(c_a.amount_paid) == 0.0
    assert float(c_b.amount_paid) == 0.0

    # 2. Account A payment FIFO (amount 1000)
    pay_a = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=1000.0,
        account_id=acc_a.id,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes="Account A FIFO",
        received_by_name="Cashier",
        allocate=True,
    )
    db_session.commit()
    db_session.refresh(c_a)
    db_session.refresh(c_b)
    db_session.refresh(c_unscoped)

    # Account A payment MUST ONLY pay c_a, NOT c_b or c_unscoped
    assert float(c_a.amount_paid) == 1000.0
    assert c_a.status.value == "paid"
    assert float(c_b.amount_paid) == 0.0
    assert float(c_unscoped.amount_paid) == 1500.0

