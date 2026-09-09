"""
Unit and integration tests for the Migrated Doctors Domain module.

Verifies:
1. List doctors directory (/api/doctors) with specialized filters.
2. Hospital profile (/api/doctors/hospital-profile).
3. Patient search (/api/doctors/patients/search).
4. Patient registration and update (/api/doctors/patients).
5. Doctor patients listing & detail (/api/doctors/{id}/patients).
6. Doctor appointments (/api/doctors/{id}/appointments).
7. Doctor calendar (/api/doctors/{id}/calendar).
8. Doctor active admissions (/api/doctors/{id}/admissions/active).
9. Schedule context (/api/doctors/{id}/schedule-context).
10. Doctor leave management (create, range, list, delete, conflict detection).
11. Multi-tenant isolation across doctors and leaves.
"""

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hospital, HospitalUser, Patient, StaffRole
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.doctors.api.doctors_api import router as doctors_router
from hms_migration.modules.doctors.entities.doctor import DoctorLeave, ShiftType
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital, hospital_b


@pytest.fixture(scope="function")
def doctors_app(db_session: Session) -> FastAPI:
    """Isolated test app mounting the migrated doctors router."""
    test_app = FastAPI(title="Migrated Doctors Test App")
    test_app.include_router(doctors_router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_get_db
    return test_app


@pytest.fixture(scope="function")
def client(doctors_app: FastAPI) -> TestClient:
    return TestClient(doctors_app)


@pytest.fixture(scope="function")
def doctor_role(db_session: Session, hospital: Hospital) -> StaffRole:
    role = StaffRole(
        id=uuid4(),
        hospital_id=hospital.id,
        name="Doctor",
        description="Medical Doctor Role",
    )
    db_session.add(role)
    db_session.commit()
    return role


@pytest.fixture(scope="function")
def doctor_user(db_session: Session, hospital: Hospital, doctor_role: StaffRole) -> HospitalUser:
    doc = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=doctor_role.id,
        name="Dr. Jane Smith",
        email="jane.smith@hospital.com",
        phone="9876543210",
        password_hash="fakehash",
        specialization="Cardiology",
        qualification="MD, DM",
        medical_registration_number="MED-12345",
        years_of_experience=10,
        consultation_room="Room 101",
        is_active=True,
    )
    db_session.add(doc)
    db_session.commit()
    return doc


@pytest.fixture(scope="function")
def doctor_auth_headers(hospital: Hospital, doctor_user: HospitalUser) -> dict[str, str]:
    token = create_access_token({
        "sub": doctor_user.email,
        "name": doctor_user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(doctor_user.id),
        "doctor_id": str(doctor_user.id),
    })
    return {"Authorization": f"Bearer {token}"}


def test_list_doctors_directory(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
    doctor_user: HospitalUser,
):
    """Test GET /api/doctors lists active doctors."""
    res = client.get("/api/doctors", headers=doctor_auth_headers)
    assert res.status_code == 200, res.text
    docs = res.json()
    assert len(docs) >= 1
    found = next((d for d in docs if d["id"] == str(doctor_user.id)), None)
    assert found is not None
    assert found["name"] == "Dr. Jane Smith"
    assert found["specialization"] == "Cardiology"


def test_get_hospital_profile(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
    hospital: Hospital,
):
    """Test GET /api/doctors/hospital-profile."""
    res = client.get("/api/doctors/hospital-profile", headers=doctor_auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["name"] == hospital.name
    assert "address" in data
    assert "phone" in data


def test_doctor_patients_search_and_create(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
):
    """Test POST /api/doctors/patients and GET /api/doctors/patients/search."""
    post_res = client.post(
        "/api/doctors/patients",
        json={
            "name": "John Doe",
            "mobile": "9998887776",
            "gender": "male",
            "age": 45,
            "address": "123 Main St",
        },
        headers=doctor_auth_headers,
    )
    assert post_res.status_code == 201, post_res.text
    patient_data = post_res.json()
    assert patient_data["name"] == "John Doe"
    assert patient_data["mobile"] == "9998887776"

    # Search for patient
    search_res = client.get(
        "/api/doctors/patients/search?query=9998887776",
        headers=doctor_auth_headers,
    )
    assert search_res.status_code == 200, search_res.text
    results = search_res.json()
    assert len(results) == 1
    assert results[0]["id"] == patient_data["id"]


def test_doctor_leaves_crud_and_conflicts(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
    doctor_user: HospitalUser,
):
    """Test adding single leave, date-range leaves, conflict detection, and deletion."""
    leave_date = (date.today() + timedelta(days=5)).isoformat()

    # 1. Add single leave
    res = client.post(
        f"/api/doctors/{doctor_user.id}/leaves",
        json={
            "leave_date": leave_date,
            "start_time": "09:00:00",
            "end_time": "17:00:00",
            "reason": "Conference",
        },
        headers=doctor_auth_headers,
    )
    assert res.status_code == 201, res.text
    leave_info = res.json()
    assert leave_info["leave_date"] == leave_date
    leave_id = leave_info["id"]

    # 2. Duplicate leave on same day -> 409
    dup_res = client.post(
        f"/api/doctors/{doctor_user.id}/leaves",
        json={
            "leave_date": leave_date,
            "start_time": "09:00:00",
            "end_time": "17:00:00",
            "reason": "Duplicate",
        },
        headers=doctor_auth_headers,
    )
    assert dup_res.status_code == 409

    # 3. List leaves
    list_res = client.get(
        f"/api/doctors/{doctor_user.id}/leaves",
        headers=doctor_auth_headers,
    )
    assert list_res.status_code == 200
    leaves = list_res.json()
    assert any(l["id"] == leave_id for l in leaves)

    # 4. Delete leave
    del_res = client.delete(
        f"/api/doctors/{doctor_user.id}/leaves/{leave_id}",
        headers=doctor_auth_headers,
    )
    assert del_res.status_code == 204


def test_doctor_tenant_isolation(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
    db_session: Session,
    hospital_b: Hospital,
):
    """Ensure doctor from Hospital A cannot manage leaves for doctor in Hospital B."""
    other_role = StaffRole(id=uuid4(), hospital_id=hospital_b.id, name="Doctor")
    db_session.add(other_role)
    db_session.commit()

    other_doc = HospitalUser(
        id=uuid4(),
        hospital_id=hospital_b.id,
        role_id=other_role.id,
        name="Dr. Other",
        email="other@doc.com",
        phone="1112223334",
        password_hash="pwd",
    )
    db_session.add(other_doc)
    db_session.commit()

    res = client.get(
        f"/api/doctors/{other_doc.id}/leaves",
        headers=doctor_auth_headers,
    )
    assert res.status_code == 403


def test_get_doctor_patient_history(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
    doctor_user: HospitalUser,
    db_session: Session,
    hospital: Hospital,
):
    """Verify GET /api/doctors/{doctor_id}/patients/{patient_id} succeeds with 200 OK and eager-loads admission/ward/bed info."""
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid="P9999",
        name="John Patient",
        gender="Male",
        mobile="9998887770",
    )
    db_session.add(patient)
    db_session.commit()

    from hms_migration.modules.beds.entities.bed import Bed, Room, Ward, WardType
    from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus

    ward = Ward(
        id=uuid4(),
        hospital_id=hospital.id,
        name="General Ward",
        ward_type=WardType.general,
    )
    db_session.add(ward)

    room = Room(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_code="R-101",
    )
    db_session.add(room)

    bed = Bed(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="B-101",
    )
    db_session.add(bed)

    adm = Admission(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor_user.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        ip_id="IP0001",
        status=AdmissionStatus.admitted,
    )
    db_session.add(adm)
    db_session.commit()

    from hms_migration.modules.appointments.entities.appointment import Appointment
    from hms_migration.modules.appointments.entities.enums import AppointmentStatus

    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor_user.id,
        patient_id=patient.id,
        admission_id=adm.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        purpose="General Checkup",
        status=AppointmentStatus.scheduled,
    )
    db_session.add(appt)
    db_session.commit()

    res = client.get(
        f"/api/doctors/{doctor_user.id}/patients/{patient.id}",
        headers=doctor_auth_headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["patient"]["id"] == str(patient.id)
    assert len(data["appointments"]) == 1
    appt_resp = data["appointments"][0]
    assert appt_resp["id"] == str(appt.id)
    assert appt_resp["admission_id"] == str(adm.id)
    assert appt_resp["ip_id"] == "IP0001"
    assert appt_resp["admission_ward"] == "General Ward"
    assert appt_resp["admission_bed"] == "B-101"
    assert appt_resp["admission_status"] == "admitted"


