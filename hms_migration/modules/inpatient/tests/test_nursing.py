"""
Unit and integration tests for Nursing Management, Merged Clinical Notes, and eMAR.

Features tested:
- Feature 12: Nursing Care Planning (/ipd/admissions/{id}/care-plans, /ipd/care-plans/{id}/reassess)
- Feature 13: Merged IPD Clinical Notes (/ipd/admissions/{id}/clinical-notes, /ipd/clinical-notes/{id})
- Feature 14: Shift Handovers SBAR (/ipd/admissions/{id}/handovers, /ipd/handovers/{id}/acknowledge)
- Feature 15: Electronic Medication Administration Record (eMAR) (/ipd/admissions/{id}/emar)
- Feature 18: High-Alert Dual Sign-Off enforcement in eMAR (/ipd/emar/{id}/record)
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.models import Bed, Hospital, HospitalUser, Patient, Room, Ward, WardType
from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.inpatient.api.nursing_api import router as nursing_router
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, doctor, hospital, hospital_b, patient, patient_b


@pytest.fixture(scope="function")
def nursing_app(db_session: Session) -> FastAPI:
    test_app = FastAPI(title="Nursing Test App")
    test_app.include_router(nursing_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(nursing_app: FastAPI) -> TestClient:
    return TestClient(nursing_app)


@pytest.fixture(scope="function")
def nurse_headers(hospital: Hospital) -> dict[str, str]:
    token = create_access_token(
        data={
            "sub": "nurse.priya@hospital.test",
            "name": "Nurse Priya",
            "email": "nurse.priya@hospital.test",
            "role": "hospital_staff",
            "staff_role_name": "nurse",
            "hospital_uuid": str(hospital.id),
            "user_id": str(uuid.uuid4()),
        }
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Hospital-ID": str(hospital.id),
    }


@pytest.fixture(scope="function")
def test_admission(db_session: Session, hospital: Hospital, patient: Patient) -> Admission:
    Base.metadata.create_all(bind=db_session.get_bind())

    ward = Ward(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Post-Op Ward 1",
        ward_type=WardType.general,
    )
    db_session.add(ward)
    db_session.flush()

    room = Room(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_code="302",
    )
    db_session.add(room)
    db_session.flush()

    bed = Bed(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="302-A",
        is_occupied=True,
    )
    db_session.add(bed)
    db_session.flush()

    admission = Admission(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        status=AdmissionStatus.admitted,
        ip_id="IP-2026-TEST",
    )
    db_session.add(admission)
    db_session.commit()
    db_session.refresh(admission)
    return admission


def test_feature_12_care_plan(client: TestClient, nurse_headers: dict[str, str], test_admission: Admission):
    adm = test_admission

    # 1. Create Care Plan
    create_payload = {
        "nursing_diagnosis": "Acute Pain related to surgical incision",
        "diagnosis_code": "00132",
        "goals": "Patient reports pain level <= 3/10 within 2 hours of analgesic administration",
        "interventions": {
            "pharmacological": "Administer analgesics as prescribed",
            "non_pharmacological": "Positioning, cold compression pack",
        },
        "evaluation_frequency": "every_shift",
    }
    res = client.post(f"/api/ipd/admissions/{adm.id}/care-plans", json=create_payload, headers=nurse_headers)
    assert res.status_code == 201, res.text
    plan = res.json()
    assert plan["nursing_diagnosis"] == create_payload["nursing_diagnosis"]
    assert plan["status"] == "active"
    plan_id = plan["id"]

    # 2. List Care Plans
    res = client.get(f"/api/ipd/admissions/{adm.id}/care-plans", headers=nurse_headers)
    assert res.status_code == 200
    plans = res.json()
    assert len(plans) == 1
    assert plans[0]["id"] == plan_id

    # 3. Reassess Care Plan
    reassess_payload = {
        "status": "resolved",
        "reassessment_notes": "Pain scale reduced to 1/10. Patient resting comfortably.",
    }
    res = client.put(f"/api/ipd/care-plans/{plan_id}/reassess", json=reassess_payload, headers=nurse_headers)
    assert res.status_code == 200
    reassessed = res.json()
    assert reassessed["status"] == "resolved"
    assert reassessed["reassessment_notes"] == reassess_payload["reassessment_notes"]
    assert reassessed["reassessed_at"] is not None


def test_feature_13_merged_clinical_notes(client: TestClient, nurse_headers: dict[str, str], test_admission: Admission):
    adm = test_admission

    # 1. Nurse Note
    nurse_payload = {
        "note_type": "nursing_progress",
        "shift_type": "morning",
        "subjective": "Patient reports feeling refreshed after morning care",
        "objective_vitals": {"bp": "118/76", "pulse": 72, "spo2": 99},
        "assessment": "Stable vitals, incisional site clean and intact",
        "plan": "Continue current care plan, assist with ambulation",
        "note_content": "Wound dressing changed under sterile precautions. No erythema.",
        "signature_data": "Nurse Priya, RN",
    }
    res = client.post(f"/api/ipd/admissions/{adm.id}/clinical-notes", json=nurse_payload, headers=nurse_headers)
    assert res.status_code == 201, res.text
    nurse_note = res.json()
    assert nurse_note["note_type"] == "nursing_progress"
    note_id = nurse_note["id"]

    # 2. Doctor Note
    doc_payload = {
        "note_type": "doctor_progress",
        "shift_type": "morning",
        "note_content": "Reviewed post-op recovery. Patient clear to mobilize.",
    }
    res = client.post(f"/api/ipd/admissions/{adm.id}/clinical-notes", json=doc_payload, headers=nurse_headers)
    assert res.status_code == 201, res.text

    # 3. List All Notes
    res = client.get(f"/api/ipd/admissions/{adm.id}/clinical-notes", headers=nurse_headers)
    assert res.status_code == 200
    all_notes = res.json()
    assert len(all_notes) == 2

    # 4. Get by ID
    res = client.get(f"/api/ipd/clinical-notes/{note_id}", headers=nurse_headers)
    assert res.status_code == 200
    single_note = res.json()
    assert single_note["id"] == note_id


def test_feature_14_shift_handover(client: TestClient, nurse_headers: dict[str, str], test_admission: Admission):
    adm = test_admission

    handover_payload = {
        "shift_date": "2026-09-12",
        "shift_type": "morning_to_evening",
        "situation": "42-year-old female post-op day 1 lap cholecystectomy",
        "background": "Uncomplicated surgery, pain managed with analgesics",
        "assessment_acuity": "Level 3 - Stable",
        "pending_stat_orders": {"repeat_cbc": "due tomorrow morning"},
        "pending_tasks": {"diet": "advance to soft diet as tolerated"},
    }
    res = client.post(f"/api/ipd/admissions/{adm.id}/handovers", json=handover_payload, headers=nurse_headers)
    assert res.status_code == 201, res.text
    handover = res.json()
    assert handover["situation"] == handover_payload["situation"]
    assert handover["is_acknowledged"] is False
    handover_id = handover["id"]

    # List
    res = client.get(f"/api/ipd/admissions/{adm.id}/handovers", headers=nurse_headers)
    assert res.status_code == 200
    assert len(res.json()) == 1

    # Acknowledge
    ack_res = client.post(
        f"/api/ipd/handovers/{handover_id}/acknowledge",
        json={"remarks": "Handover received at bedside"},
        headers=nurse_headers,
    )
    assert ack_res.status_code == 200
    assert ack_res.json()["is_acknowledged"] is True


def test_feature_15_and_18_emar_dual_signoff(client: TestClient, nurse_headers: dict[str, str], test_admission: Admission):
    adm = test_admission

    # 1. Schedule standard med
    sched_std = {
        "medicine_name": "Cefixime 200mg Tab",
        "dose": "200mg",
        "route": "Oral",
        "scheduled_time": datetime.now(timezone.utc).isoformat(),
        "is_high_alert": False,
    }
    res = client.post(f"/api/ipd/admissions/{adm.id}/emar", json=sched_std, headers=nurse_headers)
    assert res.status_code == 201, res.text
    std_rec = res.json()
    std_id = std_rec["id"]

    # 2. Administer standard med (single nurse success)
    exec_std = {
        "status": "administered",
        "vitals_before_admin": {"temp": 98.6},
        "notes_or_reason": "Administered post meals",
    }
    res = client.put(f"/api/ipd/emar/{std_id}/record", json=exec_std, headers=nurse_headers)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "administered"

    # 3. Schedule high-alert med (Potassium Chloride IV)
    sched_ha = {
        "medicine_name": "Potassium Chloride Concentrated Solution IV",
        "dose": "20 mEq in 100mL Normal Saline",
        "route": "IV Infusion",
        "scheduled_time": datetime.now(timezone.utc).isoformat(),
        "is_high_alert": True,
    }
    res = client.post(f"/api/ipd/admissions/{adm.id}/emar", json=sched_ha, headers=nurse_headers)
    assert res.status_code == 201, res.text
    ha_id = res.json()["id"]

    # 4. Attempt high-alert administration WITHOUT dual sign-off -> MUST FAIL
    exec_fail = {
        "status": "administered",
        "vitals_before_admin": {"hr": 78},
        "notes_or_reason": "Infusing at 10 mEq/hr via infusion pump",
    }
    res = client.put(f"/api/ipd/emar/{ha_id}/record", json=exec_fail, headers=nurse_headers)
    assert res.status_code == 422
    assert "Dual sign-off" in res.json()["detail"]

    # 5. Administer high-alert med WITH dual sign-off -> MUST SUCCEED
    exec_pass = {
        "status": "administered",
        "vitals_before_admin": {"hr": 78},
        "witness_nurse_id": str(uuid.uuid4()),
        "witness_nurse_name": "Nurse Sarah, BSN",
        "notes_or_reason": "Concentration and rate verified by Nurse Sarah",
    }
    res = client.put(f"/api/ipd/emar/{ha_id}/record", json=exec_pass, headers=nurse_headers)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "administered"
    assert res.json()["witness_nurse_name"] == "Nurse Sarah, BSN"

    # 6. List eMAR records
    res = client.get(f"/api/ipd/admissions/{adm.id}/emar", headers=nurse_headers)
    assert res.status_code == 200
    records = res.json()
    assert len(records) == 2
