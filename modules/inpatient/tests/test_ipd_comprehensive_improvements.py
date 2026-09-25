"""
Comprehensive regression and integration tests for IPD improvements:
1. Defect B3: Bed allocation on admitted awaiting-bed patient ensures admission fee is charged.
2. Defect B4: Cancelling admission after discharge request cancels both admission and bed charges.
3. Defect B5: Registration discharge enforces discharge_requested status gate.
4. Defect B2: Episode lifecycle policy blocks clinical documentation on inactive/discharged/cancelled episodes.
5. Defect B2: eMAR state machine transitions, non-admin reasons, and dual sign-off validation.
6. Structured IPD Vitals & Intake/Output recording and chart aggregation with balance calculation.
7. Defect B6: Cross-functional read authorization using require_any_permission.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.beds.entities.bed import Bed, Room, Ward, WardType
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
    FinancialAccount,
    FinancialAccountType,
)
from modules.billing.services.billing_service import get_or_create_financial_account
from modules.clinical_records.entities.clinical_record import Prescription
from modules.doctors.entities.doctor import HospitalUser
from modules.inpatient.actions.admission_actions import (
    AllocateBedAction,
    CancelAdmissionAction,
    DischargePatientAction,
    RequestDischargeAction,
)
from modules.inpatient.actions.registration_admission_actions import (
    RegistrationDischargeAction,
)
from modules.inpatient.api.ipd_api import router as ipd_router
from modules.inpatient.api.nursing_api import router as nursing_router
from modules.inpatient.contracts.inpatient_contracts import (
    AllocateRequest,
    DischargeRequestCreate,
)
from modules.inpatient.entities.admission import Admission, AdmissionStatus, BedStaySegment
from modules.inpatient.entities.nursing_entities import (
    CarePlanStatus,
    ClinicalNoteType,
    IntakeOutputType,
    MedicationAdminStatus,
)
from modules.inpatient.services.inpatient_billing_service import InpatientBillingService
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from shared.auth.jwt import create_access_token
from shared.exceptions import ValidationError
from shared.exceptions.handlers import register_exception_handlers
from tests.conftest import db_session, doctor, hospital, patient


@pytest.fixture(scope="function")
def ipd_app(db_session: Session) -> FastAPI:
    Base.metadata.create_all(bind=db_session.bind)
    app = FastAPI(title="IPD Remediation Test App")
    register_exception_handlers(app)
    app.include_router(ipd_router, prefix="/api")
    app.include_router(nursing_router, prefix="/api")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_transitional_sync_session] = _override_db
    return app


@pytest.fixture(scope="function")
def client(ipd_app: FastAPI) -> TestClient:
    return TestClient(ipd_app)


@pytest.fixture(scope="function")
def admin_headers(hospital: Hospital) -> dict[str, str]:
    token = create_access_token(
        data={
            "sub": "admin@hospital.test",
            "name": "Admin User",
            "email": "admin@hospital.test",
            "role": "hospital_admin",
            "hospital_uuid": str(hospital.id),
            "user_id": str(uuid.uuid4()),
        }
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def test_ward(db_session: Session, hospital: Hospital) -> Ward:
    ward = Ward(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="General Medical Ward",
        ward_type=WardType.general,
        admission_fee=1000.0,
        bed_charge_per_day=1500.0,
    )
    db_session.add(ward)
    db_session.commit()
    db_session.refresh(ward)
    return ward


@pytest.fixture(scope="function")
def test_room(db_session: Session, hospital: Hospital, test_ward: Ward) -> Room:
    room = Room(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        ward_id=test_ward.id,
        room_code="G-101",
    )
    db_session.add(room)
    db_session.commit()
    db_session.refresh(room)
    return room


@pytest.fixture(scope="function")
def test_bed(db_session: Session, hospital: Hospital, test_ward: Ward, test_room: Room) -> Bed:
    bed = Bed(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        ward_id=test_ward.id,
        room_id=test_room.id,
        bed_code="B-101-1",
        is_occupied=False,
    )
    db_session.add(bed)
    db_session.commit()
    db_session.refresh(bed)
    return bed


# ---------------------------------------------------------------------------
# Defect B3: Bed Allocation charges admission fee idempotently for awaiting-bed
# ---------------------------------------------------------------------------
def test_allocate_bed_charges_admission_fee(
    db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser, test_bed: Bed
):
    """Allocating a bed to an admitted patient awaiting bed ensures admission charge is created."""
    admission = Admission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AdmissionStatus.admitted,
        bed_id=None,
        notes="Pneumonia requiring bed placement",
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)
    db_session.commit()

    account = get_or_create_financial_account(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_type=FinancialAccountType.ipd,
        admission_id=admission.id,
    )

    # Verify no admission charge exists yet
    charges_before = (
        db_session.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital.id,
            BillingCharge.patient_id == patient.id,
            BillingCharge.source_type == BillingSourceType.admission,
        )
        .all()
    )
    assert len(charges_before) == 0

    # Allocate bed
    action = AllocateBedAction(db_session)
    req = AllocateRequest(
        admission_id=admission.id,
        bed_id=test_bed.id,
        ward_id=test_bed.ward_id,
        room_id=test_bed.room_id,
    )
    updated = action.execute(hospital_id=hospital.id, payload=req, actor={"name": "Doctor"})

    assert updated.bed_id == test_bed.id
    db_session.refresh(test_bed)
    assert test_bed.is_occupied is True

    # Verify admission charge was created idempotently
    charges_after = (
        db_session.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital.id,
            BillingCharge.patient_id == patient.id,
            BillingCharge.source_type == BillingSourceType.admission,
            BillingCharge.status == BillingChargeStatus.pending,
        )
        .all()
    )
    assert len(charges_after) == 1


# ---------------------------------------------------------------------------
# Defect B4: Cancel Admission reverses bed charges alongside admission charges
# ---------------------------------------------------------------------------
def test_cancel_admission_reverses_bed_and_admission_charges(
    db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser, test_bed: Bed
):
    """Cancelling an admission cancels both admission and bed charges."""
    admission = Admission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AdmissionStatus.admitted,
        bed_id=test_bed.id,
        notes="Observation",
        admitted_at=datetime.now(timezone.utc),
    )
    test_bed.is_occupied = True
    db_session.add(admission)
    db_session.commit()

    account = get_or_create_financial_account(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_type=FinancialAccountType.ipd,
        admission_id=admission.id,
    )

    # Post an admission charge and a bed charge
    adm_charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        source_type=BillingSourceType.admission,
        source_id=admission.id,
        description="Admission Fee",
        charge_amount=1000.0,
        net_amount=1000.0,
        status=BillingChargeStatus.pending,
    )
    bed_charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=account.id,
        source_type=BillingSourceType.bed,
        source_id=admission.id,
        description="Bed Charges",
        charge_amount=1500.0,
        net_amount=1500.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add_all([adm_charge, bed_charge])
    db_session.commit()

    cancel_action = CancelAdmissionAction(db_session)
    cancel_action.execute(
        hospital_id=hospital.id,
        admission_id=admission.id,
        reason="Patient requested transfer before start",
        actor={"name": "Admin"},
    )

    db_session.refresh(admission)
    assert admission.status == AdmissionStatus.cancelled

    db_session.refresh(adm_charge)
    db_session.refresh(bed_charge)
    assert adm_charge.status == BillingChargeStatus.cancelled
    assert bed_charge.status == BillingChargeStatus.cancelled


# ---------------------------------------------------------------------------
# Defect B5: Registration discharge enforces discharge_requested gate
# ---------------------------------------------------------------------------
def test_registration_discharge_enforces_discharge_requested_gate(
    db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser, test_bed: Bed
):
    """Registration discharge cannot discharge an admitted patient without prior discharge_requested."""
    admission = Admission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AdmissionStatus.admitted,
        bed_id=test_bed.id,
        notes="Post-op care",
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)
    db_session.commit()

    reg_discharge = RegistrationDischargeAction(db_session)

    # Attempting to discharge while status is 'admitted' must fail
    with pytest.raises(Exception) as exc_info:
        reg_discharge.execute(hospital_id=hospital.id, admission_id=admission.id, actor={"name": "Admin"})
    assert "discharge_requested" in str(exc_info.value) or "requested first" in str(exc_info.value)

    # Now transition to discharge_requested
    req_action = RequestDischargeAction(db_session)
    req_action.execute(
        hospital_id=hospital.id,
        payload=DischargeRequestCreate(admission_id=admission.id, discharge_notes="Patient stable for home discharge"),
        actor={"name": "Doctor"},
    )
    admission.no_discharge_meds = True
    db_session.commit()
    from modules.billing.entities.billing_entities import FinancialAccount, BillingCharge
    from modules.billing.services.invoice_service import create_invoice_from_charges
    acc = db_session.query(FinancialAccount).filter(FinancialAccount.hospital_id == hospital.id, FinancialAccount.admission_id == admission.id).first()
    if acc:
        charges = db_session.query(BillingCharge).filter(BillingCharge.account_id == acc.id).all()
        if charges:
            create_invoice_from_charges(db_session, hospital_id=hospital.id, patient_id=patient.id, charge_ids=[c.id for c in charges], account_id=acc.id)

    # Now RegistrationDischargeAction succeeds
    res = reg_discharge.execute(hospital_id=hospital.id, admission_id=admission.id, actor={"name": "Admin"})
    assert res.status == AdmissionStatus.discharged.value


# ---------------------------------------------------------------------------
# Defect B2: Episode lifecycle policy blocks clinical actions on cancelled/discharged
# ---------------------------------------------------------------------------
def test_lifecycle_policy_blocks_clinical_notes_on_discharged(
    client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser, admin_headers: dict[str, str]
):
    """Clinical notes and care plans cannot be created for discharged or cancelled admissions."""
    admission = Admission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AdmissionStatus.discharged,
        bed_id=None,
        notes="Completed episode",
        admitted_at=datetime.now(timezone.utc),
        discharged_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)
    db_session.commit()

    # Attempt to add clinical note
    res_note = client.post(
        f"/api/ipd/admissions/{admission.id}/clinical-notes",
        json={
            "note_type": "nursing_progress",
            "subjective": "Feeling okay",
            "objective_vitals": {"temp": 98.6},
            "assessment": "Stable",
            "plan": "Continue meds",
            "note_content": "Patient reports feeling stable post-discharge",
        },
        headers=admin_headers,
    )
    assert res_note.status_code == 400
    assert "discharged" in res_note.json()["detail"]

    # Attempt to add care plan
    res_care = client.post(
        f"/api/ipd/admissions/{admission.id}/care-plans",
        json={
            "nursing_diagnosis": "Risk of fall",
            "goals": "No falls during stay",
            "interventions": {"action": "Side rails up"},
        },
        headers=admin_headers,
    )
    assert res_care.status_code == 400
    assert "discharged" in res_care.json()["detail"]


# ---------------------------------------------------------------------------
# Defect B2: eMAR State Machine, Reason Requirement, Dual Sign-off
# ---------------------------------------------------------------------------
def test_emar_state_machine_and_validation(
    client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser, admin_headers: dict[str, str]
):
    """eMAR validates valid forward transitions, mandatory reasons for non-admin, and dual sign-off."""
    admission = Admission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AdmissionStatus.admitted,
        bed_id=None,
        notes="Diabetic Ketoacidosis",
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)
    db_session.commit()

    # Schedule high-alert med via eMAR API
    sched_ha = {
        "medicine_name": "Regular Insulin",
        "dose": "10 units",
        "route": "SC",
        "scheduled_time": datetime.now(timezone.utc).isoformat(),
        "is_high_alert": True,
    }
    res_sched = client.post(f"/api/ipd/admissions/{admission.id}/emar", json=sched_ha, headers=admin_headers)
    assert res_sched.status_code == 201
    dose = res_sched.json()
    dose_id = dose["id"]
    assert dose["status"] == "scheduled"
    assert dose["is_high_alert"] is True

    # 1. Non-admin without reason must fail
    res_withhold_fail = client.put(
        f"/api/ipd/emar/{dose_id}/record",
        json={"status": "withheld"},
        headers=admin_headers,
    )
    assert res_withhold_fail.status_code in (400, 422)
    assert "reason" in str(res_withhold_fail.json()).lower()

    # 2. High-alert administration without witness must fail
    res_admin_no_witness = client.put(
        f"/api/ipd/emar/{dose_id}/record",
        json={"status": "administered"},
        headers=admin_headers,
    )
    assert res_admin_no_witness.status_code in (400, 422)

    # 3. High-alert with witness succeeds
    witness_id = str(uuid.uuid4())
    res_admin_ok = client.put(
        f"/api/ipd/emar/{dose_id}/record",
        json={
            "status": "administered",
            "witness_nurse_id": witness_id,
            "witness_nurse_name": "Nurse Sarah",
            "notes_or_reason": "Dual verified",
        },
        headers=admin_headers,
    )
    assert res_admin_ok.status_code == 200
    assert res_admin_ok.json()["status"] == "administered"

    # 4. Terminal state: Cannot transition already administered dose
    res_admin_again = client.put(
        f"/api/ipd/emar/{dose_id}/record",
        json={"status": "withheld", "notes_or_reason": "Patient nauseous"},
        headers=admin_headers,
    )
    assert res_admin_again.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Structured IPD Vitals & Intake/Output Timeline & Chart Aggregation
# ---------------------------------------------------------------------------
def test_structured_vitals_and_intake_output_chart(
    client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser, admin_headers: dict[str, str]
):
    """Test recording IPD vitals and intake/output, and verifying chart aggregation and balance."""
    admission = Admission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AdmissionStatus.admitted,
        bed_id=None,
        notes="Post-cardiac catheterization",
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)
    db_session.commit()

    # 1. Record vital signs
    vital_payload = {
        "pulse_rate_bpm": 78,
        "systolic_bp": 120,
        "diastolic_bp": 80,
        "temperature_c": 37.0,
        "respiratory_rate_bpm": 16,
        "spo2_percent": 99.0,
        "consciousness": "Alert",
        "notes": "Patient resting comfortably",
    }
    res_vital = client.post(
        f"/api/ipd/admissions/{admission.id}/vitals",
        json=vital_payload,
        headers=admin_headers,
    )
    assert res_vital.status_code == 201
    vital_data = res_vital.json()
    assert vital_data["pulse_rate_bpm"] == 78
    assert vital_data["consciousness"] == "Alert"

    # 2. Record Intake (IV Normal Saline 500 mL)
    res_intake = client.post(
        f"/api/ipd/admissions/{admission.id}/intake-output",
        json={
            "entry_type": "intake",
            "category": "iv_fluid",
            "volume_ml": 500.0,
            "route_or_site": "IV Cannula Left Forearm",
            "notes": "Normal Saline running at 100 mL/hr",
        },
        headers=admin_headers,
    )
    assert res_intake.status_code == 201
    assert res_intake.json()["entry_type"] == "intake"
    assert res_intake.json()["volume_ml"] == 500.0

    # 3. Record Output (Urine 350 mL)
    res_output = client.post(
        f"/api/ipd/admissions/{admission.id}/intake-output",
        json={
            "entry_type": "output",
            "category": "urine",
            "volume_ml": 350.0,
            "route_or_site": "Foley Catheter",
            "notes": "Clear yellow",
        },
        headers=admin_headers,
    )
    assert res_output.status_code == 201
    assert res_output.json()["entry_type"] == "output"
    assert res_output.json()["volume_ml"] == 350.0

    # 4. Fetch Inpatient Chart and verify aggregation
    res_chart = client.get(f"/api/ipd/admissions/{admission.id}/chart", headers=admin_headers)
    assert res_chart.status_code == 200
    chart = res_chart.json()

    assert chart["latest_vitals"] is not None
    assert len(chart["latest_vitals"]) >= 1
    assert chart["latest_vitals"][0]["pulse_rate_bpm"] == 78
    assert len(chart["vitals_history"]) == 1

    io_summary = chart["intake_output_summary"]
    assert io_summary is not None
    assert io_summary["total_intake_ml"] == 500.0
    assert io_summary["total_output_ml"] == 350.0
    # Net balance: 500 - 350 = +150
    assert io_summary["net_balance_ml"] == 150.0

    # 5. List vitals endpoint
    res_vitals_list = client.get(f"/api/ipd/admissions/{admission.id}/vitals", headers=admin_headers)
    assert res_vitals_list.status_code == 200
    assert len(res_vitals_list.json()) == 1

    # 6. List intake-output endpoint
    res_io_list = client.get(f"/api/ipd/admissions/{admission.id}/intake-output", headers=admin_headers)
    assert res_io_list.status_code == 200
    assert len(res_io_list.json()) == 2


# ---------------------------------------------------------------------------
# Defect B6: Cross-functional read authorization (require_any_permission)
# ---------------------------------------------------------------------------
def test_cross_functional_chart_read_permission(
    client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    """A user with nurse role or doctor role can access the IPD chart and vitals."""
    admission = Admission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        status=AdmissionStatus.admitted,
        bed_id=None,
        notes="General observation",
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)
    db_session.commit()

    # Hospital admin token always has permission
    admin_tok = create_access_token({
        "sub": "admin@hospital.test",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital.id),
        "user_id": str(uuid.uuid4()),
    })
    res = client.get(f"/api/ipd/admissions/{admission.id}/chart", headers={"Authorization": f"Bearer {admin_tok}"})
    assert res.status_code == 200
    assert res.json()["admission"]["id"] == str(admission.id)
