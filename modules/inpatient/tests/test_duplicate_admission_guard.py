"""Duplicate-admission guard tests (FIX LIST verification).

Covers: second-doctor 409, bedded re-allocate blocked, bedless allocate
allowed, requested reuse, discharge_requested blocked, discharged allows new,
concurrent double-request, same-bed double allocate, cross-doctor visibility,
Emergency converge/409, OT post-op accept-not-duplicate.
Uses standalone sqlite pattern (no shared conftest import).
"""

import uuid
from datetime import date, time
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base

# Import every entity module so Base.metadata covers all tables before create_all.
import modules.tenancy.entities.hospital  # noqa: F401
import modules.doctors.entities.doctor  # noqa: F401
import modules.patients.entities.patient  # noqa: F401
import modules.beds.entities.bed  # noqa: F401
import modules.inpatient.entities.admission  # noqa: F401
import modules.appointments.entities.appointment  # noqa: F401
import modules.emergency.entities.emergency_entities  # noqa: F401
import modules.ot.entities.ot_entities  # noqa: F401
import modules.billing.entities.billing_entities  # noqa: F401
import modules.critical_care.entities.critical_care_entities  # noqa: F401
import shared.audit.entities.audit_log  # noqa: F401
import shared.database.sequences  # noqa: F401

# tests/conftest.py builds its shared sqlite schema at IMPORT time from
# Base.metadata, while its teardown deletes from EVERY registered table.
# The entity modules imported above (emergency/ot/critical-care/sequences)
# extend that global metadata, so when this file is collected in the same
# pytest process as tests using the shared fixture, backfill the extra
# tables onto the shared engine (checkfirst — no-op when run standalone).
try:  # pragma: no cover - test-harness compatibility shim
    from tests.conftest import test_engine as _shared_test_engine

    Base.metadata.create_all(bind=_shared_test_engine, checkfirst=True)
except Exception:
    pass


@pytest.fixture
def sdb():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    fac = sessionmaker(bind=engine)
    s = fac()
    yield s
    s.close()


def _seed(sdb: Session, beds=2):
    from modules.tenancy.entities.hospital import Hospital
    from modules.doctors.entities.doctor import HospitalUser, StaffRole
    from modules.patients.entities.patient import Patient, PatientStatus
    from modules.beds.entities.bed import Bed, Room, Ward, WardType

    hid = uuid4()
    hosp = Hospital(
        id=hid, hospital_id=f"H-{uuid4().hex[:6]}", name="T",
        address="a", phone="1", email=f"{uuid4().hex[:6]}@h.org",
        password_hash="h",
    )
    sdb.add(hosp)
    sdb.flush()
    role = StaffRole(id=uuid4(), hospital_id=hid, name="Doctor")
    sdb.add(role)
    sdb.flush()
    doc_a = HospitalUser(
        id=uuid4(), hospital_id=hid, role_id=role.id, name="Doc A",
        email=f"{uuid4().hex[:6]}@h.org", phone="111", password_hash="h",
        is_active=True,
    )
    doc_b = HospitalUser(
        id=uuid4(), hospital_id=hid, role_id=role.id, name="Doc B",
        email=f"{uuid4().hex[:6]}@h.org", phone="222", password_hash="h",
        is_active=True,
    )
    sdb.add_all([doc_a, doc_b])
    pat = Patient(
        id=uuid4(), hospital_id=hid, uhid=f"U{uuid4().hex[:8]}",
        first_name="T", last_name="P", name="T P",
        mobile=f"9{uuid4().int % 10**9:09d}", status=PatientStatus.active,
    )
    sdb.add(pat)
    ward = Ward(
        id=uuid4(), hospital_id=hid, name=f"W-{uuid4().hex[:6]}",
        ward_type=WardType.general, admission_fee=100.0,
        bed_charge_per_day=200.0, is_active=True,
    )
    sdb.add(ward)
    sdb.flush()
    room = Room(
        id=uuid4(), hospital_id=hid, ward_id=ward.id,
        room_code=f"R-{uuid4().hex[:6]}", bed_count=beds, is_active=True,
    )
    sdb.add(room)
    sdb.flush()
    bed_list = []
    for _ in range(beds):
        b = Bed(
            id=uuid4(), hospital_id=hid, ward_id=ward.id, room_id=room.id,
            bed_code=f"B-{uuid4().hex[:6]}", is_occupied=False, is_active=True,
        )
        sdb.add(b)
        bed_list.append(b)
    sdb.commit()
    return hid, doc_a, doc_b, pat, ward, room, bed_list


ACTOR = {"name": "Tester", "role": "doctor"}


def test_second_doctor_admission_attempt_409_no_second_row(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import EnsureAdmissionRequestAction
    from modules.inpatient.actions.admission_actions import AdmitPatientAction
    from modules.inpatient.contracts.inpatient_contracts import AdmitRequest
    from modules.inpatient.entities.admission import Admission

    hid, doc_a, doc_b, pat, ward, room, beds = _seed(sdb)
    EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR, doctor_id=doc_a.id)
    with pytest.raises(HTTPException) as ei:
        # second doctor tries direct admit on same patient -> must be structured 409
        AdmitPatientAction(sdb).execute(
            hid,
            AdmitRequest(patient_id=pat.id, ward_id=ward.id, room_id=room.id,
                         bed_id=beds[0].id, doctor_id=doc_b.id),
            ACTOR,
        )
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, dict) and ei.value.detail.get("code") == "ADMISSION_CONFLICT"
    rows = sdb.query(Admission).filter(
        Admission.hospital_id == hid, Admission.patient_id == pat.id).all()
    assert len(rows) == 1


def test_bedded_reallocate_blocked(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import (
        EnsureAdmissionRequestAction, AcceptAdmissionAction)
    from modules.inpatient.actions.admission_actions import AllocateBedAction
    from modules.inpatient.contracts.inpatient_contracts import AllocateRequest

    hid, doc_a, _, pat, ward, room, beds = _seed(sdb)
    adm, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    det = AcceptAdmissionAction(sdb).execute(
        hid, ACTOR, admission_id=adm.id,
        ward_id=ward.id, room_id=room.id, bed_id=beds[0].id)
    with pytest.raises(HTTPException) as ei:
        AllocateBedAction(sdb).execute(
            hid,
            AllocateRequest(admission_id=det.id, ward_id=ward.id,
                            room_id=room.id, bed_id=beds[1].id),
            ACTOR)
    assert ei.value.status_code == 409


def test_bedless_allocate_same_admission_allowed(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import (
        EnsureAdmissionRequestAction, AcceptAdmissionAction)
    from modules.inpatient.actions.admission_actions import AllocateBedAction
    from modules.inpatient.contracts.inpatient_contracts import AllocateRequest

    hid, _, _, pat, ward, room, beds = _seed(sdb)
    adm, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    det = AcceptAdmissionAction(sdb).execute(hid, ACTOR, admission_id=adm.id)
    assert det.bed_id is None
    out = AllocateBedAction(sdb).execute(
        hid,
        AllocateRequest(admission_id=det.id, ward_id=ward.id,
                        room_id=room.id, bed_id=beds[0].id),
        ACTOR)
    assert out.bed_id == beds[0].id


def test_requested_reuse_and_discharge_requested_blocked(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import EnsureAdmissionRequestAction
    from modules.inpatient.db.admissions_repository import AdmissionsRepository
    from modules.inpatient.entities.admission import AdmissionStatus

    hid, _, _, pat, _, _, _ = _seed(sdb)
    a1, c1 = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    assert c1 is True
    a2, c2 = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    assert c2 is False and a2.id == a1.id  # requested reuse
    # move to discharge_requested -> Ensure must still return same row (idempotent)
    a1.status = AdmissionStatus.discharge_requested
    sdb.commit()
    a3, c3 = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    assert c3 is False and a3.id == a1.id
    repo = AdmissionsRepository(sdb)
    assert repo.get_open_admission(hid, pat.id).id == a1.id


def test_discharged_allows_new_request(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import EnsureAdmissionRequestAction
    from modules.inpatient.entities.admission import AdmissionStatus

    hid, _, _, pat, _, _, _ = _seed(sdb)
    a1, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    a1.status = AdmissionStatus.discharged
    from datetime import datetime, timezone
    a1.discharged_at = datetime.now(timezone.utc)
    sdb.commit()
    a2, created = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    assert created is True and a2.id != a1.id


def test_concurrent_double_request_one_row(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import EnsureAdmissionRequestAction
    from modules.inpatient.entities.admission import Admission

    hid, _, _, pat, _, _, _ = _seed(sdb)
    r1, c1 = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    r2, c2 = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    assert r1.id == r2.id
    assert (c1, c2) == (True, False)
    rows = sdb.query(Admission).filter(
        Admission.hospital_id == hid, Admission.patient_id == pat.id).all()
    assert len(rows) == 1


def test_same_bed_double_allocate_one_409(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import (
        EnsureAdmissionRequestAction, AcceptAdmissionAction)
    from modules.inpatient.actions.admission_actions import AllocateBedAction
    from modules.inpatient.contracts.inpatient_contracts import AllocateRequest

    hid, _, _, pat, ward, room, beds = _seed(sdb)
    # patient 1 awaiting bed gets the bed
    adm, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    det = AcceptAdmissionAction(sdb).execute(hid, ACTOR, admission_id=adm.id)
    out = AllocateBedAction(sdb).execute(
        hid, AllocateRequest(admission_id=det.id, ward_id=ward.id,
                             room_id=room.id, bed_id=beds[0].id), ACTOR)
    assert out.bed_id == beds[0].id
    # second patient awaiting bed tries same bed -> 409
    from modules.patients.entities.patient import Patient, PatientStatus
    pat2 = Patient(
        id=uuid4(), hospital_id=hid, uhid=f"U{uuid4().hex[:8]}",
        first_name="T2", last_name="P2", name="T2 P2",
        mobile=f"8{uuid4().int % 10**9:09d}", status=PatientStatus.active)
    sdb.add(pat2)
    sdb.commit()
    adm2, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat2.id, ACTOR)
    det2 = AcceptAdmissionAction(sdb).execute(hid, ACTOR, admission_id=adm2.id)
    with pytest.raises(HTTPException) as ei:
        AllocateBedAction(sdb).execute(
            hid, AllocateRequest(admission_id=det2.id, ward_id=ward.id,
                                 room_id=room.id, bed_id=beds[0].id), ACTOR)
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, dict) and ei.value.detail.get("code") == "BED_OCCUPIED"


def test_cross_doctor_visibility_blocks_transfer(sdb):
    from modules.doctors.actions.doctor_appointment_actions import TransferAppointmentToInpatientAction
    from modules.doctors.contracts.doctor_contracts import TransferToInpatientRequest
    from modules.inpatient.actions.admission_lifecycle_actions import (
        EnsureAdmissionRequestAction, AcceptAdmissionAction)
    from modules.appointments.entities.appointment import Appointment, AppointmentStatus

    hid, doc_a, doc_b, pat, ward, room, beds = _seed(sdb)
    # doctor A creates + accepts episode
    adm, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR, doctor_id=doc_a.id)
    AcceptAdmissionAction(sdb).execute(
        hid, ACTOR, admission_id=adm.id,
        ward_id=ward.id, room_id=room.id, bed_id=beds[0].id)
    # doctor B appointment for same patient tries transfer -> must 409
    appt = Appointment(
        id=uuid4(), hospital_id=hid, doctor_id=doc_b.id, patient_id=pat.id,
        appointment_date=date.today(), appointment_time=time(11, 0),
        status=AppointmentStatus.scheduled, purpose="x")
    sdb.add(appt)
    sdb.commit()
    with pytest.raises(HTTPException) as ei:
        TransferAppointmentToInpatientAction(sdb).execute(
            hid, doc_b.id, appt.id, TransferToInpatientRequest(), ACTOR)
    assert ei.value.status_code == 409


def test_emergency_admit_on_requested_converges(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import EnsureAdmissionRequestAction
    from modules.emergency.actions.emergency_actions import RecordEmergencyDispositionAction
    from modules.emergency.contracts.emergency_contracts import EmergencyDispositionCreate
    from modules.emergency.entities.emergency_entities import (
        EmergencyEncounter, EmergencyStatus, EmergencyDispositionType)
    from modules.inpatient.entities.admission import Admission, AdmissionStatus

    hid, _, _, pat, ward, room, beds = _seed(sdb)
    adm_req, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    enc = EmergencyEncounter(
        hospital_id=hid, patient_id=pat.id, er_id=f"ER-{uuid4().hex[:6]}",
        status=EmergencyStatus.in_treatment, presenting_complaints="cp")
    sdb.add(enc)
    sdb.commit()
    out = RecordEmergencyDispositionAction(sdb, hid).execute(
        enc.id,
        EmergencyDispositionCreate(
            disposition_type=EmergencyDispositionType.admit_ipd,
            destination_ward_id=ward.id, destination_bed_id=beds[0].id),
        ACTOR)
    assert out.admission_id == adm_req.id  # converged, not duplicated
    rows = sdb.query(Admission).filter(
        Admission.hospital_id == hid, Admission.patient_id == pat.id).all()
    assert len(rows) == 1
    assert rows[0].status == AdmissionStatus.admitted


def test_emergency_admit_on_admitted_409(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import (
        EnsureAdmissionRequestAction, AcceptAdmissionAction)
    from modules.emergency.actions.emergency_actions import RecordEmergencyDispositionAction
    from modules.emergency.contracts.emergency_contracts import EmergencyDispositionCreate
    from modules.emergency.entities.emergency_entities import (
        EmergencyEncounter, EmergencyStatus, EmergencyDispositionType)

    hid, _, _, pat, ward, room, beds = _seed(sdb)
    adm_req, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    AcceptAdmissionAction(sdb).execute(
        hid, ACTOR, admission_id=adm_req.id,
        ward_id=ward.id, room_id=room.id, bed_id=beds[0].id)
    enc = EmergencyEncounter(
        hospital_id=hid, patient_id=pat.id, er_id=f"ER-{uuid4().hex[:6]}",
        status=EmergencyStatus.in_treatment, presenting_complaints="cp")
    sdb.add(enc)
    sdb.commit()
    with pytest.raises(HTTPException) as ei:
        RecordEmergencyDispositionAction(sdb, hid).execute(
            enc.id,
            EmergencyDispositionCreate(
                disposition_type=EmergencyDispositionType.admit_ipd,
                destination_ward_id=ward.id, destination_bed_id=beds[1].id),
            ACTOR)
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, dict)


def test_ot_postop_on_requested_accepts_not_duplicate(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import EnsureAdmissionRequestAction
    from modules.ot.actions.ot_actions import sync_ot_icu_transfer
    from modules.ot.entities.ot_entities import OtSurgery, OtSurgeryStatus
    from modules.inpatient.entities.admission import Admission, AdmissionStatus

    hid, _, _, pat, ward, room, beds = _seed(sdb)
    adm_req, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    surg = OtSurgery(
        hospital_id=hid, surgery_no=f"S-{uuid4().hex[:6]}",
        patient_id=pat.id, surgery_type="Appx",
        surgery_category="General", scheduled_at=date.today(),
        shifted_to="ICU", status=OtSurgeryStatus.completed)
    sdb.add(surg)
    sdb.commit()
    sync_ot_icu_transfer(sdb, hid, surg, ACTOR, target_bed_id=beds[0].id)
    sdb.commit()
    rows = sdb.query(Admission).filter(
        Admission.hospital_id == hid, Admission.patient_id == pat.id).all()
    assert len(rows) == 1
    assert rows[0].id == adm_req.id
    assert rows[0].status == AdmissionStatus.admitted
    assert rows[0].bed_id == beds[0].id


def test_open_admissions_endpoint_returns_row_or_null(sdb):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infrastructure.postgres.session import get_transitional_sync_session
    from modules.beds.api.beds_api import router as beds_router
    from modules.inpatient.actions.admission_lifecycle_actions import EnsureAdmissionRequestAction
    from shared.auth.jwt import create_access_token
    from shared.exceptions.handlers import register_exception_handlers

    hid, _, _, pat, _, _, _ = _seed(sdb)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(beds_router, prefix="/api")
    app.dependency_overrides[get_transitional_sync_session] = lambda: sdb
    client = TestClient(app, raise_server_exceptions=False)
    token = create_access_token({
        "sub": "admin@hospital.com", "name": "Admin",
        "role": "hospital_admin", "hospital_uuid": str(hid),
    })
    headers = {"Authorization": f"Bearer {token}"}

    # No episode -> 200 null.
    r0 = client.get(f"/api/beds/admissions/open?patient_id={pat.id}", headers=headers)
    assert r0.status_code == 200, r0.text
    assert r0.json() is None

    adm, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    r1 = client.get(f"/api/beds/admissions/open?patient_id={pat.id}", headers=headers)
    assert r1.status_code == 200, r1.text
    assert r1.json()["id"] == str(adm.id)
    assert r1.json()["status"] == "requested"


def test_accept_by_patient_lookup_admitted_409(sdb):
    from modules.inpatient.actions.admission_lifecycle_actions import (
        EnsureAdmissionRequestAction, AcceptAdmissionAction)

    hid, _, _, pat, ward, room, beds = _seed(sdb)
    adm_req, _ = EnsureAdmissionRequestAction(sdb).execute(hid, pat.id, ACTOR)
    AcceptAdmissionAction(sdb).execute(
        hid, ACTOR, admission_id=adm_req.id,
        ward_id=ward.id, room_id=room.id, bed_id=beds[0].id)
    with pytest.raises(HTTPException) as ei:
        AcceptAdmissionAction(sdb).execute(hid, ACTOR, patient_id=pat.id)
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, dict)
