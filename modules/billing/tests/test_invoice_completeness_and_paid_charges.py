from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from shared.exceptions import ValidationError

from modules.billing.actions.billing_actions import (
    CreateInvoiceAction,
    CreatePaymentAction,
    GetInvoiceAction,
    PrintInvoiceAction,
    ReissueInvoiceAction,
)
from modules.patients.entities.patient import Patient
from modules.billing.contracts.billing_contracts import (
    BillingInvoiceCreate,
    BillingInvoiceReissueRequest,
    BillingPaymentCreate,
    ChargeAllocationItem,
)
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPaymentAllocation,
    BillingPaymentMethod,
    BillingReceipt,
    BillingSourceType,
    DepositStatus,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
)
from modules.billing.services.billing_service import (
    create_deposit,
    create_payment,
    draw_down_deposits_bulk_for_account,
    ensure_charge,
    prepare_ipd_bill,
    recompute_charge_financials,
)
from modules.billing.services.invoice_service import create_invoice_from_charges
from modules.inpatient.actions.admission_actions import DischargePatientAction
from modules.inpatient.contracts.inpatient_contracts import DischargeRequest
from modules.inpatient.entities.admission import (
    Admission,
    AdmissionStatus,
    BedStaySegment,
)
from tests.conftest import db_session, hospital, patient


def test_deposit_paid_charges_all_remain_invoiceable(db_session, hospital, patient):
    """Requirement 20 (Items 1-5):
    Charges fully settled by IPD advance/deposits (Bed, Lab, Rad, Pharm, OT)
    MUST all remain invoiceable and snapshot into the formal invoice.
    """
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-INV-001",
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-INV01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    # Collect ₹20,000 IPD Advance
    dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=20000.0,
        deposit_date=date.today(),
        deposit_type="ipd_advance",
        admission_id=adm.id,
        account_id=acc.id,
    )
    db_session.commit()

    # Create 5 charges under this IPD account
    chg_bed = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.bed,
        source_id=adm.id,
        description="Bed Charges (2 Days)",
        charge_amount=5000.0,
    )
    chg_lab = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Complete Blood Count",
        charge_amount=2000.0,
    )
    chg_rad = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.radiology,
        source_id=uuid4(),
        description="Chest X-Ray",
        charge_amount=3000.0,
    )
    chg_pharm = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.pharmacy,
        source_id=uuid4(),
        description="IV Antibiotics",
        charge_amount=1500.0,
    )
    chg_ot = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.ot,
        source_id=uuid4(),
        description="Appendectomy OT Charge",
        charge_amount=4000.0,
    )
    db_session.commit()

    # Bulk draw down deposits for the IPD account
    drawn = draw_down_deposits_bulk_for_account(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
    )
    db_session.commit()

    assert drawn == 15500.0
    db_session.refresh(dep)
    assert dep.available_amount == 4500.0

    # Verify that all 5 charges are now status == 'paid'
    all_charges = [chg_bed, chg_lab, chg_rad, chg_pharm, chg_ot]
    for c in all_charges:
        db_session.refresh(c)
        assert c.status == BillingChargeStatus.paid
        assert float(c.amount_paid) == float(c.net_amount)

    # Formal invoice creation MUST accept all 5 paid charges
    inv = create_invoice_from_charges(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        charge_ids=[c.id for c in all_charges],
        account_id=acc.id,
    )
    db_session.commit()

    assert inv is not None
    assert inv.grand_total == 15500.0
    assert inv.status == BillingInvoiceStatus.paid
    assert len(inv.lines) == 5
    line_sources = {ln.source_type for ln in inv.lines}
    assert line_sources == {"bed", "laboratory", "radiology", "pharmacy", "ot"}


def test_normally_paid_charge_remains_invoiceable(db_session, hospital, patient):
    """Requirement 20 (Item 6):
    A charge settled by standard payment before formal invoice generation
    must remain invoiceable and appear in the invoice snapshot.
    """
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Lipid Profile",
        charge_amount=2000.0,
    )
    db_session.commit()

    # Cashier collects normal payment against charge before invoice
    pay = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=2000.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes="Standard cash payment",
        received_by_name="Cashier",
        allocate=True,
    )
    db_session.commit()

    db_session.refresh(chg)
    assert chg.status == BillingChargeStatus.paid
    assert float(chg.amount_paid) == 2000.0

    # Invoice generated afterward MUST include the paid service
    inv = create_invoice_from_charges(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        charge_ids=[chg.id],
    )
    db_session.commit()

    assert inv.grand_total == 2000.0
    assert inv.status == BillingInvoiceStatus.paid
    assert len(inv.lines) == 1
    assert inv.lines[0].description == "Lipid Profile"


def test_cancelled_charge_is_not_invoiceable(db_session, hospital, patient):
    """Requirement 20 (Item 7):
    Cancelled charges must not be invoiceable.
    """
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Voided Charge",
        charge_amount=1000.0,
    )
    chg.status = BillingChargeStatus.cancelled
    db_session.commit()

    with pytest.raises(ValueError) as exc:
        create_invoice_from_charges(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            charge_ids=[chg.id],
        )
    assert "were not found" in str(exc.value)


def test_already_actively_invoiced_charge_is_not_invoiceable_again(
    db_session, hospital, patient
):
    """Requirement 20 (Item 8):
    Charges already snapshot on an active invoice cannot be invoiced again.
    """
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        source_id=uuid4(),
        description="Doctor Consultation",
        charge_amount=800.0,
    )
    db_session.commit()

    inv1 = create_invoice_from_charges(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        charge_ids=[chg.id],
    )
    db_session.commit()
    assert inv1.status == BillingInvoiceStatus.generated

    # Attempting to invoice chg again must be rejected
    with pytest.raises(ValueError) as exc:
        create_invoice_from_charges(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            charge_ids=[chg.id],
        )
    assert "already on an active invoice" in str(exc.value)


def test_final_ipd_invoice_contains_paid_partially_paid_and_unpaid_charges(
    db_session, hospital, patient
):
    """Requirement 20 (Item 9):
    Final invoice includes mix of paid, partially paid, and unpaid charges.
    """
    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-MIX01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    c_paid = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.bed,
        source_id=uuid4(),
        description="ICU Bed",
        charge_amount=5000.0,
    )
    c_partial = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.pharmacy,
        source_id=uuid4(),
        description="Antibiotics",
        charge_amount=3000.0,
    )
    c_unpaid = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.radiology,
        source_id=uuid4(),
        description="CT Scan",
        charge_amount=2000.0,
    )
    db_session.commit()

    # Pay c_paid in full (₹5,000) and c_partial partially (₹1,000)
    pay = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        amount=6000.0,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes="Multiple charge settlement",
        received_by_name="Cashier",
        allocations_plan=[
            {"charge_id": c_paid.id, "amount": 5000.0},
            {"charge_id": c_partial.id, "amount": 1000.0},
        ],
    )
    db_session.commit()

    db_session.refresh(c_paid)
    db_session.refresh(c_partial)
    db_session.refresh(c_unpaid)
    assert c_paid.status == BillingChargeStatus.paid
    assert c_partial.status == BillingChargeStatus.partially_paid
    assert c_unpaid.status == BillingChargeStatus.pending

    inv = create_invoice_from_charges(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        charge_ids=[c_paid.id, c_partial.id, c_unpaid.id],
        account_id=acc.id,
    )
    db_session.commit()

    assert inv.grand_total == 10000.0
    assert len(inv.lines) == 3
    # Invoice is not fully paid because c_partial and c_unpaid have balances
    assert inv.status == BillingInvoiceStatus.generated


def test_invoice_reissue_preserves_allocations_and_does_not_duplicate_tender(
    db_session, hospital, patient
):
    """Requirement 20 (Items 10-11):
    Invoice reissue preserves charge-level allocations without duplicating tender,
    and historical receipts remain immutable.
    """
    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-ALLOC01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    bed_charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.bed,
        source_id=uuid4(),
        description="Bed Charges (5 Days)",
        charge_amount=7500.0,
    )
    db_session.commit()

    # Generate initial invoice
    inv_action = CreateInvoiceAction(db_session, hospital.id)
    old_inv = inv_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=acc.id,
            charge_ids=[bed_charge.id],
        ),
        user={"name": "Biller"},
    )
    assert old_inv.grand_total == 7500.0

    # Pay ₹6,000 toward bed charge with linked receipt
    pay_action = CreatePaymentAction(db_session, hospital.id)
    pay_res = pay_action.execute(
        BillingPaymentCreate(
            patient_id=patient.id,
            account_id=acc.id,
            amount=6000.0,
            payment_date=date.today(),
            payment_method=BillingPaymentMethod.upi,
            allocations=[ChargeAllocationItem(charge_id=bed_charge.id, amount=6000.0)],
            linked_invoice_id=old_inv.id,
        ),
        user={"name": "Cashier"},
    )
    db_session.commit()

    # Verify initial allocation
    allocations = (
        db_session.query(BillingPaymentAllocation)
        .filter(BillingPaymentAllocation.charge_id == bed_charge.id)
        .all()
    )
    assert len(allocations) == 1
    assert float(allocations[0].allocated_amount) == 6000.0

    # Receipt points to old invoice
    receipt = (
        db_session.query(BillingReceipt)
        .filter(BillingReceipt.payment_id == pay_res.id)
        .first()
    )
    assert receipt.linked_invoice_id == old_inv.id

    # Reprice bed charge to ₹9,000 (extended stay)
    bed_charge.charge_amount = 9000.0
    bed_charge.net_amount = 9000.0
    db_session.commit()

    # Reissue invoice
    reissue_action = ReissueInvoiceAction(db_session, hospital.id)
    new_inv = reissue_action.execute(
        old_inv.id,
        BillingInvoiceReissueRequest(reason="Stay extended from 5 to 6 days"),
        user={"name": "Biller"},
    )

    # 1. Old invoice is cancelled/superseded
    old_db = (
        db_session.query(BillingInvoice)
        .filter(BillingInvoice.id == old_inv.id)
        .first()
    )
    assert old_db.status == BillingInvoiceStatus.cancelled
    assert "Superseded" in old_db.notes

    # 2. New invoice has gross ₹9,000
    assert new_inv.grand_total == 9000.0

    # 3. Allocations were NOT duplicated
    allocations_after = (
        db_session.query(BillingPaymentAllocation)
        .filter(BillingPaymentAllocation.charge_id == bed_charge.id)
        .all()
    )
    assert len(allocations_after) == 1  # exactly 1 allocation preserved
    assert float(allocations_after[0].allocated_amount) == 6000.0

    # 4. Bed charge amount_paid remains exactly ₹6,000; remaining due is ₹3,000
    db_session.refresh(bed_charge)
    assert float(bed_charge.amount_paid) == 6000.0
    remaining = float(bed_charge.net_amount) - float(bed_charge.amount_paid)
    assert remaining == 3000.0

    # 5. Historical receipt remains immutable and retains link to old invoice
    db_session.refresh(receipt)
    assert receipt.linked_invoice_id == old_inv.id


def test_surgery_deposit_respects_exact_episode_scope(db_session, hospital, patient):
    """Requirement 20 (Item 12) & Requirements 15-17:
    Surgery deposits strictly observe account scoping:
    - IPD surgery deposit linked to admission funds that IPD OT charge.
    - General unassigned surgery deposit (account_id=None) cannot fund IPD OT charge.
    """
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-OT-001",
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-OT01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    ot_charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.ot,
        source_id=uuid4(),
        description="OR Major Procedure",
        charge_amount=8000.0,
    )
    db_session.commit()

    # 1. Unscoped / general surgery advance
    gen_surg_dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=5000.0,
        deposit_date=date.today(),
        deposit_type="surgery_deposit",
        account_id=None,
        admission_id=None,
    )
    db_session.commit()
    assert gen_surg_dep.account_id is None

    # General deposit must NOT settle the IPD OT charge
    from modules.billing.services.billing_service import allocate_specific_charges
    with pytest.raises(ValueError) as exc:
        allocate_specific_charges(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            allocations_plan=[{"charge_id": ot_charge.id, "amount": 5000.0}],
            deposit_id=gen_surg_dep.id,
        )
    assert "Cross-account allocation prohibited" in str(exc.value)

    # 2. IPD-scoped surgery advance
    ipd_surg_dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=8000.0,
        deposit_date=date.today(),
        deposit_type="surgery_deposit",
        admission_id=adm.id,
    )
    db_session.commit()
    assert ipd_surg_dep.account_id == acc.id

    # IPD deposit successfully settles the IPD OT charge
    allocated = allocate_specific_charges(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        allocations_plan=[{"charge_id": ot_charge.id, "amount": 8000.0}],
        deposit_id=ipd_surg_dep.id,
    )
    db_session.commit()

    assert allocated == 8000.0
    db_session.refresh(ot_charge)
    assert ot_charge.status == BillingChargeStatus.paid


def test_final_acceptance_scenario_end_to_end(db_session, hospital, patient):
    """Requirement 21: Full End-to-End Acceptance Scenario
    1. Patient admitted.
    2. Collect IPD Advance = ₹20,000.
    3. Generate 5 services: Bed ₹5,000, Lab ₹2,000, Rad ₹3,000, Pharm ₹1,500, OT ₹4,000. Total = ₹15,500.
    4. Central deposit application settles ₹15,500. Remaining deposit = ₹4,500.
    5. Generate IPD invoice:
       - Gross invoice = ₹15,500
       - Advance utilized = ₹15,500
       - Amount due = ₹0
       - Remaining advance = ₹4,500
       - All 5 items appear on invoice.
    6. Extend bed stay by ₹2,000 (total ₹17,500).
    7. Stale invoice blocks discharge.
    8. Prepare IPD bill draws down additional ₹2,000 (remaining advance = ₹2,500).
    9. Reissue invoice:
       - New gross invoice = ₹17,500
       - Advance utilized = ₹17,500
       - Outstanding = ₹0
       - Remaining advance = ₹2,500
       - Old invoice remains auditable.
       - No allocations duplicated.
    10. Final discharge succeeds.
    """
    # 1. Admit patient
    adm_start = datetime.now(timezone.utc) - timedelta(days=5)
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-E2E-001",
        status=AdmissionStatus.admitted,
        admitted_at=adm_start,
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-E2E01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    seg = BedStaySegment(
        hospital_id=hospital.id,
        admission_id=adm.id,
        bed_id=uuid4(),
        ward_id=uuid4(),
        rate_per_day=Decimal("1000.00"),
        started_at=adm_start,
        ended_at=None,  # active
    )
    db_session.add(seg)
    db_session.flush()

    # 2. Collect IPD Advance = ₹20,000
    dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=20000.0,
        deposit_date=date.today(),
        deposit_type="ipd_advance",
        admission_id=adm.id,
        account_id=acc.id,
    )
    db_session.commit()

    # 3. Generate 5 services: Bed ₹5,000, Lab ₹2,000, Rad ₹3,000, Pharm ₹1,500, OT ₹4,000 (Total ₹15,500)
    chg_bed = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.bed,
        source_id=adm.id,
        description="Bed Stay 5 Days",
        charge_amount=5000.0,
    )
    chg_lab = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Pathology Panels",
        charge_amount=2000.0,
    )
    chg_rad = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.radiology,
        source_id=uuid4(),
        description="MRI Brain",
        charge_amount=3000.0,
    )
    chg_pharm = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.pharmacy,
        source_id=uuid4(),
        description="Surgical Pharmacy",
        charge_amount=1500.0,
    )
    chg_ot = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.ot,
        source_id=uuid4(),
        description="Operating Theater Charges",
        charge_amount=4000.0,
    )
    db_session.commit()

    all_charges = [chg_bed, chg_lab, chg_rad, chg_pharm, chg_ot]

    # 4. Central deposit application settles ₹15,500
    drawn = draw_down_deposits_bulk_for_account(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
    )
    db_session.commit()

    assert drawn == 15500.0
    db_session.refresh(dep)
    assert dep.available_amount == 4500.0
    for c in all_charges:
        db_session.refresh(c)
        assert c.status == BillingChargeStatus.paid

    # 5. Generate IPD invoice:
    inv_action = CreateInvoiceAction(db_session, hospital.id)
    inv1 = inv_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=acc.id,
            charge_ids=[c.id for c in all_charges],
        ),
        user={"name": "Chief Biller"},
    )
    assert inv1.grand_total == 15500.0
    assert inv1.status == BillingInvoiceStatus.paid
    assert len(inv1.lines) == 5

    # 6. Extend bed stay by ₹2,000 (7 days @ 1000 = ₹7,000, total charges = ₹17,500)
    seg.started_at = adm_start - timedelta(days=2)
    chg_bed.charge_amount = 7000.0
    chg_bed.net_amount = 7000.0
    recompute_charge_financials(db_session, chg_bed)
    db_session.commit()

    # 7. Stale invoice blocks discharge
    adm.status = AdmissionStatus.discharge_requested
    db_session.commit()
    discharge_action = DischargePatientAction(db_session)
    with pytest.raises(ValidationError) as exc:
        discharge_action.execute(
            hospital.id,
            DischargeRequest(admission_id=adm.id, no_discharge_meds=True),
            actor={"name": "Discharge Coordinator"},
        )
    assert "Final bill out of date" in str(exc.value)

    # 8. Reissue IPD invoice (prepare_ipd_bill automatically settles the ₹2,000 with remaining advance)
    reissue_action = ReissueInvoiceAction(db_session, hospital.id)
    inv2 = reissue_action.execute(
        inv1.id,
        BillingInvoiceReissueRequest(reason="Bed stay extended by 2 days"),
        user={"name": "Chief Biller"},
    )
    db_session.commit()

    # 9. Verify new invoice and financial state:
    assert inv2.grand_total == 17500.0
    assert inv2.status == BillingInvoiceStatus.paid
    assert len(inv2.lines) == 5

    # Old invoice is cancelled/superseded
    old_inv_db = (
        db_session.query(BillingInvoice)
        .filter(BillingInvoice.id == inv1.id)
        .first()
    )
    assert old_inv_db.status == BillingInvoiceStatus.cancelled
    assert "Superseded" in old_inv_db.notes

    # Remaining deposit is ₹2,500 (20,000 - 17,500)
    db_session.refresh(dep)
    assert dep.available_amount == 2500.0

    # Bed charge is paid in full (₹7,000)
    db_session.refresh(chg_bed)
    assert chg_bed.status == BillingChargeStatus.paid
    assert float(chg_bed.amount_paid) == 7000.0

    # 10. Final discharge now succeeds without block!
    disc_res = discharge_action.execute(
        hospital.id,
        DischargeRequest(admission_id=adm.id, no_discharge_meds=True),
        actor={"name": "Discharge Coordinator"},
    )
    assert disc_res.status == AdmissionStatus.discharged


def test_prepare_creates_first_bed_charge_blocks_stale_invoice_creation(db_session, hospital, patient):
    """Sections 1-6 & 20: Reproduce exact browser sequence.
    Initial state:
    - Patient admitted
    - Active IPD FinancialAccount exists
    - Active BedStaySegment exists
    - NO BillingCharge(source_type = bed) exists yet
    - Laboratory charge ₹2,000 exists
    Simulate state visible to billing before bed preparation:
    - invoiceable charge IDs = [LAB_ID]
    Now trigger sequence as frontend:
    - prepareIpdBill(account_id) creates BED_ID
    - Client sends stale selection [LAB_ID] with account_id
    Safe outcome: 409 Conflict raised, preventing silent omission of bed charge.
    Then, client sends reviewed selection [LAB_ID, BED_ID]:
    Safe outcome: Invoice generated with account_id containing both Lab and Bed.
    """
    adm_start = datetime.now(timezone.utc) - timedelta(days=2)
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-BED-RACE-01",
        status=AdmissionStatus.admitted,
        admitted_at=adm_start,
    )
    db_session.add(adm)
    db_session.flush()

    seg = BedStaySegment(
        hospital_id=hospital.id,
        admission_id=adm.id,
        bed_id=uuid4(),
        ward_id=uuid4(),
        rate_per_day=Decimal("1500.00"),
        started_at=adm_start,
        ended_at=None,
    )
    db_session.add(seg)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-RACE-01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    # Create only Laboratory charge initially
    chg_lab = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Routine Lipid Panel",
        charge_amount=2000.0,
    )
    db_session.commit()

    # Verify: NO bed BillingCharge exists yet
    pre_bed_count = db_session.query(BillingCharge).filter(
        BillingCharge.account_id == acc.id,
        BillingCharge.source_type == BillingSourceType.bed,
    ).count()
    assert pre_bed_count == 0

    # Simulate sequence: prepareIpdBill runs
    prepare_ipd_bill(db_session, hospital_id=hospital.id, account_id=acc.id)
    db_session.commit()

    # Bed charge was created by preparation
    created_bed_charge = db_session.query(BillingCharge).filter(
        BillingCharge.account_id == acc.id,
        BillingCharge.source_type == BillingSourceType.bed,
    ).first()
    assert created_bed_charge is not None
    assert created_bed_charge.net_amount > 0

    # Attempt 1: Client submits stale selection [LAB_ID] omitting the new bed charge
    create_action = CreateInvoiceAction(db_session, hospital.id)
    with pytest.raises(HTTPException) as exc:
        create_action.execute(
            BillingInvoiceCreate(
                patient_id=patient.id,
                account_id=acc.id,
                charge_ids=[chg_lab.id],
            ),
            user={"name": "Biller"},
        )
    assert exc.value.status_code == 409
    assert "Billing state changed" in exc.value.detail

    # Attempt 2: Biller refreshes, reviews both charges, and submits [LAB_ID, BED_ID] with account_id
    reviewed_invoice = create_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=acc.id,
            charge_ids=[chg_lab.id, created_bed_charge.id],
        ),
        user={"name": "Biller"},
    )
    db_session.commit()

    assert reviewed_invoice.account_id == acc.id
    assert len(reviewed_invoice.lines) == 2
    line_sources = {ln.source_type for ln in reviewed_invoice.lines}
    assert "laboratory" in line_sources
    assert "bed" in line_sources


def test_repriced_bed_charge_blocks_stale_invoice_generation(db_session, hospital, patient):
    """Section 9: Bed charge repriced immediately before invoice generation.
    Initial bed charge = ₹5,000.
    Reconciliation changes bed charge to ₹7,000.
    Generating on stale view raises 409, requiring biller confirmation.
    """
    adm_start = datetime.now(timezone.utc) - timedelta(days=5)
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-REPRICE-01",
        status=AdmissionStatus.admitted,
        admitted_at=adm_start,
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-REPRICE",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    # Bed charge initially at ₹5,000
    chg_bed = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.bed,
        source_id=adm.id,
        description="General Bed Stay",
        charge_amount=5000.0,
    )
    db_session.commit()

    # Now add active bed segment with 7 days to trigger repricing during prepare_ipd_bill
    seg = BedStaySegment(
        hospital_id=hospital.id,
        admission_id=adm.id,
        bed_id=uuid4(),
        ward_id=uuid4(),
        rate_per_day=Decimal("1000.00"),
        started_at=adm_start - timedelta(days=2),  # 7 days total = ₹7,000
        ended_at=None,
    )
    db_session.add(seg)
    db_session.commit()

    # Attempting to generate invoice when reconciliation modifies charge raises 409
    create_action = CreateInvoiceAction(db_session, hospital.id)
    with pytest.raises(HTTPException) as exc:
        create_action.execute(
            BillingInvoiceCreate(
                patient_id=patient.id,
                account_id=acc.id,
                charge_ids=[chg_bed.id],
            ),
            user={"name": "Biller"},
        )
    assert exc.value.status_code == 409
    assert "Billing state changed" in exc.value.detail


def test_historical_superseded_invoice_print_settlement_immutable(db_session, hospital, patient):
    """Sections 10, 11, 12, 13:
    1. Charge = ₹10,000.
    2. Invoice A generated at ₹10,000.
    3. ₹4,000 settled.
    4. Invoice A becomes stale & is superseded by Invoice B.
    5. Additional ₹3,000 is settled after supersession.
    6. Print Invoice A -> Amount paid as of supersession = ₹4,000 (NOT ₹7,000).
    7. Print Invoice B / current invoice -> settlement = ₹7,000.
    """
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Comprehensive Health Assessment",
        charge_amount=10000.0,
    )
    db_session.commit()

    # Generate Invoice A
    create_action = CreateInvoiceAction(db_session, hospital.id)
    inv_a = create_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            charge_ids=[chg.id],
        ),
        user={"name": "Biller"},
    )
    db_session.commit()

    # Settle ₹4,000 against charge
    payment_action = CreatePaymentAction(db_session, hospital.id)
    pay1 = payment_action.execute(
        BillingPaymentCreate(
            patient_id=patient.id,
            amount=4000.0,
            payment_method=BillingPaymentMethod.cash,
            allocations=[ChargeAllocationItem(charge_id=chg.id, amount=4000.0)],
        ),
        user={"name": "Cashier"},
    )
    db_session.commit()

    # Reprice charge to make Invoice A stale and legitimately eligible for reissue
    chg.unit_price = Decimal("10500.00")
    chg.total_amount = 10500.0
    chg.net_amount = 10500.0
    db_session.commit()

    # Reissue Invoice A -> creates Invoice B
    reissue_action = ReissueInvoiceAction(db_session, hospital.id)
    inv_b = reissue_action.execute(
        inv_a.id,
        BillingInvoiceReissueRequest(reason="Rate adjustment revision"),
        user={"name": "Biller"},
    )
    db_session.commit()

    # Verify Invoice A is cancelled/superseded
    db_session.refresh(chg)
    old_inv = db_session.query(BillingInvoice).filter(BillingInvoice.id == inv_a.id).first()
    assert old_inv.status == BillingInvoiceStatus.cancelled

    # Afterwards: settle additional ₹3,000 on the same charge
    pay2 = payment_action.execute(
        BillingPaymentCreate(
            patient_id=patient.id,
            amount=3000.0,
            payment_method=BillingPaymentMethod.upi,
            allocations=[ChargeAllocationItem(charge_id=chg.id, amount=3000.0)],
        ),
        user={"name": "Cashier"},
    )
    db_session.commit()

    all_rcpts = db_session.query(BillingReceipt).all()
    # Verify pay1 receipt linked to inv_a, and pay2 receipt linked to inv_b
    r1 = db_session.query(BillingReceipt).filter(BillingReceipt.payment_id == pay1.id).first()
    r2 = db_session.query(BillingReceipt).filter(BillingReceipt.payment_id == pay2.id).first()
    assert r1.linked_invoice_id == inv_a.id
    assert r2.linked_invoice_id == inv_b.id

    # Print Invoice A: historical settlement MUST show ₹4,000 (as-of supersession), NOT ₹7,000
    print_action = PrintInvoiceAction(db_session, hospital.id)
    html_a = print_action.execute(inv_a.id, auto_print=False)
    assert "₹4,000.00" in html_a
    assert "₹7,000.00" not in html_a
    assert "SUPERSEDED" in html_a
    assert "Amount Paid (as of supersession)" in html_a

    # Print Invoice B: current settlement MUST reflect current total ₹7,000
    html_b = print_action.execute(inv_b.id, auto_print=False)
    assert "₹7,000.00" in html_b
    assert "Amount Paid" in html_b


def test_reissue_guardrail_requires_reason_and_controls_paid_invoice(db_session, hospital, patient):
    """Sections 28 & 29:
    Fresh paid invoice CANNOT be reissued even with a valid reason.
    Actually stale invoice can be reissued once source charge changes.
    """
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Minor Service",
        charge_amount=500.0,
    )
    db_session.commit()

    create_action = CreateInvoiceAction(db_session, hospital.id)
    inv = create_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            charge_ids=[chg.id],
        ),
        user={"name": "Biller"},
    )
    db_session.commit()

    # Fully settle the invoice
    payment_action = CreatePaymentAction(db_session, hospital.id)
    pay = payment_action.execute(
        BillingPaymentCreate(
            patient_id=patient.id,
            amount=500.0,
            payment_method=BillingPaymentMethod.cash,
            allocations=[ChargeAllocationItem(charge_id=chg.id, amount=500.0)],
        ),
        user={"name": "Cashier"},
    )
    db_session.commit()

    # Empty reason is rejected with 400
    reissue_action = ReissueInvoiceAction(db_session, hospital.id)
    with pytest.raises(HTTPException) as exc:
        reissue_action.execute(
            inv.id,
            BillingInvoiceReissueRequest(reason=""),
            user={"name": "Biller"},
        )
    assert exc.value.status_code == 400
    assert "reason is required" in exc.value.detail.lower()

    # Fresh paid invoice with valid long reason is REJECTED with 409
    with pytest.raises(HTTPException) as exc:
        reissue_action.execute(
            inv.id,
            BillingInvoiceReissueRequest(reason="Attempting to reissue fresh paid invoice"),
            user={"name": "Biller"},
        )
    assert exc.value.status_code == 409
    assert "current and does not require reissue" in exc.value.detail.lower()

    # Verify reissue eligibility helper directly
    from modules.billing.services.invoice_freshness_service import check_invoice_reissue_eligibility
    inv_orm = db_session.query(BillingInvoice).filter(BillingInvoice.id == inv.id).first()
    eligible, reason = check_invoice_reissue_eligibility(db_session, hospital.id, inv_orm)
    assert eligible is False

    # Now legitimately modify underlying charge (rate/amount change)
    chg.unit_price = Decimal("600.00")
    chg.charge_amount = 600.0
    chg.total_amount = 600.0
    chg.net_amount = 600.0
    db_session.commit()

    db_session.refresh(inv_orm)
    eligible, reason = check_invoice_reissue_eligibility(db_session, hospital.id, inv_orm)
    assert eligible is True
    assert "rate changed" in reason.lower() or "amount changed" in reason.lower()

    # Now reissue with valid reason succeeds!
    inv_reissued = reissue_action.execute(
        inv.id,
        BillingInvoiceReissueRequest(reason="Item code updated for billing accuracy"),
        user={"name": "Biller"},
    )
    db_session.commit()

    assert inv_reissued.invoice_number != inv.invoice_number
    assert inv_reissued.status == BillingInvoiceStatus.generated
    # Old invoice preserved as cancelled/superseded
    db_session.refresh(inv_orm)
    assert inv_orm.status == BillingInvoiceStatus.cancelled
    assert "[Superseded:" in (inv_orm.notes or "")


def test_multiple_invoices_under_same_account_do_not_merge_on_reissue(db_session, hospital, patient):
    """Section 30:
    Invoice A contains c1, c2.
    Then new charges c3, c4 appear on the account.
    c1 is repriced, making Invoice A stale.
    Reissue A: replacement MUST contain only c1, c2, NOT sweeping c3, c4.
    c3, c4 remain uninvoiced.
    """
    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-MULTI-INV",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    c1 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Day 1 Service",
        charge_amount=1000.0,
    )
    c2 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Day 2 Service",
        charge_amount=1000.0,
    )
    db_session.commit()

    create_action = CreateInvoiceAction(db_session, hospital.id)
    # Generate Invoice A for all currently uninvoiced charges on this account (c1, c2)
    inv_a = create_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=acc.id,
            charge_ids=[c1.id, c2.id],
        ),
        user={"name": "Biller"},
    )
    db_session.commit()
    assert len(inv_a.lines) == 2

    # Later: new charges c3 and c4 appear on the same account
    c3 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Day 3 Service",
        charge_amount=1500.0,
    )
    c4 = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Day 4 Service",
        charge_amount=1500.0,
    )
    db_session.commit()

    # Reprice c1, making Invoice A stale
    c1.unit_price = Decimal("1200.00")
    c1.total_amount = 1200.0
    c1.net_amount = 1200.0
    db_session.commit()

    # Reissue Invoice A: replacement MUST contain only c1, c2, without sweeping c3, c4!
    reissue_action = ReissueInvoiceAction(db_session, hospital.id)
    inv_a_replacement = reissue_action.execute(
        inv_a.id,
        BillingInvoiceReissueRequest(reason="Correction of original service rate"),
        user={"name": "Biller"},
    )
    db_session.commit()

    assert len(inv_a_replacement.lines) == 2
    rep_charge_ids = {ln.charge_id for ln in inv_a_replacement.lines}
    assert rep_charge_ids == {c1.id, c2.id}
    assert c3.id not in rep_charge_ids
    assert c4.id not in rep_charge_ids

    # c3 and c4 remain uninvoiced and can now be invoiced together
    inv_b = create_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=acc.id,
            charge_ids=[c3.id, c4.id],
        ),
        user={"name": "Biller"},
    )
    db_session.commit()
    assert len(inv_b.lines) == 2
    b_charge_ids = {ln.charge_id for ln in inv_b.lines}
    assert b_charge_ids == {c3.id, c4.id}


def test_invoice_number_collision_retry_reruns_preparation_correctly(db_session, hospital, patient, monkeypatch):
    """Section 19 & 22.8: Invoice-number collision retry reruns and revalidates
    financial preparation after transaction rollback.
    """
    adm_start = datetime.now(timezone.utc) - timedelta(days=2)
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-COLLISION-01",
        status=AdmissionStatus.admitted,
        admitted_at=adm_start,
    )
    db_session.add(adm)
    db_session.flush()

    seg = BedStaySegment(
        hospital_id=hospital.id,
        admission_id=adm.id,
        bed_id=uuid4(),
        ward_id=uuid4(),
        rate_per_day=Decimal("1200.00"),
        started_at=adm_start,
        ended_at=None,
    )
    db_session.add(seg)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-COLLISION",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    # Pre-reconcile so bed charge exists
    prep = prepare_ipd_bill(db_session, hospital_id=hospital.id, account_id=acc.id)
    db_session.commit()

    bed_charge = db_session.query(BillingCharge).filter(
        BillingCharge.account_id == acc.id,
        BillingCharge.source_type == BillingSourceType.bed,
    ).first()
    assert bed_charge is not None

    # Simulate collision on first commit by monkeypatching commit once
    original_commit = db_session.commit
    first_attempt = True

    def mock_commit():
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            from sqlalchemy.exc import IntegrityError
            raise IntegrityError("mock uq_billing_invoice_number collision", params=None, orig=Exception("uq_billing_invoice_number"))
        return original_commit()

    monkeypatch.setattr(db_session, "commit", mock_commit)

    create_action = CreateInvoiceAction(db_session, hospital.id)
    inv = create_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=acc.id,
            charge_ids=[bed_charge.id],
        ),
        user={"name": "Biller"},
    )

    assert inv is not None
    assert inv.account_id == acc.id
    assert len(inv.lines) == 1
    assert inv.lines[0].charge_id == bed_charge.id


def test_create_invoice_backend_account_validation(db_session, hospital, patient):
    """Section 24: Backend account validation before reconciliation.
    - Non-existent account -> 404
    - Account belonging to different patient -> 400
    """
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="General Clinic Service",
        charge_amount=500.0,
    )
    db_session.commit()

    create_action = CreateInvoiceAction(db_session, hospital.id)

    # 1. Non-existent account -> 404
    with pytest.raises(HTTPException) as exc:
        create_action.execute(
            BillingInvoiceCreate(
                patient_id=patient.id,
                account_id=uuid4(),
                charge_ids=[chg.id],
            ),
            user={"name": "Biller"},
        )
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()

    # 2. Account belonging to another patient -> 400
    other_patient = Patient(
        hospital_id=hospital.id,
        uhid="UHID-OTHER-ACCNT",
        name="Other Patient",
        gender="F",
        mobile="9876543210",
    )
    db_session.add(other_patient)
    db_session.flush()

    other_acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=other_patient.id,
        account_number="IPD-OTHER-001",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        created_by_name="Admin",
    )
    db_session.add(other_acc)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        create_action.execute(
            BillingInvoiceCreate(
                patient_id=patient.id,
                account_id=other_acc.id,
                charge_ids=[chg.id],
            ),
            user={"name": "Biller"},
        )
    assert exc.value.status_code == 400
    assert "does not belong" in exc.value.detail.lower()


def test_general_invoice_while_patient_is_admitted(db_session, hospital, patient):
    """Sections 25 & 27:
    Patient has:
    - Active IPD Account A + BedStaySegment
    - General charge G (account_id = None)
    - IPD charge I (account_id = A)

    Biller explicitly generates General invoice (account_id = None).
    Expected:
    - IPD preparation does NOT run for Account A.
    - Invoice contains G only.
    - account_id is None.
    - The existence of an admission does NOT hijack the general invoice.
    """
    adm_start = datetime.now(timezone.utc) - timedelta(days=3)
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-GEN-TEST-01",
        status=AdmissionStatus.admitted,
        admitted_at=adm_start,
    )
    db_session.add(adm)
    db_session.flush()

    seg = BedStaySegment(
        hospital_id=hospital.id,
        admission_id=adm.id,
        bed_id=uuid4(),
        ward_id=uuid4(),
        rate_per_day=Decimal("1500.00"),
        started_at=adm_start,
        ended_at=None,
    )
    db_session.add(seg)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-GEN-TEST",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    # General charge G (unscoped)
    chg_g = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Outpatient Followup Consultation",
        charge_amount=800.0,
    )
    # IPD charge I (scoped to Account A)
    chg_i = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Inpatient CBC Test",
        charge_amount=600.0,
    )
    db_session.commit()

    # Verify no bed charge exists yet for Account A
    bed_charges_before = db_session.query(BillingCharge).filter(
        BillingCharge.account_id == acc.id,
        BillingCharge.source_type == BillingSourceType.bed,
    ).all()
    assert len(bed_charges_before) == 0

    create_action = CreateInvoiceAction(db_session, hospital.id)
    inv = create_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=None,
            charge_ids=[chg_g.id],
        ),
        user={"name": "Biller"},
    )
    db_session.commit()

    # General invoice created cleanly
    assert inv.account_id is None
    assert inv.grand_total == 800.0
    assert len(inv.lines) == 1
    assert inv.lines[0].charge_id == chg_g.id

    # Bed preparation was NOT run for Account A (no bed charge was minted)
    bed_charges_after = db_session.query(BillingCharge).filter(
        BillingCharge.account_id == acc.id,
        BillingCharge.source_type == BillingSourceType.bed,
    ).all()
    assert len(bed_charges_after) == 0

    # IPD charge I remains uninvoiced
    db_session.refresh(chg_i)
    assert chg_i.status == BillingChargeStatus.pending


def test_immediate_partial_settlement_response_on_create_and_reissue(db_session, hospital, patient):
    """Sections 17, 19 & 31:
    Create and Reissue responses must immediately return authoritative
    amount_paid and balance_due without requiring a subsequent GET.
    """
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-PARTIAL-01",
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-PARTIAL",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    # Collect ₹10,000 IPD Advance
    dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=10000.0,
        deposit_date=date.today(),
        deposit_type="ipd_advance",
        admission_id=adm.id,
        account_id=acc.id,
    )
    db_session.commit()

    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Major Procedure",
        charge_amount=15500.0,
    )
    db_session.commit()

    # Draw down ₹10,000 advance against the charge
    drawn = draw_down_deposits_bulk_for_account(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
    )
    db_session.commit()
    assert drawn == 10000.0

    db_session.refresh(chg)
    assert chg.status == BillingChargeStatus.partially_paid
    assert float(chg.amount_paid) == 10000.0

    # 1. CreateInvoiceAction response directly
    create_action = CreateInvoiceAction(db_session, hospital.id)
    inv_resp = create_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=acc.id,
            charge_ids=[chg.id],
        ),
        user={"name": "Biller"},
    )
    db_session.commit()

    # Assert directly on returned response DTO without any secondary GET
    assert inv_resp.grand_total == 15500.0
    assert inv_resp.amount_paid == 10000.0
    assert inv_resp.balance_due == 5500.0

    # 2. Make charge stale by legitimately repricing to ₹16,500
    chg.charge_amount = 16500.0
    chg.unit_price = Decimal("16500.00")
    chg.total_amount = 16500.0
    chg.net_amount = 16500.0
    db_session.commit()

    # ReissueInvoiceAction response directly
    reissue_action = ReissueInvoiceAction(db_session, hospital.id)
    reissue_resp = reissue_action.execute(
        inv_resp.id,
        BillingInvoiceReissueRequest(reason="Procedural consumables adjustment"),
        user={"name": "Biller"},
    )
    db_session.commit()

    # Assert directly on returned reissue response DTO without secondary GET
    assert reissue_resp.grand_total == 16500.0
    assert reissue_resp.amount_paid == 10000.0
    assert reissue_resp.balance_due == 6500.0


def test_reissue_number_collision_retry_reloads_fresh_state(db_session, hospital, patient, monkeypatch):
    """Sections 20 & 32: Reissue collision retry reloads fresh DB state and charges."""
    chg = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        source_id=uuid4(),
        description="Therapy Session",
        charge_amount=2500.0,
    )
    db_session.commit()

    create_action = CreateInvoiceAction(db_session, hospital.id)
    inv = create_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            charge_ids=[chg.id],
        ),
        user={"name": "Biller"},
    )
    db_session.commit()

    # Make stale by changing charge amount
    chg.charge_amount = 3000.0
    chg.unit_price = Decimal("3000.00")
    chg.total_amount = 3000.0
    chg.net_amount = 3000.0
    db_session.commit()

    # Monkeypatch commit once on reissue to simulate collision
    original_commit = db_session.commit
    first_attempt = True

    def mock_commit():
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            from sqlalchemy.exc import IntegrityError
            raise IntegrityError("mock uq_billing_invoice_number collision", params=None, orig=Exception("uq_billing_invoice_number"))
        return original_commit()

    monkeypatch.setattr(db_session, "commit", mock_commit)

    reissue_action = ReissueInvoiceAction(db_session, hospital.id)
    reissued_inv = reissue_action.execute(
        inv.id,
        BillingInvoiceReissueRequest(reason="Therapy tier repriced"),
        user={"name": "Biller"},
    )

    assert reissued_inv is not None
    assert reissued_inv.grand_total == 3000.0
    assert len(reissued_inv.lines) == 1
    assert reissued_inv.lines[0].charge_id == chg.id




