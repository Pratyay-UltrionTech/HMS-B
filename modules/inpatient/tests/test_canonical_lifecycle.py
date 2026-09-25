"""
Canonical IPD lifecycle acceptance tests (spec §29, Tests 1-14 + Invariants).

Covers the unified REQUESTED → ADMITTED → DISCHARGE_REQUESTED → DISCHARGED
lifecycle: Doctor finalize creates/links the canonical request, Transfer
converges on the same row, acceptance works with/without a bed, transfer and
discharge preserve the invariants, and concurrency losers get controlled
conflicts instead of duplicate episodes or unhandled 500s.
"""

from datetime import date, time
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from modules.appointments.entities.appointment import Appointment, AppointmentStatus
from modules.beds.entities.bed import Bed, Room, Ward, WardType
from modules.doctors.actions.doctor_appointment_actions import TransferAppointmentToInpatientAction
from modules.doctors.contracts.doctor_contracts import TransferToInpatientRequest
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.inpatient.actions.admission_actions import (
    AllocateBedAction,
    DischargePatientAction,
    RequestDischargeAction,
    TransferBedAction,
    to_admission_detail,
)
from modules.inpatient.actions.admission_lifecycle_actions import (
    AcceptAdmissionAction,
    EnsureAdmissionRequestAction,
)
from modules.inpatient.actions.ipd_actions import CreateFormSubmissionAction
from modules.inpatient.contracts.inpatient_contracts import (
    AllocateRequest,
    DischargeRequest,
    DischargeRequestCreate,
    IpdFormSubmissionCreate,
    TransferRequest,
)
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.entities.admission import (
    AdmissionStatus,
    BedStaySegment,
    IpdFormSubmissionStatus,
)
from modules.patients.entities.patient import Patient, PatientStatus
from modules.tenancy.entities.hospital import Hospital
from shared.exceptions.base import ConflictError, ValidationError
from tests.conftest import db_session, hospital  # noqa: F401  (fixtures)


def _make_staff(db_session: Session, hospital: Hospital, name="Dr. Who", email=None):
    role = StaffRole(id=uuid4(), hospital_id=hospital.id, name="Doctor")
    db_session.add(role)
    db_session.commit()
    doc = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name=name,
        email=email or f"{uuid4().hex[:8]}@hospital.com",
        phone="9876500099",
        password_hash="pwd",
    )
    db_session.add(doc)
    db_session.commit()
    return doc


def _make_ward(db_session: Session, hospital: Hospital, beds=2):
    ward = Ward(
        id=uuid4(), hospital_id=hospital.id, name=f"W-{uuid4().hex[:6]}",
        ward_type=WardType.general, admission_fee=500.0,
        bed_charge_per_day=1000.0, is_active=True,
    )
    db_session.add(ward)
    db_session.commit()
    room = Room(
        id=uuid4(), hospital_id=hospital.id, ward_id=ward.id,
        room_code=f"R-{uuid4().hex[:6]}", bed_count=beds, is_active=True,
    )
    db_session.add(room)
    db_session.commit()
    out = [ward, room]
    for i in range(beds):
        b = Bed(
            id=uuid4(), hospital_id=hospital.id, ward_id=ward.id,
            room_id=room.id, bed_code=f"B-{uuid4().hex[:6]}",
            is_occupied=False, is_active=True,
        )
        db_session.add(b)
        out.append(b)
    db_session.commit()
    return out


def _make_patient(db_session: Session, hospital: Hospital, uhid=None):
    p = Patient(
        id=uuid4(), hospital_id=hospital.id, uhid=uhid or f"U{uuid4().hex[:8]}",
        first_name="Test", last_name="Patient", name="Test Patient",
        mobile=f"9{uuid4().int % 10**9:09d}",
    )
    db_session.add(p)
    db_session.commit()
    return p


def _make_appt(db_session: Session, hospital: Hospital, doc, patient, status=AppointmentStatus.scheduled):
    appt = Appointment(
        id=uuid4(), hospital_id=hospital.id, doctor_id=doc.id,
        patient_id=patient.id, appointment_date=date.today(),
        appointment_time=time(10, 0), status=status, purpose="Consult",
    )
    db_session.add(appt)
    db_session.commit()
    return appt


ACTOR = {"name": "Tester", "role": "doctor"}


def _finalize(db_session, hospital, patient, admission_id=None):
    return CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission_id,
            form_id="admission-advice",
            form_title="Admission Advice",
            form_data={"diagnosis": "test"},
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )


# ── Test 1: draft creates nothing ────────────────────────────────────────────
def test_1_draft_creates_no_admission(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id, form_id="admission-advice",
            form_title="Admission Advice", form_data={},
            status=IpdFormSubmissionStatus.draft,
        ),
        ACTOR,
    )
    repo = AdmissionsRepository(db_session)
    assert repo.get_open_admission(hospital.id, patient.id) is None
    assert db_session.query(Patient).filter(Patient.id == patient.id).first().status == PatientStatus.active


# ── Test 2: finalize creates REQUESTED + links form + nurse sees it ─────────
def test_2_finalize_creates_request(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    sub = _finalize(db_session, hospital, patient)
    assert sub.admission_id is not None
    repo = AdmissionsRepository(db_session)
    adm = repo.get_admission_by_id(hospital.id, sub.admission_id)
    assert adm.status == AdmissionStatus.requested
    assert adm.bed_id is None
    assert adm.patient_id == patient.id
    # Nurse request queue sees it; active census does not.
    assert any(r.id == adm.id for r in repo.list_admission_requests(hospital.id))
    assert all(r.id != adm.id for r in repo.list_active_admissions(hospital.id))
    # Patient mirror untouched while merely requested.
    assert db_session.query(Patient).filter(Patient.id == patient.id).first().status == PatientStatus.active


# ── Test 3: finalize twice → exactly one canonical admission ────────────────
def test_3_finalize_idempotent(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    first = _finalize(db_session, hospital, patient)
    second = _finalize(db_session, hospital, patient)
    assert first.admission_id == second.admission_id
    repo = AdmissionsRepository(db_session)
    rows = (
        db_session.query(repo.get_open_admission(hospital.id, patient.id).__class__)
        .filter_by(hospital_id=hospital.id, patient_id=patient.id)
        .all()
    )
    assert len(rows) == 1


# ── Test 4: transfer first, then finalize → same admission ──────────────────
def test_4_transfer_then_finalize_converge(db_session: Session, hospital: Hospital):
    doc = _make_staff(db_session, hospital)
    patient = _make_patient(db_session, hospital)
    appt = _make_appt(db_session, hospital, doc, patient)
    TransferAppointmentToInpatientAction(db_session).execute(
        hospital.id, doc.id, appt.id, TransferToInpatientRequest(notes="admit"), ACTOR
    )
    sub = _finalize(db_session, hospital, patient)
    repo = AdmissionsRepository(db_session)
    adm = repo.get_open_admission(hospital.id, patient.id)
    assert adm is not None and adm.status == AdmissionStatus.requested
    assert sub.admission_id == adm.id
    assert adm.source_appointment_id == appt.id


# ── Test 5: finalize first, then transfer → same admission ──────────────────
def test_5_finalize_then_transfer_converge(db_session: Session, hospital: Hospital):
    doc = _make_staff(db_session, hospital)
    patient = _make_patient(db_session, hospital)
    sub = _finalize(db_session, hospital, patient)
    appt = _make_appt(db_session, hospital, doc, patient)
    TransferAppointmentToInpatientAction(db_session).execute(
        hospital.id, doc.id, appt.id, TransferToInpatientRequest(), ACTOR
    )
    repo = AdmissionsRepository(db_session)
    adm = repo.get_open_admission(hospital.id, patient.id)
    assert sub.admission_id == adm.id
    refreshed = db_session.query(Appointment).filter(Appointment.id == appt.id).first()
    assert refreshed.status == AppointmentStatus.ipd_transfer_requested
    assert refreshed.admission_id == adm.id


# ── Test 6: accept with bed ─────────────────────────────────────────────────
def test_6_accept_with_bed(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    sub = _finalize(db_session, hospital, patient)
    ward, room, bed = _make_ward(db_session, hospital, beds=1)
    detail = AcceptAdmissionAction(db_session).execute(
        hospital.id, ACTOR, admission_id=sub.admission_id,
        ward_id=ward.id, room_id=room.id, bed_id=bed.id,
    )
    assert detail.status == AdmissionStatus.admitted
    assert detail.bed_id == bed.id and not detail.is_awaiting_bed
    assert db_session.query(Bed).filter(Bed.id == bed.id).first().is_occupied is True
    assert db_session.query(Patient).filter(Patient.id == patient.id).first().status == PatientStatus.admitted
    segs = db_session.query(BedStaySegment).filter(BedStaySegment.admission_id == detail.id).all()
    assert len(segs) == 1 and segs[0].ended_at is None and segs[0].bed_id == bed.id
    assert detail.ip_id and detail.ip_id.startswith("IP-")
    # Now visible in active census.
    repo = AdmissionsRepository(db_session)
    assert any(r.id == detail.id for r in repo.list_active_admissions(hospital.id))


# ── Test 7: accept without bed → awaiting bed, still visible ────────────────
def test_7_accept_without_bed(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    sub = _finalize(db_session, hospital, patient)
    detail = AcceptAdmissionAction(db_session).execute(
        hospital.id, ACTOR, admission_id=sub.admission_id
    )
    assert detail.status == AdmissionStatus.admitted
    assert detail.bed_id is None and detail.is_awaiting_bed
    repo = AdmissionsRepository(db_session)
    assert any(r.id == detail.id for r in repo.list_active_admissions(hospital.id))
    assert db_session.query(Patient).filter(Patient.id == patient.id).first().status == PatientStatus.admitted


# ── Test 8: allocate later to awaiting-bed admission ────────────────────────
def test_8_allocate_to_awaiting_bed(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    sub = _finalize(db_session, hospital, patient)
    detail = AcceptAdmissionAction(db_session).execute(
        hospital.id, ACTOR, admission_id=sub.admission_id
    )
    ward, room, bed = _make_ward(db_session, hospital, beds=1)
    out = AllocateBedAction(db_session).execute(
        hospital.id,
        AllocateRequest(admission_id=detail.id, ward_id=ward.id, room_id=room.id, bed_id=bed.id),
        ACTOR,
    )
    assert out.bed_id == bed.id and not out.is_awaiting_bed
    assert db_session.query(Bed).filter(Bed.id == bed.id).first().is_occupied is True
    segs = db_session.query(BedStaySegment).filter(BedStaySegment.admission_id == detail.id).all()
    assert len(segs) == 1 and segs[0].ended_at is None


# ── Test 9: bed transfer ────────────────────────────────────────────────────
def test_9_bed_transfer(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    sub = _finalize(db_session, hospital, patient)
    ward, room, bed_a, bed_b = _make_ward(db_session, hospital, beds=2)
    detail = AcceptAdmissionAction(db_session).execute(
        hospital.id, ACTOR, admission_id=sub.admission_id,
        ward_id=ward.id, room_id=room.id, bed_id=bed_a.id,
    )
    out = TransferBedAction(db_session).execute(
        hospital.id,
        TransferRequest(admission_id=detail.id, to_ward_id=ward.id, to_room_id=room.id, to_bed_id=bed_b.id),
        ACTOR,
    )
    assert out.bed_id == bed_b.id and out.status == AdmissionStatus.admitted
    assert db_session.query(Bed).filter(Bed.id == bed_a.id).first().is_occupied is False
    assert db_session.query(Bed).filter(Bed.id == bed_b.id).first().is_occupied is True
    segs = (
        db_session.query(BedStaySegment)
        .filter(BedStaySegment.admission_id == detail.id)
        .order_by(BedStaySegment.started_at.asc())
        .all()
    )
    assert len(segs) == 2
    assert segs[0].ended_at is not None and segs[1].ended_at is None and segs[1].bed_id == bed_b.id


# ── Test 10: discharge request stays in census ──────────────────────────────
def test_10_discharge_request_stays_in_census(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    sub = _finalize(db_session, hospital, patient)
    ward, room, bed = _make_ward(db_session, hospital, beds=1)
    detail = AcceptAdmissionAction(db_session).execute(
        hospital.id, ACTOR, admission_id=sub.admission_id,
        ward_id=ward.id, room_id=room.id, bed_id=bed.id,
    )
    out = RequestDischargeAction(db_session).execute(
        hospital.id, DischargeRequestCreate(admission_id=detail.id), ACTOR
    )
    assert out.status == AdmissionStatus.discharge_requested
    repo = AdmissionsRepository(db_session)
    assert any(r.id == detail.id for r in repo.list_active_admissions(hospital.id))
    assert db_session.query(Bed).filter(Bed.id == bed.id).first().is_occupied is True


# ── Test 11: discharge frees bed, closes segment, clears census ─────────────
def test_11_discharge(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    sub = _finalize(db_session, hospital, patient)
    ward, room, bed = _make_ward(db_session, hospital, beds=1)
    detail = AcceptAdmissionAction(db_session).execute(
        hospital.id, ACTOR, admission_id=sub.admission_id,
        ward_id=ward.id, room_id=room.id, bed_id=bed.id,
    )
    RequestDischargeAction(db_session).execute(
        hospital.id, DischargeRequestCreate(admission_id=detail.id), ACTOR
    )
    from modules.billing.entities.billing_entities import BillingCharge, BillingPayment, FinancialAccount
    from modules.billing.services.invoice_service import create_invoice_from_charges
    from modules.inpatient.services.inpatient_billing_service import InpatientBillingService
    acc = (
        db_session.query(FinancialAccount)
        .filter(
            FinancialAccount.hospital_id == hospital.id,
            FinancialAccount.admission_id == detail.id,
        )
        .first()
    )
    charges = db_session.query(BillingCharge).filter(BillingCharge.account_id == acc.id).all()
    if charges:
        create_invoice_from_charges(
            db_session,
            hospital_id=hospital.id,
            patient_id=patient.id,
            charge_ids=[c.id for c in charges],
            account_id=acc.id,
        )
    outstanding = float(
        InpatientBillingService(db_session).get_ledger_totals(hospital.id, patient.id, admission_id=detail.id).get("outstanding") or 0
    )
    assert outstanding > 0  # gate is real: charges exist
    db_session.add(
        BillingPayment(
            id=uuid4(), hospital_id=hospital.id, patient_id=patient.id,
            account_id=acc.id if acc else None,
            amount=outstanding, payment_date=date.today(),
        )
    )
    db_session.commit()
    out = DischargePatientAction(db_session).execute(
        hospital.id, DischargeRequest(admission_id=detail.id, no_discharge_meds=True), ACTOR
    )
    assert out.status == AdmissionStatus.discharged
    assert out.discharged_at is not None  # discharged ⇔ discharged_at invariant
    assert db_session.query(Bed).filter(Bed.id == bed.id).first().is_occupied is False
    assert db_session.query(Patient).filter(Patient.id == patient.id).first().status == PatientStatus.active
    repo = AdmissionsRepository(db_session)
    assert all(r.id != detail.id for r in repo.list_active_admissions(hospital.id))
    open_segs = db_session.query(BedStaySegment).filter(
        BedStaySegment.admission_id == detail.id, BedStaySegment.ended_at.is_(None)
    ).all()
    assert open_segs == []


# ── Test 12: concurrent admission attempts → single episode ─────────────────
def test_12_duplicate_request_blocked(db_session: Session, hospital: Hospital):
    patient = _make_patient(db_session, hospital)
    first, created = EnsureAdmissionRequestAction(db_session).execute(
        hospital.id, patient.id, ACTOR
    )
    assert created is True
    second, created_again = EnsureAdmissionRequestAction(db_session).execute(
        hospital.id, patient.id, ACTOR
    )
    assert created_again is False and second.id == first.id


# ── Test 13: bed double-booking controlled ──────────────────────────────────
def test_13_bed_double_booking_conflict(db_session: Session, hospital: Hospital):
    p1 = _make_patient(db_session, hospital)
    p2 = _make_patient(db_session, hospital)
    s1 = _finalize(db_session, hospital, p1)
    s2 = _finalize(db_session, hospital, p2)
    ward, room, bed = _make_ward(db_session, hospital, beds=1)
    AcceptAdmissionAction(db_session).execute(
        hospital.id, ACTOR, admission_id=s1.admission_id,
        ward_id=ward.id, room_id=room.id, bed_id=bed.id,
    )
    with pytest.raises(Exception) as exc_info:
        AcceptAdmissionAction(db_session).execute(
            hospital.id, ACTOR, admission_id=s2.admission_id,
            ward_id=ward.id, room_id=room.id, bed_id=bed.id,
        )
    assert "occupied" in str(exc_info.value).lower()
    # Loser still has exactly one open episode and no bed.
    repo = AdmissionsRepository(db_session)
    loser = repo.get_open_admission(hospital.id, p2.id)
    assert loser.status == AdmissionStatus.requested and loser.bed_id is None


# ── Test 14: cross-role convergence on the same row ─────────────────────────
def test_14_cross_role_same_state(db_session: Session, hospital: Hospital):
    doc = _make_staff(db_session, hospital)
    patient = _make_patient(db_session, hospital)
    appt = _make_appt(db_session, hospital, doc, patient)
    TransferAppointmentToInpatientAction(db_session).execute(
        hospital.id, doc.id, appt.id, TransferToInpatientRequest(), ACTOR
    )
    repo = AdmissionsRepository(db_session)
    req = repo.get_open_admission(hospital.id, patient.id)
    # Doctor view (scoped census helper uses same predicate), nurse queue,
    # and active census derive from the same tables.
    assert any(r.id == req.id for r in repo.list_admission_requests(hospital.id))
    ward, room, bed = _make_ward(db_session, hospital, beds=1)
    detail = AcceptAdmissionAction(db_session).execute(
        hospital.id, ACTOR, admission_id=req.id,
        ward_id=ward.id, room_id=room.id, bed_id=bed.id,
    )
    assert any(r.id == detail.id for r in repo.list_active_admissions(hospital.id, doctor_id=doc.id))
    hydrated_appt = db_session.query(Appointment).filter(Appointment.id == appt.id).first()
    assert hydrated_appt.status == AppointmentStatus.transferred_to_inpatient
    assert hydrated_appt.admission_id == detail.id
    assert to_admission_detail(
        repo.get_admission_by_id(hospital.id, detail.id)
    ).bed_id == bed.id


# ── Test 16: HTTP wiring for request queue + accept ─────────────────────────
def test_16_request_queue_and_accept_endpoints(db_session: Session, hospital: Hospital):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infrastructure.postgres.session import get_transitional_sync_session
    from modules.beds.api.beds_api import router as beds_router
    from shared.auth.jwt import create_access_token
    from shared.exceptions.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(beds_router, prefix="/api")

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_transitional_sync_session] = _override_get_db
    client = TestClient(app, raise_server_exceptions=False)
    token = create_access_token({
        "sub": "admin@hospital.com", "name": "Admin",
        "role": "hospital_admin", "hospital_uuid": str(hospital.id),
    })
    headers = {"Authorization": f"Bearer {token}"}

    patient = _make_patient(db_session, hospital)
    _finalize(db_session, hospital, patient)

    q = client.get("/api/beds/admission-requests", headers=headers)
    assert q.status_code == 200, q.text
    assert len(q.json()) == 1 and q.json()[0]["status"] == "requested"

    admission_id = q.json()[0]["id"]
    # Accept without bed → awaiting bed, still in census.
    a = client.post(f"/api/beds/admissions/{admission_id}/accept", json={}, headers=headers)
    assert a.status_code == 200, a.text
    assert a.json()["status"] == "admitted"
    assert a.json()["is_awaiting_bed"] is True

    q2 = client.get("/api/beds/admission-requests", headers=headers)
    assert q2.status_code == 200 and q2.json() == []
    census = client.get("/api/beds/admissions/active", headers=headers)
    assert census.status_code == 200
    assert any(r["id"] == admission_id for r in census.json())
# ── Test 15: legacy unlinked draft cannot acquire an admission on update ─────
def test_15_update_finalize_unlinked_draft_is_rejected(db_session: Session, hospital: Hospital):
    from modules.inpatient.actions.ipd_actions import UpdateFormSubmissionAction
    from modules.inpatient.contracts.inpatient_contracts import IpdFormSubmissionUpdate

    patient = _make_patient(db_session, hospital)
    draft = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id, form_id="admission-advice",
            form_title="Admission Advice", form_data={},
            status=IpdFormSubmissionStatus.draft,
        ),
        ACTOR,
    )
    assert draft.admission_id is None
    repo = AdmissionsRepository(db_session)
    assert repo.get_open_admission(hospital.id, patient.id) is None

    with pytest.raises(ValidationError, match="no linked admission"):
        UpdateFormSubmissionAction(db_session).execute(
            hospital.id, draft.id,
            IpdFormSubmissionUpdate(status=IpdFormSubmissionStatus.final),
            ACTOR,
        )
    db_session.rollback()
    assert repo.get_open_admission(hospital.id, patient.id) is None
