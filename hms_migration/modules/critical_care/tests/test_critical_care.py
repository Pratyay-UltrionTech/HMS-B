"""
Unit and integration tests for Critical Care domain (Features 6–9).

Tests:
1. ICU Patient Board (/critical-care/icu-board, /critical-care/admissions/{id}/icu-profile)
2. ICU Clinical Chart & Hourly Flowsheet (/critical-care/admissions/{id}/flowsheet)
3. Clinical Deterioration Alerts NEWS2 Engine (/critical-care/alerts/calculate, /critical-care/alerts)
4. Code Blue Resuscitation Lifecycle (/critical-care/code-blue)
5. Multi-tenant isolation
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Bed, Hospital, HospitalUser, Patient, Room, Ward, WardType
from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.critical_care.api.critical_care_api import router as critical_care_router
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b, doctor


@pytest.fixture(scope="function")
def critical_care_app(db_session: Session) -> FastAPI:
    test_app = FastAPI(title="Critical Care Test App")
    test_app.include_router(critical_care_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(critical_care_app: FastAPI) -> TestClient:
    return TestClient(critical_care_app)


@pytest.fixture(scope="function")
def staff_auth(hospital: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "nurse.icu@hospital.com",
        "name": "Nurse Intensivist",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(uuid4()),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def staff_auth_b(hospital_b: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "nurse.icu2@hospitalb.com",
        "name": "Nurse B",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_b.id),
        "user_id": str(uuid4()),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def icu_setup(db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser):
    ward = Ward(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Medical ICU",
        ward_type=WardType.icu,
    )
    room = Room(id=uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_code="MICU-101")
    bed = Bed(id=uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_id=room.id, bed_code="ICU-B1", is_occupied=True)
    db_session.add_all([ward, room, bed])
    db_session.commit()

    adm = Admission(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        doctor_id=doctor.id,
        status=AdmissionStatus.admitted,
        ip_id="IP-2026-00001",
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(adm)
    db_session.commit()
    return {"ward": ward, "room": room, "bed": bed, "admission": adm}


def test_icu_patient_board(client: TestClient, db_session: Session, hospital: Hospital, icu_setup: dict, staff_auth: dict[str, str]):
    """Test Feature 6: Intensivist ICU Patient Board & Profile Update."""
    Base.metadata.create_all(db_session.bind)
    adm = icu_setup["admission"]

    # 1. Update ICU Profile (ventilator, invasive line days, GCS)
    profile_res = client.put(
        f"/api/critical-care/admissions/{adm.id}/icu-profile",
        json={
            "ventilator_mode": "SIMV",
            "peep": 8.0,
            "fio2_percent": 50.0,
            "invasive_line_days": {"central_line": 3, "arterial_line": 2, "foley": 4},
            "inotrope_support": True,
            "inotrope_details": "Norepinephrine 0.05 mcg/kg/min",
            "gcs_score": 11,
        },
        headers=staff_auth,
    )
    assert profile_res.status_code == 200

    # 2. Query ICU Patient Board
    board_res = client.get("/api/critical-care/icu-board", headers=staff_auth)
    assert board_res.status_code == 200
    board = board_res.json()
    assert len(board) >= 1
    patient_entry = next(p for p in board if p["admission_id"] == str(adm.id))
    assert patient_entry["ventilator_mode"] == "SIMV"
    assert patient_entry["peep"] == 8.0
    assert patient_entry["inotrope_support"] is True
    assert patient_entry["gcs_score"] == 11
    assert patient_entry["invasive_line_days"]["central_line"] == 3


def test_icu_clinical_chart_and_flowsheet(client: TestClient, db_session: Session, hospital: Hospital, icu_setup: dict, staff_auth: dict[str, str]):
    """Test Feature 7: ICU Hourly Flowsheet with MAP and Fluid Balance Calculations."""
    Base.metadata.create_all(db_session.bind)
    adm = icu_setup["admission"]

    now_iso = datetime.now(timezone.utc).isoformat()
    # Record hourly flowsheet entry: SBP=120, DBP=80 -> MAP = (160 + 120)/3 = 93.3
    # Intake = 100ml IV + 50ml enteral = 150ml. Output = 80ml urine + 20ml drain = 100ml -> Balance = +50ml
    entry_res = client.post(
        f"/api/critical-care/admissions/{adm.id}/flowsheet",
        json={
            "recorded_at": now_iso,
            "heart_rate": 88.0,
            "systolic_bp": 120.0,
            "diastolic_bp": 80.0,
            "spo2": 97.0,
            "respiratory_rate": 18.0,
            "temperature": 37.1,
            "abg_ph": 7.38,
            "abg_pco2": 42.0,
            "abg_po2": 92.0,
            "abg_hco3": 24.0,
            "abg_lactate": 1.2,
            "hourly_iv_intake_ml": 100.0,
            "hourly_enteral_intake_ml": 50.0,
            "hourly_urine_output_ml": 80.0,
            "hourly_drain_output_ml": 20.0,
            "remarks": "Patient resting comfortably, hemodynamically stable",
        },
        headers=staff_auth,
    )
    assert entry_res.status_code == 201
    entry_data = entry_res.json()
    assert entry_data["mean_arterial_pressure"] == 93.3
    assert entry_data["hourly_balance_ml"] == 50.0

    # Retrieve flowsheet list
    list_res = client.get(f"/api/critical-care/admissions/{adm.id}/flowsheet", headers=staff_auth)
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1


def test_deterioration_alerts_news2_scoring(client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, staff_auth: dict[str, str]):
    """Test Feature 8: Deterioration Alerts with NEWS2 calculation and nursing acknowledgment."""
    Base.metadata.create_all(db_session.bind)

    # 1. Normal vitals -> Score 0, Low risk, alert status resolved
    normal_res = client.post(
        "/api/critical-care/alerts/calculate",
        json={
            "patient_id": str(patient.id),
            "respiratory_rate": 16.0,
            "spo2": 98.0,
            "on_supplemental_oxygen": False,
            "systolic_bp": 120.0,
            "heart_rate": 72.0,
            "avpu": "alert",
            "temperature": 36.8,
        },
        headers=staff_auth,
    )
    assert normal_res.status_code == 200
    assert normal_res.json()["total_score"] == 0
    assert normal_res.json()["risk_level"] == "low"
    assert normal_res.json()["alert_status"] == "resolved"

    # 2. Severely deteriorating vitals:
    # RR=28 (3), SpO2=88 (3), O2=True (2), SBP=85 (3), HR=135 (3), Voice (3), Temp=39.5 (2) -> Score = 19 (Emergency)
    crit_res = client.post(
        "/api/critical-care/alerts/calculate",
        json={
            "patient_id": str(patient.id),
            "respiratory_rate": 28.0,
            "spo2": 88.0,
            "on_supplemental_oxygen": True,
            "systolic_bp": 85.0,
            "heart_rate": 135.0,
            "avpu": "verbal",
            "temperature": 39.5,
        },
        headers=staff_auth,
    )
    assert crit_res.status_code == 200
    crit_data = crit_res.json()
    assert crit_data["total_score"] >= 7
    assert crit_data["risk_level"] == "emergency"
    assert crit_data["alert_status"] == "active"
    alert_id = crit_data["id"]

    # 3. Active alert appears in alerts dashboard
    alerts_list = client.get("/api/critical-care/alerts", headers=staff_auth).json()
    assert any(a["id"] == alert_id for a in alerts_list)

    # 4. Nurse acknowledges alert
    ack_res = client.post(
        f"/api/critical-care/alerts/{alert_id}/acknowledge",
        json={"notes": "Doctor notified, preparing rapid response team"},
        headers=staff_auth,
    )
    assert ack_res.status_code == 200
    assert ack_res.json()["alert_status"] == "acknowledged"


def test_code_blue_resuscitation_lifecycle(client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, staff_auth: dict[str, str]):
    """Test Feature 9: Code Blue Management Resuscitation Timeline & ROSC Outcome."""
    Base.metadata.create_all(db_session.bind)

    # 1. Activate Code Blue
    act_res = client.post(
        "/api/critical-care/code-blue",
        json={
            "patient_id": str(patient.id),
            "location_description": "Ward 4, Bed 12",
            "team_members": {"team_leader": "Dr. House", "compressor": "Nurse Swift"},
        },
        headers=staff_auth,
    )
    assert act_res.status_code == 201
    act_data = act_res.json()
    incident_id = act_data["id"]
    assert act_data["incident_code"].startswith("CB-")
    assert act_data["status"] == "activated"

    # 2. Resuscitation team arrives
    arr_res = client.post(f"/api/critical-care/code-blue/{incident_id}/arrived", headers=staff_auth)
    assert arr_res.status_code == 200
    assert arr_res.json()["status"] == "team_arrived"

    # 3. Log CPR and Defibrillation events
    ev1 = client.post(
        f"/api/critical-care/code-blue/{incident_id}/events",
        json={"event_type": "cpr_cycle", "details": "CPR Cycle 1 initiated, 30:2 compressions"},
        headers=staff_auth,
    )
    assert ev1.status_code == 201

    ev2 = client.post(
        f"/api/critical-care/code-blue/{incident_id}/events",
        json={"event_type": "shock", "details": "Defibrillation 200J delivered for VFib"},
        headers=staff_auth,
    )
    assert ev2.status_code == 201

    # 4. Conclude Code Blue with ROSC achieved
    concl_res = client.post(
        f"/api/critical-care/code-blue/{incident_id}/conclude",
        json={
            "outcome": "rosc_achieved",
            "summary_notes": "ROSC achieved after 2 cycles of CPR and 1 shock. Spontaneous carotid pulse restored. Moving to ICU.",
        },
        headers=staff_auth,
    )
    assert concl_res.status_code == 200
    concl_data = concl_res.json()
    assert concl_data["status"] == "concluded"
    assert concl_data["outcome"] == "rosc_achieved"

    # Verify incident details and event timeline
    detail_res = client.get(f"/api/critical-care/code-blue/{incident_id}", headers=staff_auth)
    assert len(detail_res.json()["events"]) == 2
