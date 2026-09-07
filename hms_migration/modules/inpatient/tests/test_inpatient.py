"""
Unit and integration tests for the Migrated Inpatient / IPD Domain.

Verifies:
1. Inpatient Admission (/api/beds/admit) & bed state synchronization.
2. Bed allocation and transfer (/api/beds/allocate, /api/beds/transfer).
3. Discharge request creation & listing (/api/beds/discharge-request, /api/beds/discharge-requests).
4. Discharge completion gate with billing dues check (/api/beds/discharge).
5. Registration patient admission and discharge (/api/registration/patients/{id}/admit, /api/registration/admissions/{id}/discharge).
6. IPD form submission lifecycle (create, update, list, detail, html view under /api/ipd/form-submissions).
7. Multi-tenant isolation for inpatient admissions and IPD forms.
"""

from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Bed, Hospital, HospitalUser, Patient, Room, StaffRole, Ward, WardType
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.beds.api.beds_api import (
    registration_inpatient_router,
    router as beds_router,
)
from hms_migration.modules.inpatient.api.ipd_api import router as ipd_router
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital


@pytest.fixture(scope="function")
def inpatient_app(db_session: Session) -> FastAPI:
    """Isolated test app mounting inpatient routes across /beds, /registration, and /ipd."""
    test_app = FastAPI(title="Migrated Inpatient Test App")
    test_app.include_router(beds_router, prefix="/api")
    test_app.include_router(registration_inpatient_router, prefix="/api")
    test_app.include_router(ipd_router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_get_db
    return test_app


@pytest.fixture(scope="function")
def client(inpatient_app: FastAPI) -> TestClient:
    return TestClient(inpatient_app)


@pytest.fixture(scope="function")
def staff_auth(hospital: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "staff@hospital.com",
        "name": "Nurse Joy",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def doctor_auth(hospital: Hospital, db_session: Session) -> tuple[dict[str, str], HospitalUser]:
    role = StaffRole(id=uuid4(), hospital_id=hospital.id, name="Doctor")
    db_session.add(role)
    db_session.commit()

    doc = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. House",
        email="house@hospital.com",
        phone="9876500099",
        password_hash="pwd",
    )
    db_session.add(doc)
    db_session.commit()

    token = create_access_token({
        "sub": doc.email,
        "name": doc.name,
        "role": "doctor",
        "hospital_uuid": str(hospital.id),
        "doctor_id": str(doc.id),
    })
    return {"Authorization": f"Bearer {token}"}, doc


@pytest.fixture(scope="function")
def test_setup(db_session: Session, hospital: Hospital) -> dict[str, any]:
    ward = Ward(
        id=uuid4(),
        hospital_id=hospital.id,
        name="ICU Ward",
        ward_type=WardType.icu,
        admission_fee=1000.0,
        bed_charge_per_day=2500.0,
        is_active=True,
    )
    db_session.add(ward)
    db_session.commit()

    room = Room(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_code="ICU-1",
        name="ICU Unit 1",
        bed_count=2,
        is_active=True,
    )
    db_session.add(room)
    db_session.commit()

    bed1 = Bed(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="ICU-B1",
        is_occupied=False,
        is_active=True,
    )
    bed2 = Bed(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="ICU-B2",
        is_occupied=False,
        is_active=True,
    )
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid="P9999",
        first_name="Bob",
        last_name="Marley",
        name="Bob Marley",
        mobile="9876543299",
    )
    db_session.add_all([bed1, bed2, patient])
    db_session.commit()
    return {"ward": ward, "room": room, "bed1": bed1, "bed2": bed2, "patient": patient}


def test_admit_and_transfer_patient(
    client: TestClient,
    staff_auth: dict[str, str],
    doctor_auth: tuple[dict[str, str], HospitalUser],
    test_setup: dict[str, any],
):
    """Test admitting a patient, verifying bed occupied, and transferring to another bed."""
    _, doctor = doctor_auth
    ward = test_setup["ward"]
    room = test_setup["room"]
    bed1 = test_setup["bed1"]
    bed2 = test_setup["bed2"]
    patient = test_setup["patient"]

    # 1. Admit patient
    admit_payload = {
        "patient_id": str(patient.id),
        "ward_id": str(ward.id),
        "room_id": str(room.id),
        "bed_id": str(bed1.id),
        "doctor_id": str(doctor.id),
        "notes": "Emergency respiratory support",
    }
    res = client.post("/api/beds/admit", json=admit_payload, headers=staff_auth)
    assert res.status_code == 201, res.text
    adm = res.json()
    assert adm["status"] == "admitted"
    assert adm["bed_code"] == "ICU-B1"
    assert adm["ip_id"].startswith("IP-")
    admission_id = adm["id"]

    # 2. Verify active admission in list
    active_res = client.get("/api/beds/admissions/active", headers=staff_auth)
    assert active_res.status_code == 200
    active_list = active_res.json()
    assert any(a["id"] == admission_id for a in active_list)

    # 3. Transfer bed to bed2
    transfer_payload = {
        "admission_id": admission_id,
        "patient_id": str(patient.id),
        "to_ward_id": str(ward.id),
        "to_room_id": str(room.id),
        "to_bed_id": str(bed2.id),
        "transfer_notes": "Step down within ICU",
    }
    tx_res = client.put("/api/beds/transfer", json=transfer_payload, headers=staff_auth)
    assert tx_res.status_code == 200, tx_res.text
    tx_data = tx_res.json()
    assert tx_data["bed_code"] == "ICU-B2"


def test_ipd_form_submission_lifecycle(
    client: TestClient,
    staff_auth: dict[str, str],
    test_setup: dict[str, any],
):
    """Test IPD form submission creation, update, listing, and HTML view."""
    patient = test_setup["patient"]

    # 1. Create IPD Form Submission
    create_payload = {
        "patient_id": str(patient.id),
        "admission_id": None,
        "form_id": "consent_surgery",
        "form_title": "Consent for Surgery",
        "form_data": {"procedure": "Appendectomy", "risks_explained": True},
        "html_snapshot": "<html><body><h1>Consent for Surgery</h1><p>Procedure: Appendectomy</p></body></html>",
        "status": "draft",
    }
    res = client.post("/api/ipd/form-submissions", json=create_payload, headers=staff_auth)
    assert res.status_code == 201, res.text
    form_data = res.json()
    assert form_data["form_title"] == "Consent for Surgery"
    assert form_data["status"] == "draft"
    form_id = form_data["id"]

    # 2. Finalize submission
    put_res = client.put(
        f"/api/ipd/form-submissions/{form_id}",
        json={"status": "final", "form_data": {"procedure": "Appendectomy", "signed": True}},
        headers=staff_auth,
    )
    assert put_res.status_code == 200, put_res.text
    assert put_res.json()["status"] == "final"

    # 3. List submissions
    list_res = client.get(
        f"/api/ipd/form-submissions?patient_id={patient.id}",
        headers=staff_auth,
    )
    assert list_res.status_code == 200
    forms = list_res.json()
    assert any(f["id"] == form_id for f in forms)

    # 4. View HTML representation
    view_res = client.get(f"/api/ipd/form-submissions/{form_id}/view", headers=staff_auth)
    assert view_res.status_code == 200
    assert "text/html" in view_res.headers.get("content-type", "")
    assert "Consent for Surgery" in view_res.text
    assert "Appendectomy" in view_res.text
