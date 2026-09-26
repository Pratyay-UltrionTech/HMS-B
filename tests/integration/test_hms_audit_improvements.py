"""
Automated regression tests for the audit HMS improvements:
1. Doctor direct IPD admission request (with and without appointment, completed appointment preservation)
2. IPD FinancialAccount ensured on requested status
3. Bed allocation financial clearance (deposit awareness, admission fee + ward tariff, shortfall calculation)
4. Financial Clearance Exceptions (separation of duties, approval/rejection)
5. Structured Emergency Financial Overrides (instant continuation, auditable, remaining receivable)
6. Unified Medical Record query layer (episode aggregation, no duplicated clinical copies)
"""

import uuid
from datetime import date, datetime, time as dt_time, timezone
from decimal import Decimal
import pytest
from sqlalchemy.orm import Session

from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.beds.entities.bed import Bed, Room, Ward
from modules.billing.contracts.exception_contracts import (
    EmergencyOverrideRequest,
    FinancialExceptionDecision,
    FinancialExceptionRequest,
)
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingSourceType,
    DepositStatus,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
)
from modules.billing.entities.financial_exception import (
    FinancialClearanceException,
    FinancialExceptionStatus,
)
from modules.billing.services.financial_exception_service import FinancialExceptionService
from modules.billing.services.service_financial_clearance import evaluate_bed_allocation_clearance
from modules.clinical_records.services.unified_medical_record_service import UnifiedMedicalRecordService
from modules.doctors.actions.doctor_appointment_actions import DoctorRequestIpdAdmissionAction
from modules.doctors.contracts.doctor_contracts import DoctorIpdAdmissionRequest
from modules.doctors.entities.doctor import HospitalUser
from modules.inpatient.actions.admission_lifecycle_actions import (
    AcceptAdmissionAction,
    EnsureAdmissionRequestAction,
)
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital


@pytest.fixture
def test_setup(db_session: Session, hospital: Hospital, doctor: HospitalUser, patient: Patient):
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
        bed_code="B-1",
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


# ===========================================================================
# 1. Doctor Direct IPD Request Tests
# ===========================================================================

def test_doctor_request_ipd_no_appointment(db_session: Session, test_setup: dict):
    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]

    req = DoctorIpdAdmissionRequest(
        clinical_reason="Severe community-acquired pneumonia requiring IV therapy",
        admission_notes="Monitor O2 saturation Q2H",
    )
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. House"}

    action = DoctorRequestIpdAdmissionAction(db_session)
    resp = action.execute(h.id, doc.id, pat.id, req, user_ctx)

    assert resp.status == "requested"
    assert resp.patient_id == pat.id
    assert resp.doctor_id == doc.id
    assert resp.source_appointment_id is None

    # Verify IPD financial account was ensured immediately upon request
    fa = (
        db_session.query(FinancialAccount)
        .filter(
            FinancialAccount.hospital_id == h.id,
            FinancialAccount.admission_id == resp.admission_id,
            FinancialAccount.account_type == FinancialAccountType.ipd,
        )
        .first()
    )
    assert fa is not None
    assert fa.status == FinancialAccountStatus.open

    # Duplicate open admission request returns existing requested admission idempotently
    resp2 = action.execute(h.id, doc.id, pat.id, req, user_ctx)
    assert resp2.admission_id == resp.admission_id
    assert resp2.status == "requested"


def test_doctor_request_ipd_from_completed_appointment(db_session: Session, test_setup: dict):
    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]

    # Create completed OPD appointment
    appt = Appointment(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        doctor_id=doc.id,
        appointment_date=date.today(),
        appointment_time=dt_time(10, 0),
        purpose="Fever and cough",
        visit_type="first_visit",
        status=AppointmentStatus.completed,
    )
    db_session.add(appt)
    db_session.commit()

    req = DoctorIpdAdmissionRequest(
        clinical_reason="Post-consultation acute respiratory failure",
        source_appointment_id=appt.id,
    )
    user_ctx = {"user_id": str(doc.id), "doctor_id": str(doc.id), "name": "Dr. House"}

    action = DoctorRequestIpdAdmissionAction(db_session)
    resp = action.execute(h.id, doc.id, pat.id, req, user_ctx)

    assert resp.status == "requested"
    assert resp.source_appointment_id == appt.id

    # CRITICAL: Verify completed appointment remains completed!
    db_session.refresh(appt)
    assert appt.status == AppointmentStatus.completed
    assert appt.admission_id == resp.admission_id


# ===========================================================================
# 2. Bed Allocation Financial Clearance & Deposit Awareness Tests
# ===========================================================================

def test_bed_allocation_clearance_shortfall_and_deposit(db_session: Session, test_setup: dict):
    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]
    ward = test_setup["ward"]
    room = test_setup["room"]
    bed = test_setup["bed"]

    actor = {"user_id": str(doc.id), "name": "Dr. House"}

    # 1. Create requested admission with IPD account using EnsureAdmissionRequestAction
    adm, created = EnsureAdmissionRequestAction(db_session).execute(
        hospital_id=h.id,
        patient_id=pat.id,
        actor=actor,
        doctor_id=doc.id,
        notes="Planned monitoring",
    )
    adm_id = adm.id

    # 2. Check clearance before any deposit: admission fee (500) + ward tariff (1500) = 2000
    state = evaluate_bed_allocation_clearance(db_session, h.id, adm_id, ward.id)
    assert not state.is_cleared
    assert state.required_advance >= 1500.0
    assert state.shortfall == state.required_advance

    # Trying to accept bed without clearance fails with HTTP 402
    with pytest.raises(Exception) as exc:
        AcceptAdmissionAction(db_session).execute(
            hospital_id=h.id,
            actor=actor,
            admission_id=adm_id,
            ward_id=ward.id,
            room_id=room.id,
            bed_id=bed.id,
        )
    assert "financial clearance required" in str(exc.value).lower() or "402" in str(exc.value)

    # 3. Add sufficient deposit to the IPD financial account
    fa = (
        db_session.query(FinancialAccount)
        .filter(FinancialAccount.admission_id == adm_id)
        .first()
    )
    from modules.billing.services.billing_service import create_deposit

    create_deposit(
        db_session,
        hospital_id=h.id,
        patient_id=pat.id,
        amount=state.required_advance + 500.0,
        deposit_date=date.today(),
        account_id=fa.id,
        admission_id=adm_id,
        deposit_type="admission",
    )
    db_session.commit()

    # 4. Now evaluate clearance again: deposit covers required advance!
    cleared_state = evaluate_bed_allocation_clearance(db_session, h.id, adm_id, ward.id)
    assert cleared_state.is_cleared
    assert cleared_state.shortfall == 0.0
    assert cleared_state.available_deposit >= cleared_state.required_advance

    # 5. Authoritative backend accepts bed successfully
    accepted_detail = AcceptAdmissionAction(db_session).execute(
        hospital_id=h.id,
        actor=actor,
        admission_id=adm_id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
    )
    assert accepted_detail.status == "admitted"


# ===========================================================================
# 3. Financial Clearance Exceptions & Separation of Duties Tests
# ===========================================================================

def test_financial_exception_workflow_and_separation_of_duties(db_session: Session, test_setup: dict):
    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]
    ward = test_setup["ward"]
    room = test_setup["room"]
    bed = test_setup["bed"]

    nurse_id = uuid.uuid4()
    nurse_user = {"id": str(nurse_id), "name": "Staff Nurse Priya", "role": "nurse"}

    approver_id = uuid.uuid4()
    approver_user = {"id": str(approver_id), "name": "Finance Head Mehta", "role": "billing_manager"}

    actor = {"user_id": str(doc.id), "name": "Dr. House"}
    adm, created = EnsureAdmissionRequestAction(db_session).execute(
        hospital_id=h.id,
        patient_id=pat.id,
        actor=actor,
        doctor_id=doc.id,
        notes="Concession admission",
    )
    adm_id = adm.id

    svc = FinancialExceptionService(db_session, h.id)

    # 1. Nurse requests exception
    req = FinancialExceptionRequest(
        patient_id=pat.id,
        admission_id=adm_id,
        service_type="admission_bed",
        action_type="bed_allocation",
        reason_category="banking_delay",
        reason="Patient relatives traveling from rural area with cash; will deposit tomorrow morning.",
        required_amount=2000.0,
    )
    exc_resp = svc.request_exception(req, nurse_user)
    assert exc_resp.status == FinancialExceptionStatus.pending.value

    # 2. Separation of duties: requester CANNOT approve own exception!
    dec_self = FinancialExceptionDecision(
        status="approved",
        approval_remarks="Self-approval attempt",
    )
    with pytest.raises(Exception) as exc:
        svc.decide_exception(exc_resp.id, dec_self, nurse_user)
    assert "separation of duties" in str(exc.value).lower() or "cannot approve" in str(exc.value).lower()

    # 3. Authorized second user approves
    dec_valid = FinancialExceptionDecision(
        status="approved",
        approval_remarks="Approved 24hr grace period for bed allocation advance.",
    )
    approved_resp = svc.decide_exception(exc_resp.id, dec_valid, approver_user)
    assert approved_resp.status == FinancialExceptionStatus.approved.value

    # 4. Bed allocation now succeeds with approved exception!
    accepted_detail = AcceptAdmissionAction(db_session).execute(
        hospital_id=h.id,
        actor=nurse_user,
        admission_id=adm_id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
    )
    assert accepted_detail.status == "admitted"


# ===========================================================================
# 4. Emergency Financial Override Tests
# ===========================================================================

def test_emergency_financial_override_instant_continuation(db_session: Session, test_setup: dict):
    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]
    ward = test_setup["ward"]
    room = test_setup["room"]
    bed = test_setup["bed"]

    actor = {"user_id": str(doc.id), "name": "Dr. House"}
    adm, created = EnsureAdmissionRequestAction(db_session).execute(
        hospital_id=h.id,
        patient_id=pat.id,
        actor=actor,
        doctor_id=doc.id,
        notes="Polytrauma with unstable vitals",
    )
    adm_id = adm.id

    # Admitted under structured emergency override without money or pre-approval
    accepted_detail = AcceptAdmissionAction(db_session).execute(
        hospital_id=h.id,
        actor=actor,
        admission_id=adm_id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        is_emergency_override=True,
        emergency_override_reason="Severe polytrauma resuscitation in progress; immediate ICU bed required",
    )
    assert accepted_detail.status == "admitted"

    # Verify emergency override record was logged in database with full audit
    override_rec = (
        db_session.query(FinancialClearanceException)
        .filter(
            FinancialClearanceException.hospital_id == h.id,
            FinancialClearanceException.admission_id == adm_id,
            FinancialClearanceException.is_emergency == True,
        )
        .first()
    )
    assert override_rec is not None
    assert override_rec.action_type == "bed_allocation"
    assert "polytrauma" in override_rec.reason.lower()


# ===========================================================================
# 5. Unified Medical Record Aggregation Tests
# ===========================================================================

def test_unified_medical_record_query_layer(db_session: Session, test_setup: dict):
    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]

    # 1. Add an OPD appointment
    appt = Appointment(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        doctor_id=doc.id,
        appointment_date=date.today(),
        appointment_time=dt_time(9, 30),
        purpose="General Checkup",
        visit_type="first_visit",
        status=AppointmentStatus.completed,
    )
    db_session.add(appt)

    # 2. Add an IPD admission
    adm = Admission(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        doctor_id=doc.id,
        ip_id=f"IP-{uuid.uuid4().hex[:6].upper()}",
        notes="Knee arthroscopy",
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(adm)
    db_session.commit()

    # Query unified medical record
    svc = UnifiedMedicalRecordService(db_session, h.id)
    rec = svc.get_patient_medical_record(pat.id)

    assert rec.patient_id == pat.id
    assert len(rec.episodes) >= 2
    episode_types = [ep.encounter_type for ep in rec.episodes]
    assert "opd" in episode_types
    assert "ipd" in episode_types

    # Find the IPD episode
    ipd_ep = next(ep for ep in rec.episodes if ep.encounter_type == "ipd")
    assert ipd_ep.identifier == adm.ip_id
    assert ipd_ep.episode_id == adm.id


# ===========================================================================
# 6. STAT String Bypass Prevention & Structured Override Tests (Part 9)
# ===========================================================================

def test_stat_string_does_not_bypass_laboratory_payment_gate(db_session: Session, test_setup: dict):
    from fastapi import HTTPException
    from modules.laboratory.actions.laboratory_actions import CollectSampleAction
    from modules.laboratory.contracts.lab_contracts import SampleCollectRequest
    from modules.laboratory.entities.lab_entities import LabOrder, LabOrderStatus, LabOrderSource, LabSampleType
    from modules.billing.entities.billing_entities import BillingCharge, BillingChargeStatus, BillingSourceType

    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]
    actor = {"id": str(doc.id), "name": doc.name, "role": "hospital_staff", "hospital_uuid": str(h.id)}

    order = LabOrder(
        id=uuid.uuid4(),
        hospital_id=h.id,
        order_no=f"LAB-{uuid.uuid4().hex[:6].upper()}",
        patient_id=pat.id,
        doctor_id=doc.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.ordered,
        sample_type=LabSampleType.blood,
        clinical_notes="Patient routine checkup; request marked STAT",
    )
    db_session.add(order)

    # Add unpaid billing charge
    charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        source_type=BillingSourceType.laboratory,
        source_id=order.id,
        description="Comprehensive Blood Panel",
        quantity=Decimal("1.0"),
        unit_price=Decimal("1200.0"),
        charge_amount=Decimal("1200.0"),
        discount_amount=Decimal("0.0"),
        tax_amount=Decimal("0.0"),
        net_amount=Decimal("1200.0"),
        amount_paid=Decimal("0.0"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(charge)
    db_session.commit()

    # Attempt specimen collection putting "STAT" in collection_remarks without structured emergency override
    payload_stat = SampleCollectRequest(
        collected_by="Lab Tech",
        collection_remarks="STAT URGENT STAT - clinical emergency note",
        is_emergency_override=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        CollectSampleAction(db_session, h.id).execute(order.id, payload_stat, actor)

    assert exc_info.value.status_code == 402
    assert "Payment required" in str(exc_info.value.detail)

    # Verify order status was NOT changed to sample_collected
    db_session.refresh(order)
    assert order.status == LabOrderStatus.ordered
    assert order.collected_at is None

    # Now execute with genuine structured emergency clinical override
    payload_override = SampleCollectRequest(
        collected_by="Lab Tech",
        collection_remarks="Emergency clinical resuscitation panel",
        is_emergency_override=True,
        emergency_override_reason="Severe anaphylactic collapse in resuscitation bay",
    )

    res = CollectSampleAction(db_session, h.id).execute(order.id, payload_override, actor)
    assert res.status == LabOrderStatus.sample_collected

    # Verify charge remains unpaid (receivable preserved, no fake money)
    db_session.refresh(charge)
    assert charge.status == BillingChargeStatus.pending
    assert float(charge.amount_paid) == 0.0


def test_stat_string_does_not_bypass_radiology_payment_gate(db_session: Session, test_setup: dict):
    from fastapi import HTTPException
    from modules.radiology.actions.radiology_actions import ScheduleOrderAction
    from modules.radiology.contracts.radiology_contracts import RadScheduleRequest
    from modules.radiology.entities.radiology_entities import RadiologyOrder, RadiologyOrderStatus
    from modules.billing.entities.billing_entities import BillingCharge, BillingChargeStatus, BillingSourceType

    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]
    actor = {"id": str(doc.id), "name": doc.name, "role": "hospital_staff", "hospital_uuid": str(h.id)}

    order = RadiologyOrder(
        id=uuid.uuid4(),
        hospital_id=h.id,
        order_no=f"RAD-{uuid.uuid4().hex[:6].upper()}",
        patient_id=pat.id,
        doctor_id=doc.id,
        scan_code="CT-BRAIN",
        scan_name="CT Brain Non-Contrast",
        category="CT",
        price=3500.0,
        status=RadiologyOrderStatus.ordered,
        clinical_notes="STAT rule out stroke",
        remarks="STAT priority requested by clinician",
    )
    db_session.add(order)

    # Add unpaid billing charge
    charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        source_type=BillingSourceType.radiology,
        source_id=order.id,
        description="CT Brain Non-Contrast",
        quantity=Decimal("1.0"),
        unit_price=Decimal("3500.0"),
        charge_amount=Decimal("3500.0"),
        discount_amount=Decimal("0.0"),
        tax_amount=Decimal("0.0"),
        net_amount=Decimal("3500.0"),
        amount_paid=Decimal("0.0"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(charge)
    db_session.commit()

    # Attempt to schedule with "STAT" in notes/remarks but without emergency override
    payload_stat = RadScheduleRequest(
        scheduled_at=datetime.now(timezone.utc),
        machine="CT-Scanner-1",
        technician_name="Rad Tech",
        is_emergency_override=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        ScheduleOrderAction(db_session, h.id, actor).execute(order.id, payload_stat)

    assert exc_info.value.status_code == 402
    assert "Payment required" in str(exc_info.value.detail)

    # Verify order status was NOT changed to scheduled
    db_session.refresh(order)
    assert order.status == RadiologyOrderStatus.ordered

    # Now schedule with genuine structured emergency override
    payload_override = RadScheduleRequest(
        scheduled_at=datetime.now(timezone.utc),
        machine="CT-Scanner-1",
        technician_name="Rad Tech",
        is_emergency_override=True,
        emergency_override_reason="Acute neurological deficit; stroke thrombolysis window",
    )

    res = ScheduleOrderAction(db_session, h.id, actor).execute(order.id, payload_override)
    assert res.status == RadiologyOrderStatus.scheduled

    # Verify charge remains unpaid (receivable preserved)
    db_session.refresh(charge)
    assert charge.status == BillingChargeStatus.pending
    assert float(charge.amount_paid) == 0.0


# ===========================================================================
# 7. Post-Service Clinical Results Never Withheld After Service Delivery
# ===========================================================================

def test_diagnostic_report_viewing_and_finalization_not_blocked_after_service(db_session: Session, test_setup: dict):
    from modules.laboratory.actions.laboratory_actions import (
        CollectSampleAction,
        SaveLabResultsAction,
        GetLabReportHtmlAction,
    )
    from modules.laboratory.contracts.lab_contracts import (
        SampleCollectRequest,
        LabReportSaveRequest,
        LabResultInput,
    )
    from modules.laboratory.entities.lab_entities import (
        LabOrder,
        LabOrderStatus,
        LabOrderItem,
        LabItemStatus,
        LabSpecimen,
        LabSpecimenStatus,
        LabSampleType,
    )
    from modules.radiology.actions.radiology_actions import (
        UploadReportAction,
    )
    from modules.radiology.contracts.radiology_contracts import (
        RadReportRequest,
    )
    from modules.radiology.entities.radiology_entities import (
        RadiologyOrder,
        RadiologyOrderStatus,
    )
    from modules.billing.entities.billing_entities import (
        BillingCharge,
        BillingChargeStatus,
        BillingSourceType,
    )

    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]
    actor = {"id": str(doc.id), "name": doc.name, "role": "doctor", "hospital_uuid": str(h.id)}

    # ── 1. Laboratory: Collected sample allows result entry, sign-off and HTML viewing even if charge is unpaid ──
    lab_order = LabOrder(
        id=uuid.uuid4(),
        hospital_id=h.id,
        order_no=f"LAB-{uuid.uuid4().hex[:6].upper()}",
        patient_id=pat.id,
        doctor_id=doc.id,
        status=LabOrderStatus.in_progress,
    )
    db_session.add(lab_order)
    order_item = LabOrderItem(
        id=uuid.uuid4(),
        hospital_id=h.id,
        order_id=lab_order.id,
        test_name="Complete Blood Count",
        test_code="CBC",
        price=450.0,
        status=LabItemStatus.processing,
    )
    db_session.add(order_item)
    sample = LabSpecimen(
        id=uuid.uuid4(),
        hospital_id=h.id,
        order_id=lab_order.id,
        specimen_no="SMP-CBC-01",
        barcode_value="BAR-CBC-01",
        sample_type=LabSampleType.blood,
        status=LabSpecimenStatus.collected,
        collected_at=datetime.now(timezone.utc),
        collected_by="Phlebotomist",
    )
    db_session.add(sample)

    # Outstanding unpaid billing charge exists
    lab_charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        source_type=BillingSourceType.laboratory,
        source_id=lab_order.id,
        description="Complete Blood Count",
        quantity=Decimal("1.0"),
        unit_price=Decimal("450.0"),
        charge_amount=Decimal("450.0"),
        discount_amount=Decimal("0.0"),
        tax_amount=Decimal("0.0"),
        net_amount=Decimal("450.0"),
        amount_paid=Decimal("0.0"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(lab_charge)
    db_session.commit()

    # Clinical result entry and sign-off MUST NOT be blocked by the unpaid billing charge!
    save_payload = LabReportSaveRequest(
        results=[
            LabResultInput(
                order_item_id=order_item.id,
                parameter_name="Hemoglobin",
                result_value="13.8",
                unit="g/dL",
                reference_range="13.0 - 17.0",
                is_panic=False,
            )
        ],
        mark_completed=True,
    )
    saved_order = SaveLabResultsAction(db_session, h.id).execute(lab_order.id, save_payload, actor)
    assert saved_order.status == LabOrderStatus.completed

    # Report viewing MUST NOT be blocked by billing status
    html_report, _filename = GetLabReportHtmlAction(db_session, h.id).execute(lab_order.id)
    assert "Hemoglobin" in html_report
    assert "13.8" in html_report

    # ── 2. Radiology: Executed scan allows report upload and viewing even if charge is unpaid ──
    rad_order = RadiologyOrder(
        id=uuid.uuid4(),
        hospital_id=h.id,
        order_no=f"RAD-{uuid.uuid4().hex[:6].upper()}",
        patient_id=pat.id,
        doctor_id=doc.id,
        scan_code="CXR-PA",
        scan_name="Chest X-Ray PA View",
        category="X-Ray",
        price=800.0,
        status=RadiologyOrderStatus.completed,
        image_file_data="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        image_file_name="cxr.png",
    )
    db_session.add(rad_order)

    rad_charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        source_type=BillingSourceType.radiology,
        source_id=rad_order.id,
        description="Chest X-Ray PA View",
        quantity=Decimal("1.0"),
        unit_price=Decimal("800.0"),
        charge_amount=Decimal("800.0"),
        discount_amount=Decimal("0.0"),
        tax_amount=Decimal("0.0"),
        net_amount=Decimal("800.0"),
        amount_paid=Decimal("0.0"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(rad_charge)
    db_session.commit()

    # Radiologist report upload MUST NOT be blocked by unpaid charge!
    rep_payload = RadReportRequest(
        findings="Clear lung fields. Normal cardiothoracic ratio.",
        impression="Normal chest radiograph.",
    )
    rep_res = UploadReportAction(db_session, h.id, actor).execute(rad_order.id, rep_payload)
    assert rep_res.status == RadiologyOrderStatus.completed
    assert rep_res.findings == "Clear lung fields. Normal cardiothoracic ratio."


# ===========================================================================
# 8. Unpaid Diagnostic Orders Remain Visible in Queues / Worklists
# ===========================================================================

def test_unpaid_diagnostic_orders_remain_visible_in_queues(db_session: Session, test_setup: dict):
    from fastapi import HTTPException
    from modules.laboratory.actions.laboratory_actions import (
        ListLabOrdersAction,
        CollectSampleAction,
    )
    from modules.laboratory.contracts.lab_contracts import SampleCollectRequest
    from modules.laboratory.entities.lab_entities import (
        LabOrder,
        LabOrderStatus,
        LabSampleType,
    )
    from modules.radiology.actions.radiology_actions import (
        ListOrdersAction,
        ScheduleOrderAction,
    )
    from modules.radiology.contracts.radiology_contracts import RadScheduleRequest
    from modules.radiology.entities.radiology_entities import (
        RadiologyOrder,
        RadiologyOrderStatus,
    )
    from modules.billing.entities.billing_entities import (
        BillingCharge,
        BillingChargeStatus,
        BillingSourceType,
    )

    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]
    actor = {"id": str(doc.id), "name": doc.name, "role": "doctor", "hospital_uuid": str(h.id)}

    # 1. Unpaid Lab Order
    lab_order = LabOrder(
        id=uuid.uuid4(),
        hospital_id=h.id,
        order_no=f"LAB-{uuid.uuid4().hex[:6].upper()}",
        patient_id=pat.id,
        doctor_id=doc.id,
        status=LabOrderStatus.ordered,
    )
    db_session.add(lab_order)
    lab_charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        source_type=BillingSourceType.laboratory,
        source_id=lab_order.id,
        description="Liver Function Test",
        quantity=Decimal("1.0"),
        unit_price=Decimal("800.0"),
        charge_amount=Decimal("800.0"),
        discount_amount=Decimal("0.0"),
        tax_amount=Decimal("0.0"),
        net_amount=Decimal("800.0"),
        amount_paid=Decimal("0.0"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(lab_charge)

    # 2. Unpaid Radiology Order
    rad_order = RadiologyOrder(
        id=uuid.uuid4(),
        hospital_id=h.id,
        order_no=f"RAD-{uuid.uuid4().hex[:6].upper()}",
        patient_id=pat.id,
        doctor_id=doc.id,
        scan_code="USG-ABD",
        scan_name="Ultrasound Whole Abdomen",
        category="Ultrasound",
        price=1200.0,
        status=RadiologyOrderStatus.ordered,
    )
    db_session.add(rad_order)
    rad_charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        source_type=BillingSourceType.radiology,
        source_id=rad_order.id,
        description="Ultrasound Whole Abdomen",
        quantity=Decimal("1.0"),
        unit_price=Decimal("1200.0"),
        charge_amount=Decimal("1200.0"),
        discount_amount=Decimal("0.0"),
        tax_amount=Decimal("0.0"),
        net_amount=Decimal("1200.0"),
        amount_paid=Decimal("0.0"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(rad_charge)
    db_session.commit()

    # Query Lab worklist: unpaid order MUST remain visible in pending worklist!
    lab_list = ListLabOrdersAction(db_session, h.id).execute(status=LabOrderStatus.ordered)
    matching_lab = [r for r in lab_list if r.id == lab_order.id]
    assert len(matching_lab) == 1
    assert matching_lab[0].is_financially_cleared is False
    assert matching_lab[0].payment_status == "pending"

    # Query Radiology worklist: unpaid order MUST remain visible in pending worklist!
    rad_list = ListOrdersAction(db_session, h.id).execute(status_filter=RadiologyOrderStatus.ordered)
    matching_rad = [r for r in rad_list if r.id == rad_order.id]
    assert len(matching_rad) == 1
    assert matching_rad[0].is_financially_cleared is False
    assert matching_rad[0].payment_status == "pending"

    # Verify execution action itself remains strictly blocked until payment/override
    with pytest.raises(HTTPException) as exc_lab:
        CollectSampleAction(db_session, h.id).execute(
            lab_order.id,
            SampleCollectRequest(sample_type=LabSampleType.blood, collected_by="Phlebotomist"),
            actor,
        )
    assert exc_lab.value.status_code == 402

    with pytest.raises(HTTPException) as exc_rad:
        ScheduleOrderAction(db_session, h.id, actor).execute(
            rad_order.id,
            RadScheduleRequest(
                scheduled_at=datetime.now(timezone.utc),
                machine="USG-1",
                technician_name="Rad Tech",
            ),
        )
    assert exc_rad.value.status_code == 402


# ===========================================================================
# 9. Emergency Override Actor Authorization & Mandatory Reason
# ===========================================================================

def test_emergency_override_validation_and_permissions(db_session: Session, test_setup: dict):
    from fastapi import HTTPException
    from modules.billing.services.service_financial_clearance import (
        assert_service_financially_cleared,
        validate_emergency_override_actor,
    )

    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]

    # 1. Blank or whitespace reason MUST fail with HTTP 400
    with pytest.raises(HTTPException) as exc_400:
        validate_emergency_override_actor(
            actor={"id": str(doc.id), "name": doc.name, "role": "doctor"},
            reason="   ",
            service_type="laboratory",
        )
    assert exc_400.value.status_code == 400
    assert "mandatory" in exc_400.value.detail.lower()

    # 2. Unauthorized actor (e.g. receptionist / desk clerk) MUST fail with HTTP 403
    unauthorized_actor = {
        "id": str(uuid.uuid4()),
        "name": "Front Desk Receptionist",
        "role": "receptionist",
        "staff_role_name": "Front Desk Clerk",
        "permissions": [],
    }
    with pytest.raises(HTTPException) as exc_403:
        validate_emergency_override_actor(
            actor=unauthorized_actor,
            reason="Patient looks very sick, need immediate test",
            service_type="laboratory",
        )
    assert exc_403.value.status_code == 403
    assert "not authorized" in exc_403.value.detail.lower()

    # 3. Authorized doctor with valid reason passes validation and returns actor details
    authorized_actor = {
        "id": str(doc.id),
        "name": doc.name,
        "role": "doctor",
        "staff_role_name": "Consultant Physician",
    }
    actor_id, actor_name = validate_emergency_override_actor(
        actor=authorized_actor,
        reason="Acute septic shock; lactate clearance monitoring",
        service_type="laboratory",
    )
    assert actor_id == doc.id
    assert actor_name == doc.name


# ===========================================================================
# 10. Financial Exception Scope & Consumption (Replay Prevention)
# ===========================================================================

def test_financial_exception_cross_scope_and_consumption(db_session: Session, test_setup: dict):
    from fastapi import HTTPException
    from modules.billing.services.service_financial_clearance import assert_service_financially_cleared
    from modules.billing.contracts.exception_contracts import (
        FinancialExceptionRequest,
        FinancialExceptionDecision,
    )
    from modules.billing.services.financial_exception_service import FinancialExceptionService
    from modules.billing.entities.billing_entities import (
        BillingCharge,
        BillingChargeStatus,
        BillingSourceType,
    )

    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]

    requester = {"user_id": str(uuid.uuid4()), "name": "Nurse Jane", "role": "hospital_staff"}
    approver = {"user_id": str(uuid.uuid4()), "name": "CFO Robert", "role": "hospital_admin"}

    # 1. Create and approve an exception specifically scoped to bed allocation
    svc = FinancialExceptionService(db_session, h.id)
    req = FinancialExceptionRequest(
        patient_id=pat.id,
        service_type="bed",
        action_type="bed_allocation",
        reason_category="administrative_approval",
        reason="Patient has approved corporate coverage arriving tomorrow morning",
        required_amount=2500.0,
    )
    exc_obj = svc.request_exception(req, requester)
    approved_exc = svc.decide_exception(
        exc_obj.id,
        FinancialExceptionDecision(status="approved", approval_remarks="Approved corporate guarantee"),
        approver,
    )
    assert approved_exc.status == "approved"

    # Create an unpaid lab charge for this patient
    lab_charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid.uuid4(),
        description="Electrolyte Panel",
        quantity=Decimal("1.0"),
        unit_price=Decimal("600.0"),
        charge_amount=Decimal("600.0"),
        discount_amount=Decimal("0.0"),
        tax_amount=Decimal("0.0"),
        net_amount=Decimal("600.0"),
        amount_paid=Decimal("0.0"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(lab_charge)
    db_session.commit()

    # 2. Attempt to use bed_allocation exception to clear laboratory service -> MUST FAIL with 402!
    with pytest.raises(HTTPException) as exc_scope:
        assert_service_financially_cleared(
            db_session,
            hospital_id=h.id,
            source_type=BillingSourceType.laboratory,
            source_id=lab_charge.source_id,
            action_description="collect lab specimen",
            patient_id=pat.id,
        )
    assert exc_scope.value.status_code == 402

    # 3. Use the exception for its legitimate scope: bed
    # This should succeed and consume the exception
    assert_service_financially_cleared(
        db_session,
        hospital_id=h.id,
        source_type=BillingSourceType.bed,
        source_id=uuid.uuid4(),
        action_description="allocate inpatient bed",
        patient_id=pat.id,
    )

    # Verify exception is consumed in database
    db_session.expire_all()
    consumed_rec = (
        db_session.query(FinancialClearanceException)
        .filter(FinancialClearanceException.id == exc_obj.id)
        .first()
    )
    assert consumed_rec.is_consumed is True
    assert consumed_rec.status == "consumed"

    # 4. Attempt to REPLAY / REUSE the consumed exception -> MUST FAIL with 402!
    with pytest.raises(HTTPException) as exc_replay:
        assert_service_financially_cleared(
            db_session,
            hospital_id=h.id,
            source_type=BillingSourceType.bed,
            source_id=uuid.uuid4(),
            action_description="allocate second bed",
            patient_id=pat.id,
        )
    assert exc_replay.value.status_code == 402


# ===========================================================================
# 11. Admission Financial Policy (Hospital-Scoped & Configurable)
# ===========================================================================

def test_admission_financial_policy_hospital_scoped(db_session: Session, test_setup: dict, hospital_b: Hospital):
    from modules.billing.services.service_financial_clearance import evaluate_bed_allocation_clearance
    from modules.inpatient.entities.admission import Admission, AdmissionStatus

    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    ward = test_setup["ward"]
    pat = test_setup["patient"]

    # Create admission for Hospital A
    adm_a = Admission(
        id=uuid.uuid4(),
        hospital_id=h.id,
        patient_id=pat.id,
        doctor_id=doc.id,
        ip_id=f"IP-{uuid.uuid4().hex[:6].upper()}",
        status=AdmissionStatus.requested,
    )
    db_session.add(adm_a)

    # 1. Hospital A: Configured with MANDATORY advance, min advance = 3000.0, includes admission fee and 1st day tariff
    h.facility_settings = {
        "admission_financial_policy": {
            "financial_gate_enabled": True,
            "advance_mode": "mandatory",
            "minimum_advance_amount": 3000.0,
            "include_admission_fee": True,
            "include_first_day_bed_tariff": True,
            "emergency_bypass_allowed": True,
        }
    }
    db_session.commit()

    # Ward has admission_fee=500.0 and bed_charge_per_day=1500.0
    # Sum of fee + tariff = 2000.0. Max with minimum_advance (3000.0) = 3000.0
    clearance_h_a = evaluate_bed_allocation_clearance(db_session, h.id, adm_a.id, ward.id)
    assert clearance_h_a.required_advance == 3000.0
    assert clearance_h_a.is_cleared is False
    assert clearance_h_a.shortfall == 3000.0

    # 2. Hospital B: Configured with WAIVED advance policy
    hospital_b.facility_settings = {
        "admission_financial_policy": {
            "financial_gate_enabled": False,
            "advance_mode": "waived",
            "minimum_advance_amount": 0.0,
            "include_admission_fee": False,
            "include_first_day_bed_tariff": False,
        }
    }
    ward_b = Ward(
        id=uuid.uuid4(),
        hospital_id=hospital_b.id,
        name="General Ward B",
        ward_type="general",
        admission_fee=500.0,
        bed_charge_per_day=1500.0,
        is_active=True,
    )
    db_session.add(ward_b)
    adm_b = Admission(
        id=uuid.uuid4(),
        hospital_id=hospital_b.id,
        patient_id=pat.id,
        doctor_id=doc.id,
        ip_id=f"IP-{uuid.uuid4().hex[:6].upper()}",
        status=AdmissionStatus.requested,
    )
    db_session.add(adm_b)
    db_session.commit()

    clearance_h_b = evaluate_bed_allocation_clearance(db_session, hospital_b.id, adm_b.id, ward_b.id)
    assert clearance_h_b.required_advance == 0.0
    assert clearance_h_b.is_cleared is True
    assert clearance_h_b.shortfall == 0.0


# ===========================================================================
# 12. Pharmacy Stock Safety: Clearance Failure Leaves Inventory Unchanged
# ===========================================================================

def test_pharmacy_dispense_clearance_failure_preserves_stock(db_session: Session, test_setup: dict):
    from datetime import timedelta
    from fastapi import HTTPException
    from modules.pharmacy.actions.pharmacy_actions import PharmacyActions
    from modules.pharmacy.contracts.pharmacy_contracts import (
        SaleCreate,
        SaleItemCreate,
    )
    from modules.pharmacy.entities.pharmacy_entities import (
        Medicine,
        MedicineBatch,
        MedicineCategory,
        MedicineInventory,
        MedicineType,
        MedicineUnit,
        PharmacySale,
        PharmacySaleType,
    )

    h = test_setup["hospital"]
    doc = test_setup["doctor"]
    pat = test_setup["patient"]
    actor = {"user_id": str(doc.id), "name": doc.name, "role": "hospital_staff"}

    cat = MedicineCategory(id=uuid.uuid4(), hospital_id=h.id, name="Antibiotics", is_active=True)
    db_session.add(cat)
    med = Medicine(
        id=uuid.uuid4(),
        hospital_id=h.id,
        category_id=cat.id,
        medicine_name="Amoxicillin 500mg",
        brand_name="Amoxil",
        manufacturer="GSK",
        medicine_type=MedicineType.capsule,
        unit=MedicineUnit.strip,
        selling_price=120.0,
        purchase_price=80.0,
        mrp=140.0,
        gst_percent=12.0,
        is_active=True,
    )
    db_session.add(med)
    inv = MedicineInventory(
        id=uuid.uuid4(),
        hospital_id=h.id,
        medicine_id=med.id,
        current_stock=50.0,
        available_stock=50.0,
        reserved_stock=0.0,
    )
    db_session.add(inv)
    batch = MedicineBatch(
        id=uuid.uuid4(),
        hospital_id=h.id,
        medicine_id=med.id,
        batch_number="AMX-2026-01",
        expiry_date=date.today() + timedelta(days=180),
        initial_quantity=50.0,
        available_quantity=50.0,
        purchase_price=80.0,
        mrp=140.0,
        selling_price=120.0,
        is_active=True,
    )
    db_session.add(batch)
    db_session.commit()

    initial_inv_stock = inv.current_stock
    initial_batch_qty = batch.available_quantity

    # 1. Attempt dispense where financial clearance fails (unpaid OPD sale, 0 amount paid, no deposits)
    sale_payload = SaleCreate(
        customer_name=pat.name,
        customer_phone=pat.mobile,
        patient_id=pat.id,
        sale_type=PharmacySaleType.patient,
        payment_method="cash",
        amount_paid=0.0,
        is_emergency_override=False,
        items=[
            SaleItemCreate(
                medicine_id=med.id,
                batch_id=batch.id,
                quantity=10.0,
                unit_price=120.0,
                discount_amount=0.0,
            )
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        PharmacyActions(db_session, h.id, actor).create_sale(sale_payload)

    assert exc_info.value.status_code == 402
    assert "payment" in str(exc_info.value.detail).lower()

    # 2. Strict Stock Safety Assertions:
    # Inventory stock must be EXACTLY unchanged
    db_session.refresh(inv)
    assert inv.current_stock == initial_inv_stock == 50.0

    # Batch quantity must be EXACTLY unchanged
    db_session.refresh(batch)
    assert batch.available_quantity == initial_batch_qty == 50.0

    # No completed pharmacy sale record must be created in the database
    sales_count = (
        db_session.query(PharmacySale)
        .filter(PharmacySale.hospital_id == h.id, PharmacySale.patient_id == pat.id)
        .count()
    )
    assert sales_count == 0


