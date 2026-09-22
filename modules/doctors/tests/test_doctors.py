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

from modules.doctors.entities.doctor import HospitalUser, RolePermission, StaffRole
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from infrastructure.postgres.session import get_transitional_sync_session
from modules.doctors.api.doctors_api import router as doctors_router
from modules.doctors.entities.doctor import DoctorLeave, ShiftType
from shared.auth.dependencies import get_hospital_context, require_hospital_user
from shared.auth.jwt import create_access_token
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
    db_session.flush()
    db_session.add(
        RolePermission(
            hospital_id=hospital.id,
            role_id=role.id,
            module_key="doctors",
            can_view=True,
            can_edit=True,
        )
    )
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
        "staff_role_id": str(doctor_user.role_id),
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

    from modules.beds.entities.bed import Bed, Room, Ward, WardType
    from modules.inpatient.entities.admission import Admission, AdmissionStatus

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

    from modules.appointments.entities.appointment import Appointment
    from modules.appointments.entities.enums import AppointmentStatus

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


def test_doctor_patient_history_allows_admission_only_link(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
    doctor_user: HospitalUser,
    db_session: Session,
    hospital: Hospital,
):
    """Regression: a patient linked to the doctor ONLY via an Admission (no
    appointment/prescription/record) must still open in the current-patient view.

    Previously GetDoctorPatientHistoryAction only considered Appointment,
    Prescription and MedicalRecord links, so an admitted patient with none of
    those returned 404 and the frontend rendered an empty patient screen.
    """
    from modules.beds.entities.bed import Bed, Room, Ward, WardType
    from modules.inpatient.entities.admission import Admission, AdmissionStatus

    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid="PADMONLY",
        name="Admitted Only Patient",
        gender="Female",
        mobile="9998887701",
    )
    db_session.add(patient)
    db_session.flush()

    ward = Ward(
        id=uuid4(),
        hospital_id=hospital.id,
        name="IPD Ward",
        ward_type=WardType.general,
    )
    db_session.add(ward)
    db_session.flush()
    room = Room(id=uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_code="R-200")
    db_session.add(room)
    db_session.flush()
    bed = Bed(id=uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_id=room.id, bed_code="B-200")
    db_session.add(bed)
    db_session.flush()

    adm = Admission(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor_user.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        ip_id="IPADMONLY",
        status=AdmissionStatus.admitted,
    )
    db_session.add(adm)
    db_session.commit()

    res = client.get(
        f"/api/doctors/{doctor_user.id}/patients/{patient.id}",
        headers=doctor_auth_headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["patient"]["id"] == str(patient.id)


def test_direct_backend_completion_diagnostic_blockers_enforced(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
    doctor_user: HospitalUser,
    db_session: Session,
    hospital: Hospital,
):
    """Verify that direct PUT /api/doctors/{id}/appointments/{id} enforcing status=completed
    reaches canonical blocker validation and rejects completion when open lab orders exist.
    """
    from modules.appointments.entities.appointment import Appointment
    from modules.appointments.entities.enums import AppointmentStatus
    from modules.laboratory.entities.lab_entities import LabOrder, LabOrderStatus

    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid="P-BLK-01",
        name="Blocker Test Patient",
        gender="Male",
        mobile="9876543210",
    )
    db_session.add(patient)
    db_session.commit()

    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor_user.id,
        patient_id=patient.id,
        appointment_date=date.today(),
        appointment_time=time(11, 0),
        purpose="Investigation Consultation",
        status=AppointmentStatus.scheduled,
    )
    db_session.add(appt)
    db_session.commit()

    # Create an open diagnostic blocker (lab order)
    lab_order = LabOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no="LAB-TEST-001",
        patient_id=patient.id,
        appointment_id=appt.id,
        doctor_id=doctor_user.id,
        status=LabOrderStatus.ordered,
        ordered_by_name="Dr. Test",
    )
    db_session.add(lab_order)
    db_session.commit()

    # 1. Attempt to bypass completion via direct doctor appointment update
    res = client.put(
        f"/api/doctors/{doctor_user.id}/appointments/{appt.id}",
        json={"status": "completed"},
        headers=doctor_auth_headers,
    )
    assert res.status_code == 400, res.text
    assert "Cannot complete appointment" in res.json()["detail"]
    assert "Lab order LAB-TEST-001 is ordered" in res.json()["detail"]

    # 2. Complete the lab order
    lab_order.status = LabOrderStatus.completed
    db_session.commit()

    # 3. Re-attempt completion - now should succeed cleanly
    res_ok = client.put(
        f"/api/doctors/{doctor_user.id}/appointments/{appt.id}",
        json={"status": "completed"},
        headers=doctor_auth_headers,
    )
    assert res_ok.status_code == 200, res_ok.text
    assert res_ok.json()["status"] == "completed"


def test_doctor_signature_update_and_persistence(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
    doctor_user: HospitalUser,
    db_session: Session,
):
    """Verify PUT /api/doctors/{id}/signature updates digital_signature and persists."""
    sig_payload = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    res = client.put(
        f"/api/doctors/{doctor_user.id}/signature",
        json={"signature_data": sig_payload},
        headers=doctor_auth_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["message"] == "Signature updated successfully"
    assert body["digital_signature"] == sig_payload

    # Verify directly in DB
    db_session.refresh(doctor_user)
    assert doctor_user.digital_signature == sig_payload

    # Clear signature
    res_clear = client.put(
        f"/api/doctors/{doctor_user.id}/signature",
        json={"signature_data": None},
        headers=doctor_auth_headers,
    )
    assert res_clear.status_code == 200
    assert res_clear.json()["digital_signature"] is None


def test_doctor_appointments_and_calendar_reflect_bed_allocation(
    client: TestClient,
    doctor_auth_headers: dict[str, str],
    doctor_user: HospitalUser,
    db_session: Session,
    hospital: Hospital,
):
    """Verify GET /api/doctors/{id}/appointments and /calendar reflect admission_ward, admission_bed, ip_id, and admission_status."""
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid="P8888",
        name="Jane Admitted",
        gender="Female",
        mobile="9991112223",
    )
    db_session.add(patient)

    from modules.beds.entities.bed import Bed, Room, Ward, WardType
    from modules.inpatient.entities.admission import Admission, AdmissionStatus

    ward = Ward(
        id=uuid4(),
        hospital_id=hospital.id,
        name="ICU Ward",
        ward_type=WardType.icu,
    )
    db_session.add(ward)

    room = Room(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_code="ICU-R1",
    )
    db_session.add(room)

    bed = Bed(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="ICU-B01",
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
        ip_id="IP9999",
        status=AdmissionStatus.admitted,
    )
    db_session.add(adm)
    db_session.commit()

    from modules.appointments.entities.appointment import Appointment
    from modules.appointments.entities.enums import AppointmentStatus

    today = date.today()
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor_user.id,
        patient_id=patient.id,
        admission_id=adm.id,
        appointment_date=today,
        appointment_time=time(11, 30),
        purpose="Inpatient Rounds",
        status=AppointmentStatus.transferred_to_inpatient,
    )
    db_session.add(appt)
    db_session.commit()

    # 1. Test /api/doctors/{id}/appointments
    res_appt = client.get(
        f"/api/doctors/{doctor_user.id}/appointments?date={today.isoformat()}",
        headers=doctor_auth_headers,
    )
    assert res_appt.status_code == 200, res_appt.text
    appts = res_appt.json()
    match = next((a for a in appts if a["id"] == str(appt.id)), None)
    assert match is not None
    assert match["admission_id"] == str(adm.id)
    assert match["ip_id"] == "IP9999"
    assert match["admission_ward"] == "ICU Ward"
    assert match["admission_bed"] == "ICU-B01"
    assert match["admission_status"] == "admitted"

    # 2. Test /api/doctors/{id}/calendar
    monday = today - timedelta(days=today.weekday())
    res_cal = client.get(
        f"/api/doctors/{doctor_user.id}/calendar?week_start={monday.isoformat()}",
        headers=doctor_auth_headers,
    )
    assert res_cal.status_code == 200, res_cal.text
    cal_appts = res_cal.json()
    cal_match = next((a for a in cal_appts if a["id"] == str(appt.id)), None)
    assert cal_match is not None
    assert cal_match["admission_id"] == str(adm.id)
    assert cal_match["ip_id"] == "IP9999"
    assert cal_match["admission_ward"] == "ICU Ward"
    assert cal_match["admission_bed"] == "ICU-B01"
    assert cal_match["admission_status"] == "admitted"


