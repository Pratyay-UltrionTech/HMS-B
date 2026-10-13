"""
Integration tests for Bed Occupancy & Discharge Clearance Invariants (Phase 4, XMOD-008, XMOD-016).

Verifies:
1. Discharge is blocked when unpaid charges exist and no exception is approved.
2. Discharge succeeds when an approved financial clearance exception exists.
3. Discharge succeeds when account is fully paid / zero balance.
4. Bed occupancy (Bed.is_occupied) and BedStaySegment are freed/closed on discharge.
5. Bed occupancy and BedStaySegments are freed/closed on admission cancellation.
6. FinancialClearanceService can_discharge_admission accurately evaluates state.
"""

from datetime import date, datetime, time, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session

from modules.beds.entities.bed import Bed, Room, Ward
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
    BillingPayment,
    BillingSourceType,
    FinancialAccount,
    FinancialAccountStatus,
)
from modules.billing.services.financial_clearance_service import FinancialClearanceService
from modules.clinical_records.entities.clinical_record import Prescription
from modules.inpatient.actions.admission_actions import (
    CancelAdmissionAction,
    DischargePatientAction,
)
from modules.inpatient.contracts.inpatient_contracts import (
    DischargeFinancialExceptionCreate,
    DischargeFinancialExceptionDecision,
    DischargeRequest,
    ExceptionStatus,
)
from modules.inpatient.db.discharge_exception_repository import DischargeExceptionRepository
from modules.inpatient.entities.admission import (
    Admission,
    AdmissionStatus,
    BedStaySegment,
    IpdFormSubmission,
    IpdFormSubmissionStatus,
)
from modules.patients.entities.patient import Patient, PatientStatus
from shared.exceptions.base import ValidationError

ACTOR = {"name": "Staff Clinician", "role": "doctor", "doctor_id": str(uuid4())}


def _setup_ipd_environment(db_session: Session, hospital):
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid=f"UHID-{uuid4().hex[:6].upper()}",
        first_name="Arthur",
        last_name="Dent",
        name="Arthur Dent",
        gender="male",
        mobile="9876543299",
        status=PatientStatus.admitted,
    )
    db_session.add(patient)

    ward = Ward(
        id=uuid4(),
        hospital_id=hospital.id,
        name="General Ward",
        ward_type="general",
        bed_charge_per_day=Decimal("1500.00"),
        is_active=True,
    )
    db_session.add(ward)

    room = Room(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_code="GW-101",
        bed_count=2,
        is_active=True,
    )
    db_session.add(room)

    bed = Bed(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="Bed-1",
        is_occupied=True,
        is_active=True,
    )
    db_session.add(bed)

    admission = Admission(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        status=AdmissionStatus.discharge_requested,
        admitted_at=datetime.now(timezone.utc),
        no_discharge_meds=True,
    )
    db_session.add(admission)

    stay_segment = BedStaySegment(
        id=uuid4(),
        hospital_id=hospital.id,
        admission_id=admission.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        rate_per_day=1500.0,
        started_at=datetime.now(timezone.utc),
        ended_at=None,
    )
    db_session.add(stay_segment)

    account = FinancialAccount(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        admission_id=admission.id,
        account_number=f"ACC-{uuid4().hex[:8].upper()}",
        account_type="ipd",
        status=FinancialAccountStatus.open,
    )
    db_session.add(account)

    db_session.commit()
    return patient, ward, room, bed, admission, account


def _setup_invoice_with_charge(db_session: Session, hospital, patient, admission, account, amount: float, is_paid: bool = False):
    charge = BillingCharge(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        source_type=BillingSourceType.bed,
        source_id=admission.id,
        description="Bed stay 1 day",
        charge_amount=Decimal(str(amount)),
        net_amount=Decimal(str(amount)),
        amount_paid=Decimal(str(amount)) if is_paid else Decimal("0.00"),
        quantity=Decimal("1"),
        unit_price=Decimal(str(amount)),
        status=BillingChargeStatus.paid if is_paid else BillingChargeStatus.pending,
        created_by_name="Test",
    )
    db_session.add(charge)

    invoice = BillingInvoice(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        invoice_number=f"INV-{uuid4().hex[:6].upper()}",
        invoice_date=date.today(),
        subtotal=Decimal(str(amount)),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        grand_total=Decimal(str(amount)),
        status=BillingInvoiceStatus.paid if is_paid else BillingInvoiceStatus.generated,
        created_by_name="Test",
    )
    db_session.add(invoice)

    line = BillingInvoiceLine(
        id=uuid4(),
        hospital_id=hospital.id,
        invoice_id=invoice.id,
        charge_id=charge.id,
        source_type="bed",
        description="Bed stay 1 day",
        amount=Decimal(str(amount)),
        quantity=Decimal("1"),
        rate=Decimal(str(amount)),
    )
    db_session.add(line)
    db_session.commit()
    return charge, invoice


def test_discharge_blocked_when_unpaid_charges_exist(db_session: Session, hospital):
    patient, ward, room, bed, admission, account = _setup_ipd_environment(db_session, hospital)
    _setup_invoice_with_charge(db_session, hospital, patient, admission, account, amount=1500.0, is_paid=False)

    fin_svc = FinancialClearanceService(db_session)
    assert fin_svc.can_discharge_admission(hospital.id, admission.id) is False

    with pytest.raises(ValidationError, match="outstanding balance"):
        DischargePatientAction(db_session).execute(
            hospital.id,
            DischargeRequest(
                admission_id=admission.id,
                discharge_date=date.today(),
                discharge_time=time(12, 0),
                no_discharge_meds=True,
            ),
            ACTOR,
        )

    # State must remain untouched
    db_session.refresh(bed)
    db_session.refresh(admission)
    assert bed.is_occupied is True
    assert admission.status == AdmissionStatus.discharge_requested


def test_discharge_succeeds_with_approved_financial_exception(db_session: Session, hospital):
    patient, ward, room, bed, admission, account = _setup_ipd_environment(db_session, hospital)
    _setup_invoice_with_charge(db_session, hospital, patient, admission, account, amount=1500.0, is_paid=False)

    # Create approved financial exception for this admission
    requester_id = uuid4()
    approver_id = uuid4()
    exc_repo = DischargeExceptionRepository(db_session, hospital.id)
    exc = exc_repo.create_exception(
        admission_id=admission.id,
        financial_account_id=account.id,
        patient_id=patient.id,
        outstanding_amount=1500.0,
        reason="Charity care approval by medical superintendent",
        recommendation=None,
        requested_by_id=requester_id,
        requested_by_name="Dr. Requester",
    )
    exc_repo.decide_exception(
        exception_id=exc.id,
        decided_by_id=approver_id,
        decided_by_name="Superintendent Approver",
        decision=ExceptionStatus.approved,
        remarks="Approved by Hospital Admin",
    )

    fin_svc = FinancialClearanceService(db_session)
    res = fin_svc.evaluate_discharge_clearance(hospital.id, admission.id)
    assert res.can_discharge is True
    assert res.status == "exception_approved"

    # Now discharge must succeed
    result = DischargePatientAction(db_session).execute(
        hospital.id,
        DischargeRequest(
            admission_id=admission.id,
            discharge_date=date.today(),
            discharge_time=time(12, 0),
            no_discharge_meds=True,
        ),
        ACTOR,
    )

    assert result.status == AdmissionStatus.discharged
    db_session.refresh(bed)
    assert bed.is_occupied is False

    # Check that open stay segment is closed
    segments = (
        db_session.query(BedStaySegment)
        .filter(BedStaySegment.admission_id == admission.id)
        .all()
    )
    assert len(segments) >= 1
    for seg in segments:
        assert seg.ended_at is not None


def test_discharge_succeeds_when_fully_paid(db_session: Session, hospital):
    patient, ward, room, bed, admission, account = _setup_ipd_environment(db_session, hospital)
    _setup_invoice_with_charge(db_session, hospital, patient, admission, account, amount=1500.0, is_paid=True)

    fin_svc = FinancialClearanceService(db_session)
    assert fin_svc.can_discharge_admission(hospital.id, admission.id) is True

    result = DischargePatientAction(db_session).execute(
        hospital.id,
        DischargeRequest(
            admission_id=admission.id,
            discharge_date=date.today(),
            discharge_time=time(12, 0),
            no_discharge_meds=True,
        ),
        ACTOR,
    )

    assert result.status == AdmissionStatus.discharged
    db_session.refresh(bed)
    assert bed.is_occupied is False


def test_cancel_admission_frees_bed_and_closes_segments(db_session: Session, hospital):
    patient, ward, room, bed, admission, account = _setup_ipd_environment(db_session, hospital)

    CancelAdmissionAction(db_session).execute(
        hospital.id,
        admission.id,
        reason="Patient opted for home care prior to surgery",
        actor=ACTOR,
    )

    db_session.refresh(admission)
    db_session.refresh(bed)
    assert admission.status == AdmissionStatus.cancelled
    assert bed.is_occupied is False

    segments = (
        db_session.query(BedStaySegment)
        .filter(BedStaySegment.admission_id == admission.id)
        .all()
    )
    assert len(segments) >= 1
    for seg in segments:
        assert seg.ended_at is not None
