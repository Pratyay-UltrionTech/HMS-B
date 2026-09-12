"""
Unit and integration tests for Emergency Department module (Features 1–5).

Tests:
1. Emergency Case Registration (/emergency/encounters)
2. Emergency Triage Assessment (/emergency/encounters/{id}/triage)
3. Triage Priority Queue & Escalation (/emergency/queue, /emergency/encounters/{id}/escalate)
4. Emergency Orders & Nurse Execution (/emergency/encounters/{id}/orders, /emergency/orders/{id}/execute)
5. Disposition Tracking (/emergency/encounters/{id}/disposition)
6. Cross-hospital multi-tenant isolation
"""

from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hospital, HospitalUser, Patient, StaffRole
from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.emergency.api.emergency_api import router as emergency_router
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b, doctor


@pytest.fixture(scope="function")
def emergency_app(db_session: Session) -> FastAPI:
    """Isolated FastAPI test app for emergency module."""
    test_app = FastAPI(title="Emergency Test App")
    test_app.include_router(emergency_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(emergency_app: FastAPI) -> TestClient:
    return TestClient(emergency_app)


@pytest.fixture(scope="function")
def staff_auth(hospital: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "nurse.er@hospital.com",
        "name": "Nurse Swift",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(uuid4()),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def staff_auth_b(hospital_b: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "nurse.er2@hospitalb.com",
        "name": "Nurse B",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_b.id),
        "user_id": str(uuid4()),
    })
    return {"Authorization": f"Bearer {token}"}


def test_emergency_registration_lifecycle(client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser, staff_auth: dict[str, str]):
    """Test Feature 1: Emergency Case Registration."""
    Base.metadata.create_all(db_session.bind)

    # 1. Register emergency encounter
    res = client.post(
        "/api/emergency/encounters",
        json={
            "patient_id": str(patient.id),
            "arrival_source": "ambulance",
            "presenting_complaints": "Severe chest pain radiating to left arm",
            "attending_doctor_id": str(doctor.id),
        },
        headers=staff_auth,
    )
    assert res.status_code == 201
    data = res.json()
    encounter_id = data["id"]
    assert data["er_id"].startswith(f"ER-{date.today().year}-")
    assert data["status"] == "registered"
    assert data["arrival_source"] == "ambulance"

    # 2. Duplicate active encounter should be rejected (409 Conflict)
    dup_res = client.post(
        "/api/emergency/encounters",
        json={
            "patient_id": str(patient.id),
            "arrival_source": "walk_in",
            "presenting_complaints": "Another complaint",
        },
        headers=staff_auth,
    )
    assert dup_res.status_code == 409


def test_emergency_triage_assessment(client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, staff_auth: dict[str, str]):
    """Test Feature 2: Emergency Triage Assessment with Acuity Scoring."""
    Base.metadata.create_all(db_session.bind)

    res = client.post(
        "/api/emergency/encounters",
        json={
            "patient_id": str(patient.id),
            "arrival_source": "walk_in",
            "presenting_complaints": "Acute breathlessness, wheezing",
        },
        headers=staff_auth,
    )
    encounter_id = res.json()["id"]

    # Perform ESI Level 1 (Resuscitation) triage
    triage_res = client.post(
        f"/api/emergency/encounters/{encounter_id}/triage",
        json={
            "acuity_level": 1,
            "avpu": "verbal",
            "pain_score": 8,
            "respiratory_distress": True,
            "systolic_bp": 85.0,
            "diastolic_bp": 50.0,
            "heart_rate": 135.0,
            "respiratory_rate": 32.0,
            "spo2": 88.0,
            "red_flags": {"stridor": True, "cyanosis": True},
            "triage_notes": "Immediate resuscitation bay required",
        },
        headers=staff_auth,
    )
    assert triage_res.status_code == 201
    tdata = triage_res.json()
    assert tdata["acuity_level"] == 1
    assert tdata["respiratory_distress"] is True
    assert tdata["spo2"] == 88.0

    # Verify encounter moved to in_treatment due to Level 1 acuity
    enc_res = client.get(f"/api/emergency/encounters/{encounter_id}", headers=staff_auth)
    assert enc_res.json()["triage_level"] == 1
    assert enc_res.json()["status"] == "in_treatment"


def test_triage_queue_priority_and_escalation(client: TestClient, db_session: Session, hospital: Hospital, staff_auth: dict[str, str]):
    """Test Feature 3: Priority queue ordering by acuity and escalation."""
    Base.metadata.create_all(db_session.bind)

    # Create Patient 1 (Low Urgency, Level 4)
    p1 = Patient(
        id=uuid4(), hospital_id=hospital.id, uhid="UHID-ER-001", name="Patient Low",
        first_name="Patient", last_name="Low", mobile="555-9001", age=25, gender="male"
    )
    # Create Patient 2 (High Urgency, Level 2)
    p2 = Patient(
        id=uuid4(), hospital_id=hospital.id, uhid="UHID-ER-002", name="Patient High",
        first_name="Patient", last_name="High", mobile="555-9002", age=65, gender="female"
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    enc1 = client.post(
        "/api/emergency/encounters",
        json={"patient_id": str(p1.id), "presenting_complaints": "Minor ankle sprain"},
        headers=staff_auth,
    ).json()["id"]

    enc2 = client.post(
        "/api/emergency/encounters",
        json={"patient_id": str(p2.id), "presenting_complaints": "Suspected Stroke / Hemiparesis"},
        headers=staff_auth,
    ).json()["id"]

    # Triage p1 as Level 4
    client.post(
        f"/api/emergency/encounters/{enc1}/triage",
        json={"acuity_level": 4, "avpu": "alert", "pain_score": 3},
        headers=staff_auth,
    )
    # Triage p2 as Level 2
    client.post(
        f"/api/emergency/encounters/{enc2}/triage",
        json={"acuity_level": 2, "avpu": "verbal", "pain_score": 5},
        headers=staff_auth,
    )

    # Fetch Queue: Level 2 should appear BEFORE Level 4
    queue_res = client.get("/api/emergency/queue", headers=staff_auth)
    assert queue_res.status_code == 200
    queue = queue_res.json()
    assert len(queue) >= 2
    assert queue[0]["encounter_id"] == enc2
    assert queue[1]["encounter_id"] == enc1

    # Escalate enc1 (patient condition deteriorates suddenly)
    esc_res = client.post(
        f"/api/emergency/encounters/{enc1}/escalate",
        json={"escalation_reason": "Patient became unresponsive in waiting area"},
        headers=staff_auth,
    )
    assert esc_res.status_code == 200

    # Fetch Queue again: Escalated patient enc1 should jump to the very TOP
    queue_after = client.get("/api/emergency/queue", headers=staff_auth).json()
    assert queue_after[0]["encounter_id"] == enc1
    assert queue_after[0]["is_escalated"] is True


def test_emergency_orders_and_nurse_execution(client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, staff_auth: dict[str, str]):
    """Test Feature 4: Emergency Orders & Treatment Workflow."""
    Base.metadata.create_all(db_session.bind)

    enc_id = client.post(
        "/api/emergency/encounters",
        json={"patient_id": str(patient.id), "presenting_complaints": "Anaphylaxis to bee sting"},
        headers=staff_auth,
    ).json()["id"]

    # Place STAT Epinephrine order
    order_res = client.post(
        f"/api/emergency/encounters/{enc_id}/orders",
        json={
            "order_type": "medication",
            "description": "Epinephrine 1:1000",
            "dosage": "0.3 mg",
            "route": "IM",
            "is_stat": True,
            "is_verbal": True,
            "clinical_notes": "Immediate IM injection in anterolateral thigh",
        },
        headers=staff_auth,
    )
    assert order_res.status_code == 201
    order_data = order_res.json()
    order_id = order_data["id"]
    assert order_data["is_stat"] is True
    assert order_data["is_verbal"] is True
    assert order_data["execution_status"] == "pending"

    # Nurse executes order
    exec_res = client.post(
        f"/api/emergency/orders/{order_id}/execute",
        json={"clinical_notes": "Administered 0.3mg IM right thigh, patient breathing improved"},
        headers=staff_auth,
    )
    assert exec_res.status_code == 200
    assert exec_res.json()["execution_status"] == "completed"
    assert exec_res.json()["executed_at"] is not None


def test_emergency_disposition_tracking(client: TestClient, db_session: Session, hospital: Hospital, patient: Patient, staff_auth: dict[str, str]):
    """Test Feature 5: Disposition Tracking."""
    Base.metadata.create_all(db_session.bind)

    enc_id = client.post(
        "/api/emergency/encounters",
        json={"patient_id": str(patient.id), "presenting_complaints": "Mild laceration, sutured"},
        headers=staff_auth,
    ).json()["id"]

    # Discharge home disposition
    disp_res = client.post(
        f"/api/emergency/encounters/{enc_id}/disposition",
        json={
            "disposition_type": "discharge_home",
            "disposition_notes": "Wound dressed, tetanus toxoid administered, follow up in 5 days",
        },
        headers=staff_auth,
    )
    assert disp_res.status_code == 201
    assert disp_res.json()["disposition_type"] == "discharge_home"

    # Verify encounter is now completed
    enc = client.get(f"/api/emergency/encounters/{enc_id}", headers=staff_auth).json()
    assert enc["status"] == "completed"

    # Test disposition with admit_ipd and bed allocation
    from hms_migration.modules.beds.entities.bed import Ward, Room, Bed
    ward = Ward(id=uuid4(), hospital_id=hospital.id, name="Emergency Observation Ward", ward_type="emergency")
    room = Room(id=uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_code="ER-101")
    bed = Bed(id=uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_id=room.id, bed_code="B-101", is_occupied=False)
    db_session.add_all([ward, room, bed])
    db_session.commit()

    p_admit = Patient(
        id=uuid4(), hospital_id=hospital.id, uhid="UHID-ER-ADMIT", name="Patient For Admission",
        first_name="Patient", last_name="Admit", mobile="555-9099", age=45, gender="male"
    )
    db_session.add(p_admit)
    db_session.commit()

    enc_admit_id = client.post(
        "/api/emergency/encounters",
        json={"patient_id": str(p_admit.id), "presenting_complaints": "Acute abdominal pain, query appendicitis"},
        headers=staff_auth,
    ).json()["id"]

    admit_disp_res = client.post(
        f"/api/emergency/encounters/{enc_admit_id}/disposition",
        json={
            "disposition_type": "admit_ipd",
            "destination_ward_id": str(ward.id),
            "destination_bed_id": str(bed.id),
            "disposition_notes": "Admit under surgical unit for appendectomy workup",
        },
        headers=staff_auth,
    )
    assert admit_disp_res.status_code == 201
    admit_disp = admit_disp_res.json()
    assert admit_disp["disposition_type"] == "admit_ipd"
    assert admit_disp["admission_id"] is not None
    assert admit_disp["destination_bed_id"] == str(bed.id)
    assert admit_disp["destination_ward_id"] == str(ward.id)

    # Bed should now be marked occupied
    db_session.refresh(bed)
    assert bed.is_occupied is True


def test_emergency_cross_hospital_isolation(client: TestClient, db_session: Session, hospital: Hospital, hospital_b: Hospital, patient: Patient, patient_b: Patient, staff_auth: dict[str, str], staff_auth_b: dict[str, str]):
    """Test Tenant Isolation: Hospital A cannot see or mutate Hospital B emergency data."""
    Base.metadata.create_all(db_session.bind)

    # Create encounter in Hospital A
    enc_a = client.post(
        "/api/emergency/encounters",
        json={"patient_id": str(patient.id), "presenting_complaints": "Hospital A patient emergency"},
        headers=staff_auth,
    ).json()["id"]

    # Hospital B staff attempts to access Hospital A encounter
    get_res = client.get(f"/api/emergency/encounters/{enc_a}", headers=staff_auth_b)
    assert get_res.status_code == 404

    # Hospital B queue should be empty
    queue_b = client.get("/api/emergency/queue", headers=staff_auth_b).json()
    assert not any(q["encounter_id"] == enc_a for q in queue_b)
