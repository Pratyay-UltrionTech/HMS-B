"""Tests for Bed Billing reconciliation across transfers and Invoice Freshness verification."""

from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingInvoiceStatus,
    BillingPaymentMethod,
    BillingSourceType,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
)
from modules.billing.services.billing_service import (
    ensure_bed_charge_for_admission,
    ensure_charge,
)
from modules.billing.services.invoice_freshness_service import verify_invoice_freshness
from modules.billing.services.invoice_service import create_invoice_from_charges
from modules.billing.services.billing_service import create_payment
from modules.billing.actions.billing_actions import (
    CreateInvoiceAction,
    PrepareIpdBillAction,
    ReissueInvoiceAction,
)
from modules.billing.contracts.billing_contracts import (
    BillingInvoiceCreate,
    BillingInvoiceReissueRequest,
)
from modules.inpatient.actions.admission_actions import (
    DischargePatientAction,
    DischargeRequest,
)
from modules.inpatient.entities.admission import (
    Admission,
    AdmissionStatus,
    BedStaySegment,
)
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from shared.exceptions import ValidationError
from tests.conftest import db_session, hospital, patient


def test_bed_transfer_calculates_segmented_rates_correctly(
    db_session: Session, hospital: Hospital, patient: Patient
):
    """Scenario:
    Bed A for 2 days at ₹1,000/day
    Transfer to Bed B for 3 days at ₹2,500/day
    Total expected = 2 * 1,000 + 3 * 2,500 = ₹9,500 (5 days total)
    """
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-SEG-001",
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-SEG001",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    # Segment 1: Bed A, days 0 to 2 (2 days) at 1000/day
    seg1_start = adm.admitted_at
    seg1_end = adm.admitted_at + timedelta(days=2)
    s1 = BedStaySegment(
        hospital_id=hospital.id,
        admission_id=adm.id,
        bed_id=uuid4(),
        ward_id=uuid4(),
        rate_per_day=Decimal("1000.00"),
        started_at=seg1_start,
        ended_at=seg1_end,
    )
    # Segment 2: Bed B, days 2 to 5 (3 days) at 2500/day
    seg2_start = seg1_end
    seg2_end = adm.admitted_at + timedelta(days=5)
    s2 = BedStaySegment(
        hospital_id=hospital.id,
        admission_id=adm.id,
        bed_id=uuid4(),
        ward_id=uuid4(),
        rate_per_day=Decimal("2500.00"),
        started_at=seg2_start,
        ended_at=seg2_end,
    )
    db_session.add_all([s1, s2])
    db_session.commit()

    charge = ensure_bed_charge_for_admission(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        admission_id=adm.id,
        admitted_at=adm.admitted_at,
        discharged_at=seg2_end,
    )
    db_session.commit()

    assert charge is not None
    # 2 * 1000 + 3 * 2500 = 2000 + 7500 = 9500
    assert float(charge.charge_amount) == 9500.0
    assert float(charge.quantity) == 5.0
    assert "multi_segment" in charge.notes


def test_stale_invoice_detected_after_bed_charge_repricing(
    db_session: Session, hospital: Hospital, patient: Patient
):
    """Critical P0 verification:
    1. Bed charge ID BED-1 = ₹6,000.
    2. Invoice generated snapshots ₹6,000.
    3. Patient stay is reconciled/repriced to ₹7,500 (same charge ID).
    4. verify_invoice_freshness must detect invoice is STALE.
    5. Discharge must be blocked.
    """
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-STALE-001",
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc) - timedelta(days=4),
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-STALE01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    # Initial bed charge: 4 days @ 1500 = 6000
    bed_charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.bed,
        source_id=adm.id,
        description="Bed Charges (4 Days)",
        quantity=4.0,
        unit_price=1500.0,
        charge_amount=6000.0,
    )
    db_session.commit()

    # Snapshot into invoice
    invoice = create_invoice_from_charges(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        charge_ids=[bed_charge.id],
        account_id=acc.id,
        created_by_name="Biller",
    )
    db_session.commit()

    # Invoice is fresh initially
    is_fresh, reason = verify_invoice_freshness(db_session, hospital.id, invoice)
    assert is_fresh is True
    assert reason is None

    # Patient stays an extra day: bed charge is updated to 5 days @ 1500 = 7500
    bed_charge.quantity = 5.0
    bed_charge.charge_amount = 7500.0
    bed_charge.net_amount = 7500.0
    db_session.commit()

    # Freshness check must now detect the stale invoice despite identical charge ID!
    is_fresh, reason = verify_invoice_freshness(db_session, hospital.id, invoice)
    assert is_fresh is False
    assert "quantity changed" in reason.lower() or "amount changed" in reason.lower()

    # Attempting discharge must raise ValidationError
    adm.status = AdmissionStatus.discharge_requested
    db_session.commit()
    discharge_action = DischargePatientAction(db_session)
    with pytest.raises(ValidationError) as exc:
        discharge_action.execute(
            hospital.id,
            DischargeRequest(admission_id=adm.id, no_discharge_meds=True),
            actor={"name": "Nurse"},
        )
    assert "Final bill out of date" in str(exc.value)


def test_active_inpatient_bed_billing_before_discharge(
    db_session: Session, hospital: Hospital, patient: Patient
):
    """Scenario (Requirement 23):
    1. Admit patient.
    2. Assign bed with ₹2,000/day rate.
    3. Keep admission ACTIVE.
    4. Accrue 3 billable days.
    5. Do NOT request discharge.
    6. Prepare IPD bill from Billing.
    7. Verify bed charge = correct 3-day accrued amount (₹6,000).
    8. Generate invoice.
    9. Verify invoice contains bed charge.
    """
    adm_start = datetime.now(timezone.utc) - timedelta(days=3)
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-ACT-001",
        status=AdmissionStatus.admitted,
        admitted_at=adm_start,
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-ACT01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    s = BedStaySegment(
        hospital_id=hospital.id,
        admission_id=adm.id,
        bed_id=uuid4(),
        ward_id=uuid4(),
        rate_per_day=Decimal("2000.00"),
        started_at=adm_start,
        ended_at=None,  # active stay
    )
    db_session.add(s)
    db_session.commit()

    # Step 6: Prepare IPD bill from Billing
    prep_action = PrepareIpdBillAction(db_session, hospital.id)
    bill_prep = prep_action.execute(acc.id, user={"name": "Biller"}, as_of=datetime.now(timezone.utc))

    # Step 7: Verify bed charge = 3 days @ 2000 = 6000
    assert bill_prep.total_charges == 6000.0
    assert len(bill_prep.charges) == 1
    bed_charge_dict = bill_prep.charges[0]
    assert float(bed_charge_dict.charge_amount) == 6000.0
    assert float(bed_charge_dict.quantity) == 3.0

    # Step 8: Generate invoice
    inv_action = CreateInvoiceAction(db_session, hospital.id)
    inv_res = inv_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=acc.id,
            charge_ids=[bed_charge_dict.id],
        ),
        user={"name": "Biller"},
    )

    # Step 9: Verify invoice contains bed charge
    assert inv_res.grand_total == 6000.0
    assert len(inv_res.lines) == 1
    assert inv_res.lines[0].charge_id == bed_charge_dict.id
    assert inv_res.lines[0].amount == 6000.0


def test_stale_invoice_recovery_and_reissue_end_to_end(
    db_session: Session, hospital: Hospital, patient: Patient
):
    """Scenario (Requirement 24):
    1. Generate IPD invoice with bed charge ₹6,000.
    2. Bed stay increases → current bed charge ₹7,500.
    3. Discharge correctly fails stale-invoice validation.
    4. Run the real reissue/supersede operation.
    5. Old invoice remains auditable (cancelled status with reason).
    6. New invoice contains ₹7,500 bed charge.
    7. Existing payment allocations are not duplicated.
    8. Discharge financial validation now evaluates replacement invoice.
    9. Settle balance; discharge succeeds.
    """
    adm_start = datetime.now(timezone.utc) - timedelta(days=5)
    adm = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ip_id="IP-REISSUE-001",
        status=AdmissionStatus.admitted,
        admitted_at=adm_start,
    )
    db_session.add(adm)
    db_session.flush()

    acc = FinancialAccount(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_number="IPD-ACC-REISSUE01",
        account_type=FinancialAccountType.ipd,
        status=FinancialAccountStatus.open,
        admission_id=adm.id,
        created_by_name="Admin",
    )
    db_session.add(acc)
    db_session.flush()

    # Step 1: 4 days initial stay = ₹6,000
    bed_charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=acc.id,
        source_type=BillingSourceType.bed,
        source_id=adm.id,
        description="Bed Charges (4 Days)",
        quantity=4.0,
        unit_price=1500.0,
        charge_amount=6000.0,
    )
    db_session.commit()

    inv_action = CreateInvoiceAction(db_session, hospital.id)
    old_inv = inv_action.execute(
        BillingInvoiceCreate(
            patient_id=patient.id,
            account_id=acc.id,
            charge_ids=[bed_charge.id],
        ),
        user={"name": "Biller"},
    )
    assert old_inv.grand_total == 6000.0

    # Pay initial ₹6,000
    pay1 = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=6000.0,
        account_id=acc.id,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes="Initial payment",
        received_by_name="Cashier",
        allocate=True,
    )
    db_session.commit()

    # Step 2: Patient stay increases to 5 days → ₹7,500
    bed_charge.quantity = 5.0
    bed_charge.charge_amount = 7500.0
    bed_charge.net_amount = 7500.0
    db_session.commit()

    # Step 3: Discharge fails stale-invoice check
    adm.status = AdmissionStatus.discharge_requested
    db_session.commit()
    discharge_action = DischargePatientAction(db_session)
    with pytest.raises(ValidationError) as exc:
        discharge_action.execute(
            hospital.id,
            DischargeRequest(admission_id=adm.id, no_discharge_meds=True),
            actor={"name": "Nurse"},
        )
    assert "Final bill out of date" in str(exc.value)

    # Step 4: Run the real reissue/supersede operation
    reissue_action = ReissueInvoiceAction(db_session, hospital.id)
    new_inv_dto = reissue_action.execute(
        old_inv.id,
        BillingInvoiceReissueRequest(reason="Bed stay extended by 1 day"),
        user={"name": "Biller"},
    )

    # Step 5: Old invoice remains auditable (cancelled status)
    from modules.billing.entities.billing_entities import BillingInvoice
    old_inv_db = db_session.query(BillingInvoice).filter(BillingInvoice.id == old_inv.id).first()
    assert old_inv_db.status == BillingInvoiceStatus.cancelled
    assert "Superseded" in old_inv_db.notes

    # Step 6: New invoice contains ₹7,500 bed charge
    assert new_inv_dto.grand_total == 7500.0
    assert len(new_inv_dto.lines) == 1
    assert new_inv_dto.lines[0].amount == 7500.0

    # Step 7: Existing payment allocation ₹6,000 is preserved, outstanding balance is ₹1,500
    db_session.refresh(bed_charge)
    assert float(bed_charge.amount_paid) == 6000.0
    remaining_due = float(bed_charge.net_amount) - float(bed_charge.amount_paid)
    assert remaining_due == 1500.0

    # Pay remaining ₹1,500
    pay2 = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=1500.0,
        account_id=acc.id,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes="Settlement payment",
        received_by_name="Cashier",
        allocate=True,
    )
    db_session.commit()
    db_session.refresh(bed_charge)
    assert float(bed_charge.amount_paid) == 7500.0

    # Step 8 & 9: Discharge succeeds now that invoice is fresh and balance is 0
    discharge_res = discharge_action.execute(
        hospital.id,
        DischargeRequest(admission_id=adm.id, no_discharge_meds=True),
        actor={"name": "Nurse"},
    )
    assert discharge_res.status == AdmissionStatus.discharged
