"""
Tests for ICU direct admission, inpatient-to-ICU transfer, step-down,
and Operation Theatre (OT) post-op handover into ICU.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from modules.beds.entities.bed import Bed, Room, Ward, WardType
from modules.doctors.entities.doctor import HospitalUser
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from infrastructure.postgres.session import get_transitional_sync_session
from modules.critical_care.api.critical_care_api import router as critical_care_router
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.ot.api.ot_api import router as ot_router
from modules.ot.entities.ot_entities import OtRoom, OtSurgery, OtSurgeryStatus, OtPriority
from modules.masters.entities.organization_entities import Department
from shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b, doctor


@pytest.fixture(scope="function")
def cc_app(db_session: Session) -> FastAPI:
    test_app = FastAPI(title="ICU & OT Handover Test App")
    test_app.include_router(critical_care_router, prefix="/api")
    test_app.include_router(ot_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(cc_app: FastAPI) -> TestClient:
    return TestClient(cc_app)


@pytest.fixture(scope="function")
def staff_auth(hospital: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "intensivist@hospital.com",
        "name": "Dr. Intensivist",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital.id),
        "user_id": str(uuid4()),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def icu_beds_setup(db_session: Session, hospital: Hospital):
    ward = Ward(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Main ICU Ward",
        ward_type=WardType.icu,
    )
    room = Room(id=uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_code="ICU-101")
    bed1 = Bed(id=uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_id=room.id, bed_code="ICU-B1", is_occupied=False)
    bed2 = Bed(id=uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_id=room.id, bed_code="ICU-B2", is_occupied=False)

    gen_ward = Ward(
        id=uuid4(),
        hospital_id=hospital.id,
        name="General Ward A",
        ward_type=WardType.general,
    )
    gen_room = Room(id=uuid4(), hospital_id=hospital.id, ward_id=gen_ward.id, room_code="GEN-101")
    gen_bed = Bed(id=uuid4(), hospital_id=hospital.id, ward_id=gen_ward.id, room_id=gen_room.id, bed_code="GEN-B1", is_occupied=False)

    db_session.add_all([ward, room, bed1, bed2, gen_ward, gen_room, gen_bed])
    db_session.commit()
    return {
        "icu_ward": ward,
        "icu_bed1": bed1,
        "icu_bed2": bed2,
        "gen_ward": gen_ward,
        "gen_bed": gen_bed,
    }


def test_list_available_icu_beds(client: TestClient, icu_beds_setup: dict, staff_auth: dict[str, str]):
    res = client.get("/api/critical-care/available-beds", headers=staff_auth)
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 2
    bed_codes = [b["bed_code"] for b in data]
    assert "ICU-B1" in bed_codes
    assert "ICU-B2" in bed_codes


def test_direct_icu_admission(client: TestClient, db_session: Session, patient: Patient, icu_beds_setup: dict, staff_auth: dict[str, str]):
    icu_bed = icu_beds_setup["icu_bed1"]
    icu_ward = icu_beds_setup["icu_ward"]

    payload = {
        "patient_id": str(patient.id),
        "ward_id": str(icu_ward.id),
        "bed_id": str(icu_bed.id),
        "diagnosis": "Severe Sepsis with Septic Shock",
        "acuity": "critical",
        "ventilator_mode": "SIMV-PC",
        "peep": 8.0,
        "fio2_percent": 60.0,
        "gcs_score": 10,
        "inotrope_support": True,
        "inotrope_details": "Noradrenaline 0.1 mcg/kg/min",
        "invasive_lines": {"cvc": 1, "arterial_line": 1, "foley": 1},
        "notes": "Direct admission from Emergency to ICU",
    }

    res = client.post("/api/critical-care/admit", json=payload, headers=staff_auth)
    assert res.status_code == 201
    data = res.json()
    assert data["patient_name"] == patient.name
    assert data["bed_code"] == "ICU-B1"
    assert data["ventilator_mode"] == "SIMV-PC"
    assert data["peep"] == 8.0
    assert data["fio2_percent"] == 60.0
    assert data["inotrope_support"] is True
    assert data["care_indicators"]["acuity"] == "critical"

    # Verify board reflects this patient
    board_res = client.get("/api/critical-care/icu-board", headers=staff_auth)
    assert board_res.status_code == 200
    board = board_res.json()
    assert any(p["patient_id"] == str(patient.id) and p["bed_code"] == "ICU-B1" for p in board)


def test_inpatient_transfer_to_icu(client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, icu_beds_setup: dict, staff_auth: dict[str, str]):
    gen_ward = icu_beds_setup["gen_ward"]
    gen_bed = icu_beds_setup["gen_bed"]
    icu_ward = icu_beds_setup["icu_ward"]
    icu_bed = icu_beds_setup["icu_bed2"]

    # Patient is admitted in General Ward
    adm = Admission(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        ward_id=gen_ward.id,
        room_id=gen_bed.room_id,
        bed_id=gen_bed.id,
        status=AdmissionStatus.admitted,
        ip_id="IP-2026-99991",
        admitted_at=datetime.now(timezone.utc),
    )
    gen_bed.is_occupied = True
    db_session.add(adm)
    db_session.commit()

    # Transfer to ICU
    transfer_payload = {
        "admission_id": str(adm.id),
        "to_ward_id": str(icu_ward.id),
        "to_bed_id": str(icu_bed.id),
        "transfer_reason": "Acute respiratory distress requiring invasive ventilation",
        "acuity": "critical",
        "ventilator_mode": "PRVC",
        "peep": 10.0,
        "fio2_percent": 70.0,
        "gcs_score": 11,
    }
    res = client.post("/api/critical-care/transfer", json=transfer_payload, headers=staff_auth)
    assert res.status_code == 200
    data = res.json()
    assert data["bed_code"] == "ICU-B2"
    assert data["ventilator_mode"] == "PRVC"

    # Verify previous bed is now unoccupied and ICU bed is occupied
    db_session.refresh(gen_bed)
    db_session.refresh(icu_bed)
    assert gen_bed.is_occupied is False
    assert icu_bed.is_occupied is True

    # Test step-down
    stepdown_payload = {
        "admission_id": str(adm.id),
        "to_ward_id": str(gen_ward.id),
        "to_bed_id": str(gen_bed.id),
        "step_down_notes": "Extubated, hemodynamically stable. Stepped down to ward.",
    }
    step_res = client.post("/api/critical-care/step-down", json=stepdown_payload, headers=staff_auth)
    assert step_res.status_code == 200
    db_session.refresh(gen_bed)
    db_session.refresh(icu_bed)
    assert gen_bed.is_occupied is True
    assert icu_bed.is_occupied is False


def test_ot_surgery_completion_transfers_patient_to_icu(client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, icu_beds_setup: dict, staff_auth: dict[str, str]):
    dept = Department(
        id=uuid4(),
        hospital_id=hospital.id,
        name="General Surgery",
        code="SURG",
        is_active=True,
    )
    ot_room = OtRoom(
        id=uuid4(),
        hospital_id=hospital.id,
        department_id=dept.id,
        code="OT-2",
        name="Operating Theatre 2",
        base_ot_charge=12000.0,
        is_active=True,
    )
    db_session.add_all([dept, ot_room])
    db_session.commit()

    surgery = OtSurgery(
        id=uuid4(),
        hospital_id=hospital.id,
        surgery_no="SURG-2026-00042",
        patient_id=patient.id,
        surgery_type="Emergency Laparotomy",
        surgery_category="General",
        priority=OtPriority.emergency,
        department_id=dept.id,
        ot_room_id=ot_room.id,
        ot_room="OT-2",
        scheduled_at=datetime.now(timezone.utc),
        status=OtSurgeryStatus.in_progress,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(surgery)
    db_session.commit()

    # Complete surgery with shifted_to: "ICU"
    complete_payload = {
        "shifted_to": "ICU",
        "target_bed_id": str(icu_beds_setup["icu_bed1"].id),
        "ventilator_mode": "Mechanical Ventilation (Post-Op)",
        "peep": 6.0,
        "fio2_percent": 50.0,
    }
    complete_res = client.post(f"/api/ot/surgeries/{surgery.id}/complete", json=complete_payload, headers=staff_auth)
    assert complete_res.status_code == 200
    assert complete_res.json()["status"] == "completed"

    # Check ICU Board: Patient must now be in the ICU census!
    board_res = client.get("/api/critical-care/icu-board", headers=staff_auth)
    assert board_res.status_code == 200
    board = board_res.json()
    matching = [p for p in board if p["patient_id"] == str(patient.id)]
    assert len(matching) >= 1
    icu_pt = matching[0]
    assert icu_pt["bed_code"] == "ICU-B1"
    assert icu_pt["ventilator_mode"] == "Mechanical Ventilation (Post-Op)"
    assert icu_pt["peep"] == 6.0
    assert icu_pt["fio2_percent"] == 50.0
