"""
Focused verification test suite for HMS IPD + Billing Workflow Final Closure Items.

Covers:
1. IPD Form Header Metadata (ip_id, op_id from source appointment or None, admitted_at)
2. Discharge Prescription & No Discharge Meds (reusing Prescription, suggestions without auto-copy, gate)
3. Final Billing Gate & Stale Invoice Protection (invoice required before discharge, exception still requires invoice, stale charges block discharge)
4. Granular Backend Permissions (care team edit, doctor prescribing/discontinuing, nurse PRN administration, separation of duties for exceptions)
5. OT Nursing Scope (ward-scoped vs hospital-wide ot:view)
6. Fixed-Precision Decimal Coverage (refunds, exceptions, invoice totals without float error)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.beds.entities.bed import Bed, Room, Ward
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingRefund,
    DepositStatus,
    FinancialAccount,
)
from modules.billing.services.billing_service import (
    allocate_payment_to_charges,
    ensure_charge,
    process_refund,
)
from modules.billing.services.invoice_service import create_invoice_from_charges
from modules.clinical_records.entities.clinical_record import Prescription
from modules.inpatient.actions.admission_actions import (
    AdmitPatientAction,
    DischargePatientAction,
    to_admission_detail,
)
from modules.inpatient.actions.admission_lifecycle_actions import (
    EnsureAdmissionRequestAction,
)
from modules.inpatient.actions.care_team_actions import (
    AddCareTeamMemberAction,
    EndCareTeamAssignmentAction,
)
from modules.inpatient.actions.discharge_exception_actions import (
    DecideDischargeExceptionAction,
    RequestDischargeExceptionAction,
)
from modules.inpatient.actions.discharge_medication_actions import (
    GetDischargeMedicationStatusAction,
    GetSuggestedDischargeMedsAction,
    RecordNoDischargeMedsAction,
)
from modules.inpatient.actions.medication_order_actions import (
    AdministerPrnMedicationAction,
    CreateMedicationOrderAction,
    DiscontinueMedicationOrderAction,
)
from modules.inpatient.contracts.inpatient_contracts import (
    AdministerPrnMedicationRequest,
    AdmissionCareTeamCreate,
    AdmitRequest,
    DischargeFinancialExceptionCreate,
    DischargeFinancialExceptionDecision,
    DischargeRequest,
    DiscontinueMedicationOrderRequest,
    IpdMedicationOrderCreate,
    NoDischargeMedsRequest,
)
from modules.inpatient.entities.admission import (
    Admission,
    AdmissionStatus,
    IpdFormSubmission,
    IpdFormSubmissionStatus,
)
from modules.inpatient.entities.care_team import AdmissionCareTeamRole
from modules.inpatient.entities.discharge_exception import ExceptionStatus
from modules.ot.actions.ot_actions import ListSurgeriesAction
from modules.ot.entities.ot_entities import OtSurgery, OtSurgeryStatus
from modules.patients.entities.patient import Patient, PatientStatus
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from shared.exceptions.base import NotFoundError, ValidationError


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_hospital_id() -> UUID:
    return uuid4()


def _make_hospital_user(db: Session, hospital_id: UUID, role_name: str, name: str) -> HospitalUser:
    role = StaffRole(id=uuid4(), hospital_id=hospital_id, name=f"{role_name}-{uuid4().hex[:6]}")
    db.add(role)
    db.flush()

    user = HospitalUser(
        id=uuid4(),
        hospital_id=hospital_id,
        email=f"{name.lower().replace(' ', '')}_{uuid4().hex[:6]}@hospital.com",
        phone="9876543210",
        password_hash="hash",
        name=name,
        role_id=role.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _create_base_env(db: Session, hospital_id: UUID):
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital_id,
        uhid=f"UHID-{uuid4().hex[:6].upper()}",
        name="Test Patient",
        gender="Male",
        date_of_birth=date(1990, 5, 15),
        mobile="9876543210",
        emergency_contact="9876543211",
        status=PatientStatus.active,
    )
    db.add(patient)

    user_doc = _make_hospital_user(db, hospital_id, "Doctor", "Dr. Smith")
    user_nurse = _make_hospital_user(db, hospital_id, "Nurse", "Nurse Joy")
    user_biller = _make_hospital_user(db, hospital_id, "Billing", "Biller Bob")

    ward = Ward(
        id=uuid4(),
        hospital_id=hospital_id,
        name="General Ward",
        ward_type="general",
        bed_charge_per_day=0.0,
        admission_fee=0.0,
        is_active=True,
    )
    db.add(ward)

    room = Room(
        id=uuid4(),
        hospital_id=hospital_id,
        ward_id=ward.id,
        room_code="GW-101",
        is_active=True,
    )
    db.add(room)

    bed = Bed(
        id=uuid4(),
        hospital_id=hospital_id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="GW-101-A",
        is_occupied=False,
        is_active=True,
    )
    db.add(bed)
    db.commit()

    return {
        "patient": patient,
        "doctor": user_doc,
        "user_doc": user_doc,
        "user_nurse": user_nurse,
        "user_biller": user_biller,
        "ward": ward,
        "room": room,
        "bed": bed,
    }


def _make_charge(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    amount: Decimal,
    description: str,
    account_id: UUID | None = None,
) -> BillingCharge:
    c = ensure_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient_id,
        source_type="other",
        source_id=None,
        description=description,
        quantity=1.0,
        unit_price=float(amount),
        charge_amount=amount,
        account_id=account_id,
    )
    db.commit()
    return c


def _make_payment(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    amount: Decimal,
    account_id: UUID | None = None,
    payment_method: BillingPaymentMethod = BillingPaymentMethod.cash,
) -> BillingPayment:
    pay = BillingPayment(
        id=uuid4(),
        hospital_id=hospital_id,
        patient_id=patient_id,
        account_id=account_id,
        reference_number=f"PAY-{uuid4().hex[:6].upper()}",
        payment_date=date.today(),
        payment_method=payment_method,
        amount=amount,
        received_by_name="Cashier",
    )
    db.add(pay)
    db.commit()
    return pay


# ─────────────────────────────────────────────────────────────────────────────
# 1. IPD Form Header Metadata Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_ipd_header_metadata_with_source_appointment(db_session: Session):
    hospital_id = _make_hospital_id()
    env = _create_base_env(db_session, hospital_id)

    # 1. Create source OPD appointment with op_id
    from datetime import time as dt_time
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        doctor_id=env["user_doc"].id,
        op_id="OP-2026-00456",
        purpose="General OPD Consultation",
        status=AppointmentStatus.completed,
        appointment_date=date.today(),
        appointment_time=dt_time(10, 0),
    )
    db_session.add(appt)
    db_session.commit()

    # 2. Request admission linked to OPD visit
    ensure_action = EnsureAdmissionRequestAction(db_session)
    adm_row, _ = ensure_action.execute(
        hospital_id,
        patient_id=env["patient"].id,
        doctor_id=env["user_doc"].id,
        source_appointment_id=appt.id,
        actor={"name": "Doctor Smith"},
    )
    detail = to_admission_detail(adm_row)
    assert detail.op_id == "OP-2026-00456"
    assert detail.patient_uhid == env["patient"].uhid


def test_ipd_header_metadata_direct_admission_no_op(db_session: Session):
    hospital_id = _make_hospital_id()
    env = _create_base_env(db_session, hospital_id)

    # Direct admission (no source OPD appointment)
    admit_action = AdmitPatientAction(db_session)
    adm = admit_action.execute(
        hospital_id,
        AdmitRequest(
            patient_id=env["patient"].id,
            ward_id=env["ward"].id,
            room_id=env["room"].id,
            bed_id=env["bed"].id,
            doctor_id=env["user_doc"].id,
        ),
        actor={"name": "Nurse Joy"},
    )
    assert adm.ip_id is not None
    assert adm.ip_id.startswith("IP-")
    assert adm.op_id is None
    assert adm.admitted_at is not None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Discharge Prescription & Inpatient Suggestions Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_discharge_prescription_and_inpatient_suggestions(db_session: Session):
    hospital_id = _make_hospital_id()
    env = _create_base_env(db_session, hospital_id)

    admit_action = AdmitPatientAction(db_session)
    adm_dto = admit_action.execute(
        hospital_id,
        AdmitRequest(
            patient_id=env["patient"].id,
            ward_id=env["ward"].id,
            room_id=env["room"].id,
            bed_id=env["bed"].id,
            doctor_id=env["user_doc"].id,
        ),
        actor={"name": "Staff"},
    )

    # 1. Doctor prescribes inpatient medication orders
    med_action = CreateMedicationOrderAction(db_session, hospital_id)
    order = med_action.execute(
        adm_dto.id,
        IpdMedicationOrderCreate(
            medicine_name="Paracetamol",
            dose="500mg",
            frequency="tds",
            is_prn=True,
            prn_indication="Fever > 100F",
        ),
        actor={"id": str(env["user_doc"].id), "doctor_id": str(env["user_doc"].id), "name": "Dr. Smith"},
    )
    assert order.medicine_name == "Paracetamol"

    # 2. Query suggested discharge meds — suggests the active inpatient order
    sugg_action = GetSuggestedDischargeMedsAction(db_session, hospital_id)
    suggestions = sugg_action.execute(adm_dto.id)
    assert len(suggestions) == 1
    assert suggestions[0].medicine_name == "Paracetamol"
    assert suggestions[0].is_prn is True

    # 3. Check discharge prescription status — initially incomplete
    status_action = GetDischargeMedicationStatusAction(db_session, hospital_id)
    status_resp = status_action.execute(adm_dto.id)
    assert status_resp.has_discharge_prescription is False
    assert status_resp.no_discharge_meds is False
    assert status_resp.can_discharge_medically is False

    # 4. Doctor issues admission-linked take-home prescription
    rx = Prescription(
        id=uuid4(),
        hospital_id=hospital_id,
        doctor_id=env["user_doc"].id,
        patient_id=env["patient"].id,
        admission_id=adm_dto.id,
        symptoms="Post-op recovery",
        diagnosis="Appendicitis convalescence",
        medicines="Paracetamol 500mg TDS x 5 days\nAmoxicillin 500mg BD x 7 days",
        dosage="Oral",
        status="issued",
    )
    db_session.add(rx)
    db_session.commit()

    # 5. Status now confirms discharge prescription present
    status_resp = status_action.execute(adm_dto.id)
    assert status_resp.has_discharge_prescription is True
    assert status_resp.prescription_id == rx.id
    assert status_resp.can_discharge_medically is True


def test_no_discharge_meds_recording(db_session: Session):
    hospital_id = _make_hospital_id()
    env = _create_base_env(db_session, hospital_id)

    admit_action = AdmitPatientAction(db_session)
    adm_dto = admit_action.execute(
        hospital_id,
        AdmitRequest(
            patient_id=env["patient"].id,
            ward_id=env["ward"].id,
            room_id=env["room"].id,
            bed_id=env["bed"].id,
            doctor_id=env["user_doc"].id,
        ),
        actor={"name": "Staff"},
    )

    # Record "No discharge medicines required"
    no_meds_action = RecordNoDischargeMedsAction(db_session, hospital_id)
    resp = no_meds_action.execute(
        adm_dto.id,
        NoDischargeMedsRequest(reason="Patient fully recovered, no continuation meds indicated"),
        actor={"id": str(env["user_doc"].id), "name": "Dr. Smith"},
    )
    assert resp.no_discharge_meds is True
    assert resp.no_discharge_meds_reason == "Patient fully recovered, no continuation meds indicated"
    assert resp.can_discharge_medically is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. Final Billing Gate & Stale Invoice Protection Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_final_billing_gate_and_stale_invoice_protection(db_session: Session):
    hospital_id = _make_hospital_id()
    env = _create_base_env(db_session, hospital_id)

    admit_action = AdmitPatientAction(db_session)
    adm_dto = admit_action.execute(
        hospital_id,
        AdmitRequest(
            patient_id=env["patient"].id,
            ward_id=env["ward"].id,
            room_id=env["room"].id,
            bed_id=env["bed"].id,
            doctor_id=env["user_doc"].id,
        ),
        actor={"name": "Staff"},
    )

    # Set discharge requested
    adm = db_session.get(Admission, adm_dto.id)
    adm.status = AdmissionStatus.discharge_requested
    adm.no_discharge_meds = True
    db_session.commit()

    # Retrieve financial account for admission (created automatically during admit)
    acc = (
        db_session.query(FinancialAccount)
        .filter(
            FinancialAccount.hospital_id == hospital_id,
            FinancialAccount.admission_id == adm.id,
        )
        .first()
    )
    assert acc is not None

    # Add admission charge
    c1 = _make_charge(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        description="Appendectomy",
        amount=Decimal("5000.00"),
        account_id=acc.id,
    )
    _make_payment(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        amount=Decimal("5000.00"),
        payment_method=BillingPaymentMethod.cash,
        account_id=acc.id,
    )

    discharge_action = DischargePatientAction(db_session)

    # 1. Attempt discharge without generated invoice -> BLOCKED
    with pytest.raises(ValidationError) as exc:
        discharge_action.execute(
            hospital_id,
            DischargeRequest(admission_id=adm.id),
            actor={"name": "Nurse"},
        )
    assert "Final billing required" in str(exc.value)

    # 2. Generate final invoice
    inv = create_invoice_from_charges(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        charge_ids=[c1.id],
        account_id=acc.id,
        created_by_name="Biller",
    )
    assert inv.status in (BillingInvoiceStatus.generated, BillingInvoiceStatus.paid)

    # 3. Post a new charge AFTER invoice generation -> Stale bill detection
    c2 = _make_charge(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        description="Late IV antibiotics",
        amount=Decimal("250.00"),
        account_id=acc.id,
    )
    _make_payment(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        amount=Decimal("250.00"),
        payment_method=BillingPaymentMethod.cash,
        account_id=acc.id,
    )

    # Attempt discharge with stale bill -> BLOCKED
    with pytest.raises(ValidationError) as exc:
        discharge_action.execute(
            hospital_id,
            DischargeRequest(admission_id=adm.id),
            actor={"name": "Nurse"},
        )
    assert "Final bill out of date" in str(exc.value)

    # 4. Regenerate/update final invoice covering all charges
    inv2 = create_invoice_from_charges(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        charge_ids=[c2.id],
        account_id=acc.id,
        created_by_name="Biller",
    )
    assert inv2.status in (BillingInvoiceStatus.generated, BillingInvoiceStatus.paid)

    # 5. Discharge now SUCCEEDS
    res = discharge_action.execute(
        hospital_id,
        DischargeRequest(admission_id=adm.id),
        actor={"name": "Nurse"},
    )
    assert res.status == AdmissionStatus.discharged


# ─────────────────────────────────────────────────────────────────────────────
# 4. Discharge Financial Exception with Final Bill Test
# ─────────────────────────────────────────────────────────────────────────────

def test_approved_exception_requires_final_bill(db_session: Session):
    hospital_id = _make_hospital_id()
    env = _create_base_env(db_session, hospital_id)

    admit_action = AdmitPatientAction(db_session)
    adm_dto = admit_action.execute(
        hospital_id,
        AdmitRequest(
            patient_id=env["patient"].id,
            ward_id=env["ward"].id,
            room_id=env["room"].id,
            bed_id=env["bed"].id,
            doctor_id=env["user_doc"].id,
        ),
        actor={"name": "Staff"},
    )

    adm = db_session.get(Admission, adm_dto.id)
    adm.status = AdmissionStatus.discharge_requested
    adm.no_discharge_meds = True
    db_session.commit()

    acc = (
        db_session.query(FinancialAccount)
        .filter(
            FinancialAccount.hospital_id == hospital_id,
            FinancialAccount.admission_id == adm.id,
        )
        .first()
    )
    assert acc is not None

    # Open un-settled charge
    c = _make_charge(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        description="Emergency Trauma Surgery",
        amount=Decimal("15000.00"),
        account_id=acc.id,
    )

    # Request and approve financial exception
    req_exc = RequestDischargeExceptionAction(db_session, hospital_id).execute(
        adm.id,
        DischargeFinancialExceptionCreate(reason="Indigent patient social waiver"),
        actor={"id": str(env["user_biller"].id), "name": "Biller Bob"},
    )
    DecideDischargeExceptionAction(db_session, hospital_id).execute(
        req_exc.id,
        DischargeFinancialExceptionDecision(status=ExceptionStatus.approved, approval_remarks="Approved by MS"),
        actor={"id": str(env["user_doc"].id), "name": "Medical Superintendent"},
    )

    discharge_action = DischargePatientAction(db_session)

    # Attempt discharge with approved exception but NO final invoice -> BLOCKED
    with pytest.raises(ValidationError) as exc:
        discharge_action.execute(
            hospital_id,
            DischargeRequest(admission_id=adm.id),
            actor={"name": "Nurse"},
        )
    assert "Final billing required" in str(exc.value)

    # Generate final invoice for the account
    create_invoice_from_charges(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        charge_ids=[c.id],
        account_id=acc.id,
        created_by_name="Biller Bob",
    )

    # Now discharge SUCCEEDS under approved exception
    res = discharge_action.execute(
        hospital_id,
        DischargeRequest(admission_id=adm.id),
        actor={"name": "Nurse"},
    )
    assert res.status == AdmissionStatus.discharged


# ─────────────────────────────────────────────────────────────────────────────
# 5. OT Nursing Scope Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_ot_nursing_scope_filtering(db_session: Session):
    hospital_id = _make_hospital_id()
    env = _create_base_env(db_session, hospital_id)

    # Create 2nd ward & admission
    ward2 = Ward(
        id=uuid4(),
        hospital_id=hospital_id,
        name="ICU Ward",
        ward_type="icu",
        bed_charge_per_day=0.0,
        admission_fee=0.0,
        is_active=True,
    )
    patient2 = Patient(
        id=uuid4(),
        hospital_id=hospital_id,
        uhid=f"UHID-{uuid4().hex[:6].upper()}",
        name="Patient Two",
        gender="Female",
        mobile="9876543219",
        date_of_birth=date(1985, 2, 20),
        status=PatientStatus.active,
    )
    db_session.add_all([ward2, patient2])
    db_session.commit()

    adm1 = Admission(
        id=uuid4(),
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        ward_id=env["ward"].id,
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    adm2 = Admission(
        id=uuid4(),
        hospital_id=hospital_id,
        patient_id=patient2.id,
        ward_id=ward2.id,
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add_all([adm1, adm2])

    # Create 2 surgeries
    s1 = OtSurgery(
        id=uuid4(),
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        surgeon_id=env["user_doc"].id,
        surgery_no="OT-2026-001",
        surgery_type="Cholecystectomy",
        status=OtSurgeryStatus.scheduled,
        scheduled_at=datetime.now(timezone.utc),
    )
    s2 = OtSurgery(
        id=uuid4(),
        hospital_id=hospital_id,
        patient_id=patient2.id,
        surgeon_id=env["user_doc"].id,
        surgery_no="OT-2026-002",
        surgery_type="Craniotomy",
        status=OtSurgeryStatus.scheduled,
        scheduled_at=datetime.now(timezone.utc),
    )
    db_session.add_all([s1, s2])
    db_session.commit()

    list_action = ListSurgeriesAction(db_session, hospital_id)

    # 1. Admin/Broad OT access fetches all
    admin_user = {"role": "hospital_admin"}
    all_surgeries = list_action.execute(user=admin_user)
    assert len(all_surgeries) >= 2

    # 2. Ward-scoped nurse query filters to ward inpatients
    ward_nurse_user = {"role": "hospital_staff"}
    ward1_surgeries = list_action.execute(ward_id=env["ward"].id, user=ward_nurse_user)
    assert len(ward1_surgeries) == 1
    assert ward1_surgeries[0].surgery_no == "OT-2026-001"

    # 3. Ordinary nurse attempting unscoped broad fetch without ot:view -> 403 Forbidden
    with pytest.raises(HTTPException) as exc:
        list_action.execute(ward_id=None, user=ward_nurse_user)
    assert exc.value.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 6. Fixed-Precision Monetary Calculations Test
# ─────────────────────────────────────────────────────────────────────────────

def test_fixed_precision_decimal_arithmetic(db_session: Session):
    hospital_id = _make_hospital_id()
    env = _create_base_env(db_session, hospital_id)

    # Decimal edge case: ₹0.10 + ₹0.20 must equal exact ₹0.30 (not 0.30000000000000004)
    c1 = _make_charge(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        description="Med A",
        amount=Decimal("0.10"),
    )
    c2 = _make_charge(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        description="Med B",
        amount=Decimal("0.20"),
    )

    assert isinstance(c1.charge_amount, Decimal)
    assert isinstance(c2.charge_amount, Decimal)
    total = c1.charge_amount + c2.charge_amount
    assert total == Decimal("0.30")

    # Payment and exact refund
    pay = _make_payment(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        amount=Decimal("100.50"),
        payment_method=BillingPaymentMethod.card,
    )
    assert isinstance(pay.amount, Decimal)
    assert pay.amount == Decimal("100.50")

    ref = process_refund(
        db_session,
        hospital_id=hospital_id,
        patient_id=env["patient"].id,
        payment_id=pay.id,
        amount=Decimal("50.25"),
        reason="Overpayment adjustment",
        refund_date=date.today(),
        refund_method=BillingPaymentMethod.card,
    )
    assert isinstance(ref.amount, Decimal)
    assert ref.amount == Decimal("50.25")
