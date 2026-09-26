"""
Automated tests for IPD Admission Advance / Deposit Workflow:
- IPD financial account created immediately on admission request (status=requested)
- Admission request does not require payment at time of request
- Required advance baseline calculation when bed is not yet selected
- BillingDeposit linked to correct IPD financial account
- Partial deposit reduces shortfall but bed allocation remains blocked
- Subsequent deposit clears shortfall and bed allocation succeeds
- Over-deposit remains available balance in ledger (not truncated)
- Other admission/patient deposits do not clear this admission
- Emergency override allows bed allocation without advance deposit (balance receivable)
- Normal financial exception allows bed allocation while distinguishing authorization from accounting
- Deposit creation stages admission sync event for real-time nurse queue refresh
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from shared.audit.entities.audit_log import AuditLog
from modules.beds.entities.bed import Bed, Room, Ward
from modules.billing.actions.billing_actions import CreateDepositAction
from modules.billing.contracts.billing_contracts import BillingDepositCreate, BillingPaymentMethod
from modules.billing.contracts.exception_contracts import (
    EmergencyOverrideRequest,
    FinancialExceptionDecision,
    FinancialExceptionRequest,
)
from modules.billing.entities.billing_entities import (
    BillingDeposit,
    DepositStatus,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
)
from modules.billing.entities.financial_exception import FinancialExceptionStatus
from modules.billing.services.financial_exception_service import FinancialExceptionService
from modules.billing.services.service_financial_clearance import evaluate_bed_allocation_clearance
from modules.doctors.actions.doctor_appointment_actions import DoctorRequestIpdAdmissionAction
from modules.doctors.contracts.doctor_contracts import DoctorIpdAdmissionRequest
from modules.doctors.entities.doctor import HospitalUser
from modules.inpatient.actions.admission_lifecycle_actions import AcceptAdmissionAction
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital


@pytest.fixture
def workflow_setup(db_session: Session, hospital: Hospital, doctor: HospitalUser, patient: Patient):
    ward = Ward(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="General Medical Ward",
        ward_type="general",
        admission_fee=500.0,
        bed_charge_per_day=1500.0,
        is_active=True,
    )
    db_session.add(ward)

    room = Room(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_code="R-101",
        is_active=True,
    )
    db_session.add(room)

    bed = Bed(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="B-101",
        is_occupied=False,
        is_active=True,
    )
    db_session.add(bed)

    db_session.commit()
    return {
        "hospital": hospital,
        "doctor": doctor,
        "patient": patient,
        "ward": ward,
        "room": room,
        "bed": bed,
    }


def test_ipd_financial_account_created_immediately_on_request(db_session: Session, workflow_setup: dict):
    h = workflow_setup["hospital"]
    doc = workflow_setup["doctor"]
    pat = workflow_setup["patient"]

    req = DoctorIpdAdmissionRequest(
        clinical_reason="Acute appendicitis requiring surgery",
        admission_notes="NPO after midnight",
    )
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. Sarah"}

    action = DoctorRequestIpdAdmissionAction(db_session)
    resp = action.execute(h.id, doc.id, pat.id, req, user_ctx)

    assert resp.status == "requested"
    assert resp.financial_account_id is not None

    account = (
        db_session.query(FinancialAccount)
        .filter(
            FinancialAccount.hospital_id == h.id,
            FinancialAccount.patient_id == pat.id,
            FinancialAccount.account_type == FinancialAccountType.ipd,
            FinancialAccount.status == FinancialAccountStatus.open,
        )
        .first()
    )
    assert account is not None
    assert str(account.id) == str(resp.financial_account_id)
    assert account.admission_id == resp.admission_id


def test_required_advance_baseline_estimation_without_bed_selected(db_session: Session, workflow_setup: dict):
    h = workflow_setup["hospital"]
    doc = workflow_setup["doctor"]
    pat = workflow_setup["patient"]

    req = DoctorIpdAdmissionRequest(clinical_reason="Observation")
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. Sarah"}
    resp = DoctorRequestIpdAdmissionAction(db_session).execute(h.id, doc.id, pat.id, req, user_ctx)

    # Admission created without ward_id or bed_id yet
    clearance = evaluate_bed_allocation_clearance(db_session, h.id, resp.admission_id, ward_id=None, bed_id=None)

    # Ward has admission_fee=500, bed_charge_per_day=1500 -> total required advance = 2000
    assert clearance.required_advance == 2000.0
    assert clearance.shortfall == 2000.0
    assert clearance.is_cleared is False
    assert clearance.status == "payment_required"
    assert "estimated" in clearance.reason.lower() or "advance" in clearance.reason.lower()


def test_deposit_linked_to_correct_ipd_account_and_reduces_shortfall(db_session: Session, workflow_setup: dict):
    h = workflow_setup["hospital"]
    doc = workflow_setup["doctor"]
    pat = workflow_setup["patient"]
    ward = workflow_setup["ward"]
    bed = workflow_setup["bed"]

    # 1. Doctor requests IPD admission
    req = DoctorIpdAdmissionRequest(clinical_reason="Evaluation")
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. Sarah"}
    resp = DoctorRequestIpdAdmissionAction(db_session).execute(h.id, doc.id, pat.id, req, user_ctx)
    admission_id = resp.admission_id
    account_id = resp.financial_account_id

    # 2. Billing cashier collects partial advance (e.g. ₹1,200 out of ₹2,000)
    cashier_user = {"user_id": str(uuid.uuid4()), "name": "Cashier Rahul"}
    dep_payload = BillingDepositCreate(
        patient_id=pat.id,
        admission_id=admission_id,
        amount=1200.0,
        payment_method=BillingPaymentMethod.cash,
        deposit_type="ipd_advance",
        notes="Partial advance deposit at counter",
    )
    dep_resp = CreateDepositAction(db_session, h.id).execute(dep_payload, cashier_user)

    # Verify BillingDeposit entity
    deposit_row = db_session.query(BillingDeposit).filter(BillingDeposit.id == dep_resp.id).first()
    assert deposit_row is not None
    assert str(deposit_row.account_id) == str(account_id)
    assert deposit_row.admission_id == admission_id
    assert deposit_row.status == DepositStatus.available
    assert deposit_row.available_amount == 1200.0

    # 3. Clearance recheck: shortfall reduced to 800, still blocked
    clearance = evaluate_bed_allocation_clearance(db_session, h.id, admission_id, ward_id=ward.id, bed_id=bed.id)
    assert clearance.required_advance == 2000.0
    assert clearance.available_deposit == 1200.0
    assert clearance.shortfall == 800.0
    assert clearance.is_cleared is False

    # 4. Nurse attempt to allocate bed with partial advance must fail
    nurse_user = {"user_id": str(uuid.uuid4()), "name": "Nurse Priya"}
    accept_action = AcceptAdmissionAction(db_session)
    with pytest.raises(HTTPException) as exc_info:
        accept_action.execute(
            hospital_id=h.id,
            actor=nurse_user,
            admission_id=admission_id,
            ward_id=ward.id,
            room_id=workflow_setup["room"].id,
            bed_id=bed.id,
        )
    assert exc_info.value.status_code == 402
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail.get("shortfall") == 800.0
    assert "800" in detail.get("message", "") or "Shortfall" in detail.get("message", "")

    # 5. Billing cashier collects the remaining ₹800
    dep_payload_2 = BillingDepositCreate(
        patient_id=pat.id,
        admission_id=admission_id,
        amount=800.0,
        payment_method=BillingPaymentMethod.upi,
        deposit_type="ipd_advance",
        reference_number="UPI-884920",
    )
    CreateDepositAction(db_session, h.id).execute(dep_payload_2, cashier_user)

    # 6. Clearance automatically recomputes to financially cleared
    clearance_after = evaluate_bed_allocation_clearance(db_session, h.id, admission_id, ward_id=ward.id, bed_id=bed.id)
    assert clearance_after.required_advance == 2000.0
    assert clearance_after.available_deposit == 2000.0
    assert clearance_after.shortfall == 0.0
    assert clearance_after.is_cleared is True
    assert clearance_after.status == "financially_cleared"

    # 7. Nurse now allocates bed - succeeds
    accepted = accept_action.execute(
        hospital_id=h.id,
        actor=nurse_user,
        admission_id=admission_id,
        ward_id=ward.id,
        room_id=workflow_setup["room"].id,
        bed_id=bed.id,
    )
    assert accepted.status == AdmissionStatus.admitted
    assert accepted.bed_id == bed.id
    db_session.refresh(bed)
    assert bed.is_occupied is True


def test_over_deposit_remains_as_available_deposit_balance(db_session: Session, workflow_setup: dict):
    h = workflow_setup["hospital"]
    doc = workflow_setup["doctor"]
    pat = workflow_setup["patient"]
    ward = workflow_setup["ward"]
    bed = workflow_setup["bed"]

    req = DoctorIpdAdmissionRequest(clinical_reason="Surgery")
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. Sarah"}
    resp = DoctorRequestIpdAdmissionAction(db_session).execute(h.id, doc.id, pat.id, req, user_ctx)

    # Cashier collects 5,000 (required is 2,000)
    cashier_user = {"user_id": str(uuid.uuid4()), "name": "Cashier Rahul"}
    dep_payload = BillingDepositCreate(
        patient_id=pat.id,
        admission_id=resp.admission_id,
        amount=5000.0,
        payment_method=BillingPaymentMethod.card,
        deposit_type="ipd_advance",
    )
    dep_resp = CreateDepositAction(db_session, h.id).execute(dep_payload, cashier_user)

    clearance = evaluate_bed_allocation_clearance(db_session, h.id, resp.admission_id, ward_id=ward.id, bed_id=bed.id)
    assert clearance.required_advance == 2000.0
    assert clearance.available_deposit == 5000.0
    assert clearance.shortfall == 0.0
    assert clearance.is_cleared is True

    # Available deposit balance in ledger is 5000 (not truncated)
    deposit_row = db_session.query(BillingDeposit).filter(BillingDeposit.id == dep_resp.id).first()
    assert deposit_row.available_amount == 5000.0


def test_deposit_from_unrelated_admission_does_not_clear_this_admission(
    db_session: Session, workflow_setup: dict
):
    h = workflow_setup["hospital"]
    doc = workflow_setup["doctor"]
    pat = workflow_setup["patient"]
    ward = workflow_setup["ward"]
    bed = workflow_setup["bed"]

    # Admission A
    reqA = DoctorIpdAdmissionRequest(clinical_reason="Stay A")
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. Sarah"}
    respA = DoctorRequestIpdAdmissionAction(db_session).execute(h.id, doc.id, pat.id, reqA, user_ctx)

    # Create another unrelated admission B
    admB = Admission(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        doctor_id=doc.id,
        status=AdmissionStatus.discharged,
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admB)
    db_session.commit()

    # Deposit paid on admission B
    cashier_user = {"user_id": str(uuid.uuid4()), "name": "Cashier Rahul"}
    depB = BillingDepositCreate(
        patient_id=pat.id,
        admission_id=admB.id,
        amount=5000.0,
        payment_method=BillingPaymentMethod.cash,
        deposit_type="ipd_advance",
    )
    CreateDepositAction(db_session, h.id).execute(depB, cashier_user)

    # Admission A clearance must NOT see admission B's deposit
    clearanceA = evaluate_bed_allocation_clearance(db_session, h.id, respA.admission_id, ward_id=ward.id, bed_id=bed.id)
    assert clearanceA.available_deposit == 0.0
    assert clearanceA.shortfall == 2000.0
    assert clearanceA.is_cleared is False


def test_emergency_override_allows_bed_allocation_without_deposit(db_session: Session, workflow_setup: dict):
    h = workflow_setup["hospital"]
    doc = workflow_setup["doctor"]
    pat = workflow_setup["patient"]
    ward = workflow_setup["ward"]
    bed = workflow_setup["bed"]

    # 1. Doctor requests IPD admission
    req = DoctorIpdAdmissionRequest(clinical_reason="Massive trauma")
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. Sarah"}
    resp = DoctorRequestIpdAdmissionAction(db_session).execute(h.id, doc.id, pat.id, req, user_ctx)

    # 2. Record emergency financial override
    er_service = FinancialExceptionService(db_session, h.id)
    er_req = EmergencyOverrideRequest(
        patient_id=pat.id,
        admission_id=resp.admission_id,
        financial_account_id=resp.financial_account_id,
        service_type="admission_bed",
        action_type="bed_allocation",
        reason="Severe active hemorrhage resuscitation",
        emergency_nature="Critical resuscitation in progress",
        life_threat_assessment="Severe shock and active hemorrhage",
        authorized_by_title="CMO",
        amount_deferred=2000.0,
        clinical_summary="Emergency admission override authorized by duty CMO",
    )
    er_resp = er_service.emergency_override(er_req, user_ctx)
    assert er_resp.is_emergency is True
    assert er_resp.status == FinancialExceptionStatus.approved.value or er_resp.status == FinancialExceptionStatus.approved

    # 3. Clearance recheck recognizes emergency override
    clearance = evaluate_bed_allocation_clearance(db_session, h.id, resp.admission_id, ward_id=ward.id, bed_id=bed.id)
    assert clearance.is_cleared is True
    assert clearance.status == "emergency_override"
    # Balance receivable is still 2000 (accounting truth preserved)
    assert clearance.shortfall == 2000.0

    # 4. Nurse allocates bed - succeeds
    nurse_user = {"user_id": str(uuid.uuid4()), "name": "Nurse Priya"}
    accepted = AcceptAdmissionAction(db_session).execute(
        hospital_id=h.id,
        actor=nurse_user,
        admission_id=resp.admission_id,
        ward_id=ward.id,
        room_id=workflow_setup["room"].id,
        bed_id=bed.id,
    )
    assert accepted.status == AdmissionStatus.admitted


def test_financial_exception_approval_allows_bed_allocation(db_session: Session, workflow_setup: dict):
    h = workflow_setup["hospital"]
    doc = workflow_setup["doctor"]
    pat = workflow_setup["patient"]
    ward = workflow_setup["ward"]
    bed = workflow_setup["bed"]

    req = DoctorIpdAdmissionRequest(clinical_reason="Urgent surgery awaiting insurance clearance")
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. Sarah"}
    resp = DoctorRequestIpdAdmissionAction(db_session).execute(h.id, doc.id, pat.id, req, user_ctx)

    # 1. Requester asks for financial exception
    fe_service = FinancialExceptionService(db_session, h.id)
    fe_req = FinancialExceptionRequest(
        patient_id=pat.id,
        admission_id=resp.admission_id,
        financial_account_id=resp.financial_account_id,
        service_type="admission_bed",
        action_type="bed_allocation",
        reason_category="tpa_insurance_pending",
        reason="Pre-auth submitted to Star Health, awaiting letter",
        amount_covered=2000.0,
    )
    fe_record = fe_service.request_exception(fe_req, user_ctx)
    assert fe_record.status == FinancialExceptionStatus.pending

    # Clearance while pending is NOT cleared
    clearance_pending = evaluate_bed_allocation_clearance(db_session, h.id, resp.admission_id, ward_id=ward.id, bed_id=bed.id)
    assert clearance_pending.is_cleared is False
    assert clearance_pending.status == "exception_pending"

    # 2. Second user (administrator / billing head) approves exception
    approver_user = {"user_id": str(uuid.uuid4()), "name": "Finance Head Vikram"}
    decision = FinancialExceptionDecision(
        status="approved",
        approval_remarks="Pre-auth claim verified on TPA portal",
    )
    fe_service.decide_exception(fe_record.id, decision, approver_user)

    # 3. Clearance now reflects exception_approved and clears bed allocation
    clearance_approved = evaluate_bed_allocation_clearance(db_session, h.id, resp.admission_id, ward_id=ward.id, bed_id=bed.id)
    assert clearance_approved.is_cleared is True
    assert clearance_approved.status == "exception_approved"

    # 4. Nurse allocates bed - succeeds
    nurse_user = {"user_id": str(uuid.uuid4()), "name": "Nurse Priya"}
    accepted = AcceptAdmissionAction(db_session).execute(
        hospital_id=h.id,
        actor=nurse_user,
        admission_id=resp.admission_id,
        ward_id=ward.id,
        room_id=workflow_setup["room"].id,
        bed_id=bed.id,
    )
    assert accepted.status == AdmissionStatus.admitted


def test_deposit_creation_emits_admission_sync_event(db_session: Session, workflow_setup: dict):
    h = workflow_setup["hospital"]
    doc = workflow_setup["doctor"]
    pat = workflow_setup["patient"]

    req = DoctorIpdAdmissionRequest(clinical_reason="Check sync events")
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. Sarah"}
    resp = DoctorRequestIpdAdmissionAction(db_session).execute(h.id, doc.id, pat.id, req, user_ctx)

    cashier_user = {"user_id": str(uuid.uuid4()), "name": "Cashier Rahul"}
    dep_payload = BillingDepositCreate(
        patient_id=pat.id,
        admission_id=resp.admission_id,
        amount=2000.0,
        payment_method=BillingPaymentMethod.cash,
        deposit_type="ipd_advance",
    )
    CreateDepositAction(db_session, h.id).execute(dep_payload, cashier_user)

    # Verify audit log recorded for admission financial_update
    logs = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.hospital_id == h.id,
            AuditLog.entity_type == "admission",
            AuditLog.entity_id == str(resp.admission_id),
            AuditLog.action == "financial_update",
        )
        .all()
    )
    assert len(logs) > 0
    assert "Advance deposit" in logs[0].summary
