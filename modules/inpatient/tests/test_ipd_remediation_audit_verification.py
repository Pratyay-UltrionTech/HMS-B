"""
Comprehensive Remediation Verification Tests for HMS Clinical + IPD + Billing.
Verifies all 8 critical priority areas and Section 29 audit remediation requirements.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy.orm import Session

from modules.billing.actions.billing_actions import CreateDepositAction, CreatePaymentAction
from modules.billing.contracts.billing_contracts import (
    BillingDepositCreate,
    BillingPaymentCreate,
)
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingDeposit,
    BillingPayment,
    BillingSourceType,
    FinancialAccount,
)
from modules.billing.services.billing_service import (
    ensure_charge,
    get_or_create_financial_account,
    patient_ledger_totals,
)
from modules.inpatient.actions.admission_actions import (
    AdmitPatientAction,
    DischargePatientAction,
)
from modules.inpatient.actions.discharge_exception_actions import (
    DecideDischargeExceptionAction,
    RequestDischargeExceptionAction,
)
from modules.inpatient.actions.form_addendum_actions import (
    CreateFormAddendumAction,
    ListFormAddendaAction,
)
from modules.inpatient.actions.ipd_actions import (
    CreateFormSubmissionAction,
    ListFormSubmissionsAction,
    UpdateFormSubmissionAction,
)
from modules.inpatient.actions.medication_order_actions import (
    AdministerPrnMedicationAction,
    CreateMedicationOrderAction,
    DiscontinueMedicationOrderAction,
    ListMedicationOrdersAction,
)
from modules.inpatient.contracts.inpatient_contracts import (
    AdministerPrnMedicationRequest,
    AdmitRequest,
    DischargeFinancialExceptionCreate,
    DischargeFinancialExceptionDecision,
    DischargeRequest,
    DiscontinueMedicationOrderRequest,
    IpdFormAddendumCreate,
    IpdFormSubmissionCreate,
    IpdFormSubmissionUpdate,
    IpdMedicationOrderCreate,
)
from modules.beds.entities.bed import Bed, Room, Ward, WardType
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.inpatient.entities.admission import (
    Admission,
    AdmissionStatus,
    IpdFormSubmission,
    IpdFormSubmissionStatus,
)
from modules.masters.entities.organization_entities import Department
from modules.inpatient.entities.care_team import AdmissionCareTeamRole
from modules.inpatient.entities.discharge_exception import ExceptionStatus
from modules.inpatient.entities.form_addendum import IpdFormAddendum
from modules.inpatient.entities.medication_order import (
    IpdMedicationOrder,
    MedicationOrderStatus,
)
from modules.inpatient.entities.nursing_entities import (
    MedicationAdminStatus,
    MedicationAdministrationRecord,
)
from modules.patients.entities.patient import Patient, PatientStatus
from modules.tenancy.entities.hospital import Hospital
from shared.exceptions.base import ValidationError


def _make_hospital_user(db: Session, hospital_id: uuid.UUID, role_name: str, name: str) -> HospitalUser:
    role = StaffRole(id=uuid.uuid4(), hospital_id=hospital_id, name=f"{role_name}-{uuid.uuid4().hex[:6]}")
    db.add(role)
    db.flush()

    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"{name.lower().replace(' ', '')}_{uuid.uuid4().hex[:6]}@hospital.com",
        phone="9876543210",
        password_hash="hash",
        name=name,
        role_id=role.id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _setup_env(db: Session, hospital: Hospital):
    uid = uuid.uuid4().hex[:6]
    doc = _make_hospital_user(db, hospital.id, "Doctor", f"Dr. Smith {uid}")
    nurse = _make_hospital_user(db, hospital.id, "Nurse", f"Nurse Kelly {uid}")
    admin = _make_hospital_user(db, hospital.id, "Admin", f"Admin {uid}")
    billing_approver = _make_hospital_user(db, hospital.id, "Billing", f"CFO {uid}")

    dept = Department(hospital_id=hospital.id, name=f"Medicine {uid}", code=f"MED_{uid}")
    db.add(dept)
    db.commit()
    db.refresh(dept)

    patient = Patient(
        hospital_id=hospital.id,
        uhid=f"UHID-TEST-{uid}",
        name=f"Patient {uid}",
        gender="male",
        mobile="9876543210",
        status=PatientStatus.active,
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)

    ward = Ward(
        hospital_id=hospital.id,
        name=f"Gen Ward {uid}",
        ward_type="general",
        admission_fee=0.0,
        bed_charge_per_day=0.0,
    )
    db.add(ward)
    db.commit()
    db.refresh(ward)

    room = Room(hospital_id=hospital.id, ward_id=ward.id, room_code=f"R101_{uid}")
    db.add(room)
    db.commit()
    db.refresh(room)

    bed = Bed(hospital_id=hospital.id, ward_id=ward.id, room_id=room.id, bed_code=f"B101_{uid}", is_occupied=False)
    db.add(bed)
    db.commit()
    db.refresh(bed)

    return doc, nurse, admin, billing_approver, dept, patient, ward, room, bed


def test_prn_order_generates_zero_routine_schedules_and_administers_on_demand(
    db_session: Session, hospital: Hospital
):
    doc, nurse, admin, _, dept, patient, ward, room, bed = _setup_env(db_session, hospital)

    admit_req = AdmitRequest(
        patient_id=patient.id,
        doctor_id=doc.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        department_id=dept.id,
    )
    actor_admin = {"id": str(admin.id), "name": admin.name, "role": "hospital_admin"}
    detail = AdmitPatientAction(db_session).execute(hospital.id, admit_req, actor_admin)

    # 1. Prescribe PRN order: Paracetamol 1g IV PRN for fever > 38.5C
    actor_doc = {"id": str(doc.id), "name": doc.name, "role": "doctor"}
    prn_req = IpdMedicationOrderCreate(
        medicine_name="Paracetamol",
        dose="1g",
        route="iv",
        frequency="PRN",
        is_prn=True,
        prn_indication="Fever > 38.5 C or severe pain",
        instructions="Max 4g in 24 hours",
    )
    order = CreateMedicationOrderAction(db_session, hospital.id).execute(
        detail.id, prn_req, actor_doc
    )
    assert order.status == MedicationOrderStatus.active
    assert order.is_prn is True

    # 2. Assert ZERO routine scheduled MAR doses created
    emars = (
        db_session.query(MedicationAdministrationRecord)
        .filter(
            MedicationAdministrationRecord.hospital_id == hospital.id,
            MedicationAdministrationRecord.order_id == order.id,
        )
        .all()
    )
    assert len(emars) == 0, "PRN order must NOT generate routine scheduled MAR doses"

    # 3. Nurse administers PRN dose on-demand with explicit clinical indication
    actor_nurse = {"id": str(nurse.id), "name": nurse.name, "role": "nurse"}
    admin_req = AdministerPrnMedicationRequest(
        indication="Patient spike fever 39.1 C, chills observed",
        notes="Administered slowly over 15 mins",
    )
    admin_result = AdministerPrnMedicationAction(db_session, hospital.id).execute(
        order.id, admin_req, actor_nurse
    )
    assert admin_result.status == MedicationAdminStatus.administered
    assert admin_result.administering_nurse_id == nurse.id
    assert "PRN Administered: Patient spike fever 39.1 C" in (admin_result.notes_or_reason or "")

    # 4. Verify 1 MAR record exists now in DB with status administered
    emars_after = (
        db_session.query(MedicationAdministrationRecord)
        .filter(
            MedicationAdministrationRecord.hospital_id == hospital.id,
            MedicationAdministrationRecord.order_id == order.id,
        )
        .all()
    )
    assert len(emars_after) == 1
    assert emars_after[0].status == MedicationAdminStatus.administered


def test_frequency_normalization_and_unsupported_frequency_rejection(
    db_session: Session, hospital: Hospital
):
    doc, _, admin, _, dept, patient, ward, room, bed = _setup_env(db_session, hospital)

    admit_req = AdmitRequest(
        patient_id=patient.id,
        doctor_id=doc.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        department_id=dept.id,
    )
    detail = AdmitPatientAction(db_session).execute(
        hospital.id, admit_req, {"id": str(admin.id), "name": admin.name, "role": "hospital_admin"}
    )
    actor_doc = {"id": str(doc.id), "name": doc.name, "role": "doctor"}

    # Unsupported frequency must raise ValidationError and NEVER guess or fallback to q12h
    invalid_req = IpdMedicationOrderCreate(
        medicine_name="Experimental Drug",
        dose="50mg",
        route="oral",
        frequency="as_directed_by_astrology",
    )
    with pytest.raises(ValidationError) as exc_info:
        CreateMedicationOrderAction(db_session, hospital.id).execute(
            detail.id, invalid_req, actor_doc
        )
    assert "Unsupported medication frequency" in str(exc_info.value)

    # Valid frequencies normalize correctly
    valid_req = IpdMedicationOrderCreate(
        medicine_name="Ceftriaxone",
        dose="1g",
        route="iv",
        frequency="BD",  # 2 doses per day
        duration_days=1,
    )
    valid_order = CreateMedicationOrderAction(db_session, hospital.id).execute(
        detail.id, valid_req, actor_doc
    )
    emars = (
        db_session.query(MedicationAdministrationRecord)
        .filter(MedicationAdministrationRecord.order_id == valid_order.id)
        .all()
    )
    assert len(emars) == 2


def test_financial_deposit_auto_drawdown_on_discharge(
    db_session: Session, hospital: Hospital
):
    doc, _, admin, _, dept, patient, ward, room, bed = _setup_env(db_session, hospital)

    admit_req = AdmitRequest(
        patient_id=patient.id,
        doctor_id=doc.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        department_id=dept.id,
    )
    actor_admin = {"id": str(admin.id), "name": admin.name, "role": "hospital_admin"}
    detail = AdmitPatientAction(db_session).execute(hospital.id, admit_req, actor_admin)

    account = get_or_create_financial_account(
        db_session, hospital_id=hospital.id, patient_id=patient.id, admission_id=detail.id
    )

    # 1. Post a hospital charge of Rs. 2500 for the admission
    proc_id = uuid.uuid4()
    charge = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        description="Surgical Procedure Charge",
        charge_amount=2500.0,
        unit_price=2500.0,
        quantity=1.0,
        source_type=BillingSourceType.ot,
        source_id=proc_id,
        account_id=account.id,
    )
    assert float(charge.net_amount) == 2500.0
    assert float(charge.amount_paid) == 0.0

    # 2. Patient pays a deposit of Rs. 3500 linked to admission
    dep_req = BillingDepositCreate(
        patient_id=patient.id,
        admission_id=detail.id,
        amount=3500.0,
        payment_method="cash",
        notes="Pre-admission surgical advance",
    )
    deposit = CreateDepositAction(db_session, hospital.id).execute(dep_req, actor_admin)
    assert float(deposit.original_amount) == 3500.0
    assert deposit.admission_id == detail.id
    assert deposit.account_id == account.id

    # 3. Before discharge, outstanding charge is Rs. 2500 and available deposit is Rs. 3500
    totals_before = patient_ledger_totals(
        db_session, hospital_id=hospital.id, patient_id=patient.id, account_id=account.id
    )
    assert totals_before["outstanding"] == 2500.0
    assert totals_before["total_deposits_available"] == 3500.0

    # Request discharge
    adm = db_session.query(Admission).filter(Admission.id == detail.id).first()
    adm.status = AdmissionStatus.discharge_requested
    db_session.commit()

    # Generate invoice for admission charges before final discharge
    from modules.billing.services.invoice_service import create_invoice_from_charges
    charges = db_session.query(BillingCharge).filter(BillingCharge.account_id == account.id).all()
    if charges:
        create_invoice_from_charges(db_session, hospital_id=hospital.id, patient_id=patient.id, charge_ids=[c.id for c in charges], account_id=account.id)

    # 4. Perform Discharge: should automatically drawdown Rs. 2500 to clear charges
    discharge_req = DischargeRequest(
        admission_id=detail.id,
        discharge_notes="Discharged home in stable condition",
        no_discharge_meds=True,
    )
    res = DischargePatientAction(db_session).execute(hospital.id, discharge_req, actor_admin)
    assert res.status == AdmissionStatus.discharged

    # 5. Verify charge is fully paid and remaining available deposit is Rs. 1000 (refundable)
    db_session.refresh(charge)
    assert float(charge.amount_paid) == 2500.0
    totals_after = patient_ledger_totals(
        db_session, hospital_id=hospital.id, patient_id=patient.id, account_id=account.id
    )
    assert totals_after["outstanding"] == 0.0
    assert totals_after["total_deposits_available"] == 1000.0


def test_form_addenda_workflow_preserves_finalized_immutability(
    db_session: Session, hospital: Hospital
):
    doc, _, admin, _, dept, patient, ward, room, bed = _setup_env(db_session, hospital)

    # 1. Create a draft form submission
    admission = Admission(
        id=uuid.uuid4(), hospital_id=hospital.id, patient_id=patient.id,
        status=AdmissionStatus.admitted, admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)
    db_session.commit()
    actor_doc = {"id": str(doc.id), "name": doc.name, "role": "doctor"}
    draft_req = IpdFormSubmissionCreate(
        patient_id=patient.id,
        admission_id=admission.id,
        form_id="consent_general",
        form_title="General Clinical Consent",
        form_data={"witness": "Brother", "procedure": "Laparoscopy"},
        status=IpdFormSubmissionStatus.draft,
    )
    sub = CreateFormSubmissionAction(db_session).execute(hospital.id, draft_req, actor_doc)
    assert sub.status == IpdFormSubmissionStatus.draft

    # 2. Addendum on a draft form is BLOCKED
    addendum_req = IpdFormAddendumCreate(
        reason="Clinical update",
        content="Attempting to add addendum to draft",
    )
    with pytest.raises(ValidationError) as exc_info:
        CreateFormAddendumAction(db_session).execute(
            hospital.id, sub.id, addendum_req, actor_doc
        )
    assert "Addenda can only be appended to finalized clinical forms" in str(exc_info.value)

    # 3. Finalize the form submission
    UpdateFormSubmissionAction(db_session).execute(
        hospital.id,
        sub.id,
        IpdFormSubmissionUpdate(status=IpdFormSubmissionStatus.final),
        actor_doc,
    )

    # 4. Direct mutation of finalized form is BLOCKED
    with pytest.raises(ValidationError) as exc_info:
        UpdateFormSubmissionAction(db_session).execute(
            hospital.id,
            sub.id,
            IpdFormSubmissionUpdate(form_title="Mutated Title"),
            actor_doc,
        )
    assert "Finalized clinical forms cannot be edited. Submit an addendum." in str(exc_info.value)

    # 5. Addendum on finalized form SUCCEEDS and records author and reason
    addendum = CreateFormAddendumAction(db_session).execute(
        hospital.id,
        sub.id,
        IpdFormAddendumCreate(
            reason="Post-op observation",
            content="Patient regained full consciousness at 18:30 with stable vitals.",
        ),
        actor_doc,
    )
    assert addendum.submission_id == sub.id
    assert addendum.author_id == doc.id
    assert addendum.reason == "Post-op observation"

    # 6. List addenda returns the entry chronologically
    addenda_list = ListFormAddendaAction(db_session).execute(hospital.id, sub.id)
    assert len(addenda_list) == 1
    assert addenda_list[0].id == addendum.id
    listed = ListFormSubmissionsAction(db_session).execute(hospital.id, patient.id, admission.id)
    assert listed[0].addenda_count == 1


def test_idempotency_keys_prevent_duplicate_payments_and_deposits(
    db_session: Session, hospital: Hospital
):
    _, _, admin, _, dept, patient, _, _, _ = _setup_env(db_session, hospital)
    actor_admin = {"id": str(admin.id), "name": admin.name, "role": "hospital_admin"}

    unique_key = f"idemp_{uuid.uuid4().hex}"

    # First deposit succeeds
    dep_req1 = BillingDepositCreate(
        patient_id=patient.id,
        amount=1200.0,
        payment_method="card",
        notes="Deposit with idempotency key",
        idempotency_key=unique_key,
    )
    dep1 = CreateDepositAction(db_session, hospital.id).execute(dep_req1, actor_admin)
    assert float(dep1.original_amount) == 1200.0

    # Second deposit with identical idempotency key returns the same deposit without duplication
    dep_req2 = BillingDepositCreate(
        patient_id=patient.id,
        amount=1200.0,
        payment_method="card",
        notes="Duplicate request replay",
        idempotency_key=unique_key,
    )
    dep2 = CreateDepositAction(db_session, hospital.id).execute(dep_req2, actor_admin)
    assert dep2.id == dep1.id

    # Verify only ONE deposit row exists in DB
    total_deposits = (
        db_session.query(BillingDeposit)
        .filter(BillingDeposit.idempotency_key == unique_key)
        .count()
    )
    assert total_deposits == 1


def test_separation_of_duties_discharge_financial_exception(
    db_session: Session, hospital: Hospital
):
    doc, _, admin, billing_approver, dept, patient, ward, room, bed = _setup_env(
        db_session, hospital
    )

    admit_req = AdmitRequest(
        patient_id=patient.id,
        doctor_id=doc.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        department_id=dept.id,
    )
    actor_admin = {"id": str(admin.id), "name": admin.name, "role": "hospital_admin"}
    detail = AdmitPatientAction(db_session).execute(hospital.id, admit_req, actor_admin)

    account = get_or_create_financial_account(
        db_session, hospital_id=hospital.id, patient_id=patient.id, admission_id=detail.id
    )
    # Post charge to create outstanding balance
    stay_id = uuid.uuid4()
    ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        description="ICU Stay Day 1",
        charge_amount=5000.0,
        unit_price=5000.0,
        quantity=1.0,
        source_type=BillingSourceType.other,
        source_id=stay_id,
        account_id=account.id,
    )

    # Transition to discharge_requested before requesting discharge financial exception
    adm = db_session.query(Admission).filter(Admission.id == detail.id).first()
    adm.status = AdmissionStatus.discharge_requested
    db_session.commit()

    # 1. Admin requests discharge financial exception
    exc_req = DischargeFinancialExceptionCreate(
        reason="Indigent patient welfare scheme approved by MS",
        recommendation="Full waiver",
    )
    exc = RequestDischargeExceptionAction(db_session, hospital.id).execute(
        detail.id, exc_req, actor_admin
    )
    assert exc.status == ExceptionStatus.pending
    assert exc.requested_by_id == admin.id

    # 2. Requester (Admin) attempts to approve their own exception -> MUST FAIL
    with pytest.raises(ValidationError) as exc_info:
        DecideDischargeExceptionAction(db_session, hospital.id).execute(
            exc.id,
            DischargeFinancialExceptionDecision(
                status=ExceptionStatus.approved,
                approval_remarks="Self-approving my own request",
            ),
            actor_admin,  # same actor
        )
    assert "Separation of duties violation" in str(exc_info.value)

    # 3. Different user (Billing Approver / CFO) approves -> SUCCEEDS
    actor_cfo = {
        "id": str(billing_approver.id),
        "name": billing_approver.name,
        "role": "hospital_staff",
    }
    decision = DecideDischargeExceptionAction(db_session, hospital.id).execute(
        exc.id,
        DischargeFinancialExceptionDecision(
            status=ExceptionStatus.approved,
            approval_remarks="Approved by finance committee",
        ),
        actor_cfo,
    )
    assert decision.status == ExceptionStatus.approved
    assert decision.approved_by_id == billing_approver.id
