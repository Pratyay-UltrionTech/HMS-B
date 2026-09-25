"""
Comprehensive test suite for:
1. IPD Care Team management (primary consultant, secondary, end assignment)
2. IPD Medication Orders & decoupled eMAR schedule generation & discontinuance
3. Staged Discharge & Separation of Duties for Financial Exceptions
4. Billing Deposits with admission linkage & ledger calculations
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from modules.beds.entities.bed import Bed, Room, Ward
from modules.billing.actions.billing_actions import (
    AllocateDepositAction,
    CreateDepositAction,
    CreateRefundAction,
    ListDepositsAction,
)
from modules.billing.contracts.billing_contracts import (
    BillingDepositAllocate,
    BillingDepositCreate,
    ChargeAllocationItem,
)
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    DepositStatus,
    BillingSourceType,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
)
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.masters.entities.organization_entities import Department
from modules.inpatient.actions.admission_actions import (
    AdmitPatientAction,
    DischargePatientAction,
    RequestDischargeAction,
)
from modules.inpatient.actions.care_team_actions import (
    AddCareTeamMemberAction,
    EndCareTeamAssignmentAction,
    ListCareTeamAction,
)
from modules.inpatient.actions.discharge_exception_actions import (
    DecideDischargeExceptionAction,
    ListDischargeExceptionsAction,
    RequestDischargeExceptionAction,
)
from modules.inpatient.actions.medication_order_actions import (
    CreateMedicationOrderAction,
    DiscontinueMedicationOrderAction,
    ListMedicationOrdersAction,
)
from modules.inpatient.contracts.inpatient_contracts import (
    AdmissionCareTeamCreate,
    AdmitRequest,
    DischargeFinancialExceptionCreate,
    DischargeFinancialExceptionDecision,
    DischargeRequest,
    DischargeRequestCreate,
    DiscontinueMedicationOrderRequest,
    IpdMedicationOrderCreate,
)
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.inpatient.entities.care_team import AdmissionCareTeamRole
from modules.inpatient.entities.discharge_exception import ExceptionStatus
from modules.inpatient.entities.medication_order import MedicationOrderStatus
from modules.inpatient.entities.nursing_entities import (
    MedicationAdminStatus,
    MedicationAdministrationRecord,
)
from modules.inpatient.services.admission_chart_service import AdmissionChartService
from modules.inpatient.services.inpatient_billing_service import InpatientBillingService
from modules.patients.entities.patient import Patient, PatientStatus
from modules.tenancy.entities.hospital import Hospital
from shared.exceptions.base import ValidationError
from tests.conftest import db_session, hospital  # noqa: F401


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


def _setup_ipd_environment(db: Session, hospital: Hospital):
    doctor1 = _make_hospital_user(db, hospital.id, "Doctor", "Dr. Alice Sharma")
    doctor2 = _make_hospital_user(db, hospital.id, "Doctor", "Dr. Bob Mehta")
    billing_admin = _make_hospital_user(db, hospital.id, "Admin", "CFO Charlie")

    dept = Department(
        hospital_id=hospital.id,
        name=f"Internal Medicine {uuid4().hex[:4]}",
    )
    db.add(dept)
    db.commit()
    db.refresh(dept)

    patient = Patient(
        hospital_id=hospital.id,
        uhid=f"UHID-TEST-{uuid4().hex[:6]}",
        name="John Doe",
        mobile=f"98765{uuid4().hex[:5]}",
        gender="male",
        status=PatientStatus.active,
    )
    db.add(patient)

    ward = Ward(
        hospital_id=hospital.id,
        name=f"General Ward {uuid4().hex[:4]}",
        ward_type="general",
        admission_fee=500.0,
        bed_charge_per_day=1000.0,
    )
    db.add(ward)
    db.commit()
    db.refresh(ward)

    room = Room(
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_code=f"R-{uuid4().hex[:4]}",
    )
    db.add(room)
    db.commit()
    db.refresh(room)

    bed = Bed(
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code=f"B-{uuid4().hex[:4]}",
        is_occupied=False,
    )
    db.add(bed)
    db.commit()
    db.refresh(bed)

    return doctor1, doctor2, billing_admin, dept, patient, ward, room, bed


def test_care_team_lifecycle_and_primary_consultant_handling(db_session: Session, hospital: Hospital):
    doc1, doc2, admin, dept, patient, ward, room, bed = _setup_ipd_environment(db_session, hospital)

    # 1. Admit patient with doctor1 as primary consultant
    admit_req = AdmitRequest(
        patient_id=patient.id,
        doctor_id=doc1.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        department_id=dept.id,
        department_name=dept.name,
        notes="Clinical care team test",
    )
    actor = {"id": str(admin.id), "name": admin.name, "role": "hospital_admin"}
    detail = AdmitPatientAction(db_session).execute(hospital.id, admit_req, actor)

    assert detail.doctor_id == doc1.id
    assert detail.department_id == dept.id

    # 2. Check care team has doc1 as primary_consultant and admitting_doctor
    care_team = ListCareTeamAction(db_session, hospital.id).execute(detail.id)
    assert len(care_team) == 2
    roles = {m.role: m for m in care_team}
    assert AdmissionCareTeamRole.primary_consultant in roles
    assert AdmissionCareTeamRole.admitting_doctor in roles
    assert roles[AdmissionCareTeamRole.primary_consultant].doctor_id == doc1.id
    assert roles[AdmissionCareTeamRole.primary_consultant].is_active is True
    assert roles[AdmissionCareTeamRole.admitting_doctor].doctor_id == doc1.id
    assert roles[AdmissionCareTeamRole.admitting_doctor].is_active is True

    # 3. Add doc2 as consulting_doctor
    add_req = AdmissionCareTeamCreate(
        doctor_id=doc2.id,
        role=AdmissionCareTeamRole.consulting_doctor,
        notes="Cardiology consult",
    )
    member2 = AddCareTeamMemberAction(db_session, hospital.id).execute(detail.id, add_req, actor)
    assert member2.doctor_id == doc2.id
    assert member2.role == AdmissionCareTeamRole.consulting_doctor
    assert member2.is_active is True

    # 4. Reassign primary consultant to doc2 -> should deactivate doc1's primary assignment
    reassign_req = AdmissionCareTeamCreate(
        doctor_id=doc2.id,
        role=AdmissionCareTeamRole.primary_consultant,
        notes="Transferred primary lead to Dr. Bob",
    )
    AddCareTeamMemberAction(db_session, hospital.id).execute(detail.id, reassign_req, actor)

    all_members = ListCareTeamAction(db_session, hospital.id).execute(detail.id, active_only=False)
    active_members = ListCareTeamAction(db_session, hospital.id).execute(detail.id, active_only=True)

    # doc1 primary should now be inactive
    doc1_records = [m for m in all_members if m.doctor_id == doc1.id]
    assert any(m.is_active is False for m in doc1_records)

    # doc2 should now be active primary
    active_primary = [m for m in active_members if m.role == AdmissionCareTeamRole.primary_consultant]
    assert len(active_primary) == 1
    assert active_primary[0].doctor_id == doc2.id

    # 5. End doc2's primary assignment
    EndCareTeamAssignmentAction(db_session, hospital.id).execute(active_primary[0].id, actor)
    active_after_end = ListCareTeamAction(db_session, hospital.id).execute(detail.id, active_only=True)
    assert not any(m.id == active_primary[0].id for m in active_after_end)


def test_ipd_medication_orders_and_emar_integration(db_session: Session, hospital: Hospital):
    doc1, _, admin, dept, patient, ward, room, bed = _setup_ipd_environment(db_session, hospital)

    admit_req = AdmitRequest(
        patient_id=patient.id,
        doctor_id=doc1.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        notes="Medication order test",
    )
    actor = {"id": str(doc1.id), "name": doc1.name, "role": "doctor"}
    detail = AdmitPatientAction(db_session).execute(hospital.id, admit_req, actor)

    # 1. Doctor creates medication order: Amoxicillin 500mg, Oral, TDS (3 doses/day) for 2 days
    med_req = IpdMedicationOrderCreate(
        medicine_name="Amoxicillin",
        dose="500mg",
        route="oral",
        frequency="TDS",
        duration_days=2,
        instructions="After meals",
    )
    order = CreateMedicationOrderAction(db_session, hospital.id).execute(detail.id, med_req, actor)
    assert order.status == MedicationOrderStatus.active
    assert order.medicine_name == "Amoxicillin"

    # 2. Check that 6 eMAR doses (3 times * 2 days) were auto-generated linked via order_id
    emars = (
        db_session.query(MedicationAdministrationRecord)
        .filter(
            MedicationAdministrationRecord.hospital_id == hospital.id,
            MedicationAdministrationRecord.order_id == order.id,
        )
        .order_by(MedicationAdministrationRecord.scheduled_time.asc())
        .all()
    )
    assert len(emars) == 6
    assert all(e.status == MedicationAdminStatus.scheduled for e in emars)
    assert all(e.medicine_name == "Amoxicillin" for e in emars)

    # 3. Nurse administers first dose -> does not mutate doctor's medication order
    first_dose = emars[0]
    first_dose.status = MedicationAdminStatus.administered
    first_dose.administered_time = datetime.now(timezone.utc)
    first_dose.administered_by_name = "Nurse Joy"
    db_session.commit()

    active_orders = ListMedicationOrdersAction(db_session, hospital.id).execute(detail.id)
    assert len(active_orders) == 1
    assert active_orders[0].status == MedicationOrderStatus.active

    # 4. Doctor discontinues the medication order
    disc_req = DiscontinueMedicationOrderRequest(reason="Adverse skin rash observed")
    disc_order = DiscontinueMedicationOrderAction(db_session, hospital.id).execute(
        order.id, disc_req, actor
    )
    assert disc_order.status == MedicationOrderStatus.discontinued
    assert disc_order.discontinued_reason == "Adverse skin rash observed"

    # 5. Check eMAR records: administered dose remains administered; remaining due doses are withheld
    refreshed_emars = (
        db_session.query(MedicationAdministrationRecord)
        .filter(MedicationAdministrationRecord.order_id == order.id)
        .order_by(MedicationAdministrationRecord.scheduled_time.asc())
        .all()
    )
    assert refreshed_emars[0].status == MedicationAdminStatus.administered
    for d in refreshed_emars[1:]:
        assert d.status == MedicationAdminStatus.withheld


def test_staged_discharge_financial_exception_and_separation_of_duties(db_session: Session, hospital: Hospital):
    doc1, _, admin, dept, patient, ward, room, bed = _setup_ipd_environment(db_session, hospital)

    admit_req = AdmitRequest(
        patient_id=patient.id,
        doctor_id=doc1.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        notes="Discharge exception test",
    )
    doctor_actor = {"id": str(doc1.id), "name": doc1.name, "role": "doctor"}
    admin_actor = {"id": str(admin.id), "name": admin.name, "role": "hospital_admin"}

    detail = AdmitPatientAction(db_session).execute(hospital.id, admit_req, doctor_actor)

    # Request discharge
    RequestDischargeAction(db_session).execute(
        hospital.id, DischargeRequestCreate(admission_id=detail.id), doctor_actor
    )

    # Generate final invoice for existing charges before clearance check
    from modules.billing.entities.billing_entities import BillingCharge, FinancialAccount
    from modules.billing.services.invoice_service import create_invoice_from_charges
    acc = db_session.query(FinancialAccount).filter(FinancialAccount.hospital_id == hospital.id, FinancialAccount.admission_id == detail.id).first()
    charges = db_session.query(BillingCharge).filter(BillingCharge.account_id == acc.id).all()
    if charges:
        create_invoice_from_charges(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            charge_ids=[c.id for c in charges],
            account_id=acc.id,
        )

    # 1. Attempt discharge without clearance -> blocked by outstanding net balance
    with pytest.raises(ValidationError) as exc:
        DischargePatientAction(db_session).execute(
            hospital.id, DischargeRequest(admission_id=detail.id, no_discharge_meds=True), doctor_actor
        )
    assert "outstanding balance" in str(exc.value)

    # 2. Doctor requests a financial exception
    exc_req = DischargeFinancialExceptionCreate(
        reason="Patient family unable to pay remaining dues immediately; signed promissory note",
        recommendation="Discharge granted on humanitarian grounds; follow up for collection",
    )
    exception = RequestDischargeExceptionAction(db_session, hospital.id).execute(
        detail.id, exc_req, doctor_actor
    )
    assert exception.status == ExceptionStatus.pending
    assert exception.requested_by_id == doc1.id

    # 3. Separation of duties: Doctor cannot approve their own exception request!
    self_decision = DischargeFinancialExceptionDecision(
        status=ExceptionStatus.approved,
        approval_remarks="I approve my own request",
    )
    with pytest.raises(ValidationError) as exc_self:
        DecideDischargeExceptionAction(db_session, hospital.id).execute(
            exception.id, self_decision, doctor_actor
        )
    assert "Separation of duties" in str(exc_self.value)

    # 4. Administrator approves the exception
    admin_decision = DischargeFinancialExceptionDecision(
        status=ExceptionStatus.approved,
        approval_remarks="Approved per administration guidelines. Keep account open.",
    )
    decided_exc = DecideDischargeExceptionAction(db_session, hospital.id).execute(
        exception.id, admin_decision, admin_actor
    )
    assert decided_exc.status == ExceptionStatus.approved
    assert decided_exc.approved_by_id == admin.id

    # 5. Discharge now succeeds with bed freed, but financial account remains open
    discharge_res = DischargePatientAction(db_session).execute(
        hospital.id, DischargeRequest(admission_id=detail.id, no_discharge_meds=True), doctor_actor
    )
    assert discharge_res.status == AdmissionStatus.discharged

    db_session.refresh(bed)
    assert bed.is_occupied is False

    # Financial account remains open for receivable collection
    fin_acct = (
        db_session.query(FinancialAccount)
        .filter(
            FinancialAccount.hospital_id == hospital.id,
            FinancialAccount.admission_id == detail.id,
        )
        .first()
    )
    assert fin_acct is not None
    assert fin_acct.status == FinancialAccountStatus.open


def test_billing_deposit_workflow_with_admission(db_session: Session, hospital: Hospital):
    doc1, _, admin, dept, patient, ward, room, bed = _setup_ipd_environment(db_session, hospital)

    admit_req = AdmitRequest(
        patient_id=patient.id,
        doctor_id=doc1.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        notes="Deposit workflow test",
    )
    admin_actor = {"id": str(admin.id), "name": admin.name, "role": "hospital_admin"}
    detail = AdmitPatientAction(db_session).execute(hospital.id, admit_req, admin_actor)

    # 1. Collect advance deposit for admission: 10,000
    dep_req = BillingDepositCreate(
        patient_id=patient.id,
        admission_id=detail.id,
        amount=10000.0,
        payment_method="cash",
        account_type=FinancialAccountType.ipd,
        notes="Initial IPD admission deposit",
    )
    deposit = CreateDepositAction(db_session, hospital.id).execute(dep_req, admin_actor)
    assert deposit.admission_id == detail.id
    assert deposit.original_amount == 10000.0
    assert deposit.available_amount == 10000.0
    assert deposit.status == DepositStatus.available

    # 2. Check ledger totals reflect deposit
    billing_svc = InpatientBillingService(db_session)
    totals = billing_svc.get_ledger_totals(hospital.id, patient.id, admission_id=detail.id)
    assert totals["total_deposits_available"] == 10000.0
    assert totals["net_patient_balance"] <= 0.0  # Charges (500 + bed) covered by available deposit

    # 3. List deposits filtered by admission_id
    deposits = ListDepositsAction(db_session, hospital.id).execute(admission_id=detail.id)
    assert len(deposits) == 1
    assert deposits[0].id == deposit.id

    # 4. Partial allocation towards a charge
    charges = (
        db_session.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital.id,
            BillingCharge.patient_id == patient.id,
        )
        .all()
    )
    assert len(charges) > 0
    charge = charges[0]

    alloc_res = AllocateDepositAction(db_session, hospital.id).execute(
        deposit.id,
        BillingDepositAllocate(
            allocations=[
                ChargeAllocationItem(
                    charge_id=charge.id,
                    amount=charge.net_amount,
                )
            ]
        ),
        admin_actor,
    )
    assert alloc_res.available_amount == 10000.0 - float(charge.net_amount)


def test_admission_chart_service_aggregates_all_clinical_and_financial_data(db_session: Session, hospital: Hospital):
    doc1, _, admin, dept, patient, ward, room, bed = _setup_ipd_environment(db_session, hospital)

    admit_req = AdmitRequest(
        patient_id=patient.id,
        doctor_id=doc1.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        notes="Full chart test",
    )
    actor = {"id": str(admin.id), "name": admin.name, "role": "hospital_admin"}
    detail = AdmitPatientAction(db_session).execute(hospital.id, admit_req, actor)

    # Create medication order
    med_req = IpdMedicationOrderCreate(
        medicine_name="Paracetamol",
        dose="650mg",
        route="oral",
        frequency="BD",
        duration_days=1,
    )
    CreateMedicationOrderAction(db_session, hospital.id).execute(detail.id, med_req, actor)

    # Query chart service
    chart = AdmissionChartService(db_session, hospital.id).get(detail.id)

    assert chart.admission.id == detail.id
    assert len(chart.care_team) >= 1
    assert chart.care_team[0]["doctor_id"] == str(doc1.id)
    assert len(chart.medication_orders) == 1
    assert chart.medication_orders[0]["medicine_name"] == "Paracetamol"
    assert chart.financial_clearance is not None
    assert "net_patient_balance" in chart.financial_clearance
    assert "is_cleared" in chart.financial_clearance
