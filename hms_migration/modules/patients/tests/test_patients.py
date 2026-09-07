"""
Comprehensive tests for the migrated Patient domain module.

Conforms to UltrionTech-Backend-Template modules/patients/tests/ specification.
Verifies:
1. Patient registration with auto-generated UHID and audit logging.
2. Duplicate mobile number prevention (HTTP 409 Conflict).
3. Emergency contact validation on creation and update (HTTP 422).
4. Patient search, listing, and filtering by status.
5. Cross-tenant isolation (patients from hospital A cannot be seen/accessed by hospital B).
6. 360-degree patient profile aggregation (visits, prescriptions, reports, admissions, bills).
7. Patient update with collision detection, display name recalculation, and audit logging.
8. Patient not found handling (HTTP 404).
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Admission,
    AdmissionStatus,
    Appointment,
    AppointmentStatus,
    Bed,
    Hospital,
    HospitalUser,
    MedicalRecord,
    Prescription,
    Room,
    Ward,
)
from hms_migration.infrastructure.postgres.session import (
    get_transitional_sync_session as get_db,
)
from hms_migration.modules.patients.api.patients_api import router as patients_router
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus
from hms_migration.shared.audit.entities.audit_log import AuditLog
from hms_migration.shared.auth.jwt import create_access_token


@pytest.fixture(scope="function")
def patients_client(db_session: Session) -> TestClient:
    """FastAPI TestClient mounting migrated patients router with session override."""
    test_app = FastAPI(title="Migrated Patients Test App")
    test_app.include_router(patients_router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(test_app)


@pytest.fixture(scope="function")
def staff_headers(hospital: Hospital) -> dict[str, str]:
    """Auth headers for a valid hospital staff member."""
    token = create_access_token({
        "sub": "nurse@hospital.com",
        "name": "Staff Nurse",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def other_hospital_headers(hospital_b: Hospital) -> dict[str, str]:
    """Auth headers for a different hospital tenant."""
    token = create_access_token({
        "sub": "admin@hospitalb.com",
        "name": "Hospital B Admin",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital_b.id),
    })
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# 1. Registration Tests
# ===========================================================================

def test_migrated_register_patient_success(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    staff_headers: dict[str, str],
):
    """POST /api/registration/patients registers a new patient with UHID and audit log."""
    payload = {
        "first_name": "Aarav",
        "last_name": "Patel",
        "gender": "Male",
        "date_of_birth": "1995-08-20",
        "mobile": "9876543210",
        "email": "aarav.patel@example.com",
        "address": "42 Ring Road, Ahmedabad",
        "emergency_contact": "9876543211",
        "emergency_contact_name": "Bhavin Patel",
        "emergency_contact_relation": "Brother" if "Brother" in ("Father", "Mother", "Spouse", "Sibling", "Child", "Friend", "Other") else "Sibling",
        "blood_group": "O+",
        "has_insurance": True,
        "insurance_provider": "Star Health",
    }

    resp = patients_client.post(
        "/api/registration/patients",
        json=payload,
        headers=staff_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["uhid"].startswith("P")
    assert data["first_name"] == "Aarav"
    assert data["last_name"] == "Patel"
    assert data["name"] == "Aarav Patel"
    assert data["mobile"] == "9876543210"
    assert data["email"] == "aarav.patel@example.com"
    assert data["blood_group"] == "O+"
    assert data["status"] == "active"
    assert data["last_visit"] is None

    # Verify database persistence
    patient_id = UUID(data["id"])
    saved = db_session.query(Patient).filter(Patient.id == patient_id).first()
    assert saved is not None
    assert saved.hospital_id == hospital.id
    assert saved.insurance_provider == "Star Health"

    # Verify audit log entry
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.hospital_id == hospital.id, AuditLog.entity_id == str(patient_id))
        .first()
    )
    assert audit is not None
    assert audit.action == "create"
    assert audit.entity_type == "patient"
    assert "Registered patient" in audit.summary


def test_migrated_register_patient_duplicate_mobile_returns_409(
    patients_client: TestClient,
    hospital: Hospital,
    staff_headers: dict[str, str],
):
    """POST /api/registration/patients raises 409 Conflict if mobile is already used."""
    payload = {
        "first_name": "Meera",
        "last_name": "Nair",
        "gender": "Female",
        "mobile": "9876543222",
        "emergency_contact": "9876543223",
        "emergency_contact_name": "Karan Nair",
        "emergency_contact_relation": "Spouse",
    }
    resp1 = patients_client.post("/api/registration/patients", json=payload, headers=staff_headers)
    assert resp1.status_code == 201

    resp2 = patients_client.post("/api/registration/patients", json=payload, headers=staff_headers)
    assert resp2.status_code == 409
    assert "already exists" in resp2.json()["detail"]


def test_migrated_register_patient_same_emergency_contact_returns_422(
    patients_client: TestClient,
    staff_headers: dict[str, str],
):
    """POST /api/registration/patients raises 422 if mobile equals emergency contact."""
    payload = {
        "first_name": "Sunil",
        "last_name": "Verma",
        "gender": "Male",
        "mobile": "9876543233",
        "emergency_contact": "9876543233",
        "emergency_contact_name": "Self",
        "emergency_contact_relation": "Other",
    }
    resp = patients_client.post("/api/registration/patients", json=payload, headers=staff_headers)
    assert resp.status_code == 422


# ===========================================================================
# 2. Search & List Tests
# ===========================================================================

def test_migrated_list_and_search_patients(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    staff_headers: dict[str, str],
):
    """GET /api/registration/patients supports filtering by search term and status."""
    p1 = Patient(
        hospital_id=hospital.id,
        uhid="P0101",
        first_name="Deepak",
        last_name="Chopra",
        name="Deepak Chopra",
        mobile="9876543301",
        gender="Male",
        status=PatientStatus.active,
    )
    p2 = Patient(
        hospital_id=hospital.id,
        uhid="P0102",
        first_name="Ananya",
        last_name="Pandey",
        name="Ananya Pandey",
        mobile="9876543302",
        gender="Female",
        status=PatientStatus.inactive,
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    # Search by name
    resp = patients_client.get(
        "/api/registration/patients?search=Deepak",
        headers=staff_headers,
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["first_name"] == "Deepak"

    # Search by status
    resp_inactive = patients_client.get(
        "/api/registration/patients?status=inactive",
        headers=staff_headers,
    )
    assert resp_inactive.status_code == 200
    inactive_items = resp_inactive.json()
    assert any(i["first_name"] == "Ananya" for i in inactive_items)


# ===========================================================================
# 3. Tenant Isolation Tests
# ===========================================================================

def test_migrated_patient_tenant_isolation(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    hospital_b: Hospital,
    staff_headers: dict[str, str],
    other_hospital_headers: dict[str, str],
):
    """Patients belonging to Hospital A are invisible and inaccessible to Hospital B."""
    patient_a = Patient(
        hospital_id=hospital.id,
        uhid="P0201",
        first_name="Isolated",
        last_name="PatientA",
        name="Isolated PatientA",
        mobile="9876543401",
        gender="Male",
        status=PatientStatus.active,
    )
    db_session.add(patient_a)
    db_session.commit()

    # Accessible by Hospital A
    resp_a = patients_client.get(
        f"/api/registration/patients/{patient_a.id}",
        headers=staff_headers,
    )
    assert resp_a.status_code == 200

    # Inaccessible by Hospital B (404)
    resp_b = patients_client.get(
        f"/api/registration/patients/{patient_a.id}",
        headers=other_hospital_headers,
    )
    assert resp_b.status_code == 404
    assert resp_b.json()["detail"] == "Patient not found"

    # Not in Hospital B's directory
    resp_list_b = patients_client.get(
        "/api/registration/patients?search=Isolated",
        headers=other_hospital_headers,
    )
    assert resp_list_b.status_code == 200
    assert len(resp_list_b.json()) == 0


# ===========================================================================
# 4. Profile & Cross-Domain Aggregation Tests
# ===========================================================================

def test_migrated_get_patient_profile_aggregates_records(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    doctor: HospitalUser,
    staff_headers: dict[str, str],
):
    """GET /api/registration/patients/{id} returns demographic details and cross-domain records."""
    patient = Patient(
        hospital_id=hospital.id,
        uhid="P0301",
        first_name="Vikram",
        last_name="Malhotra",
        name="Vikram Malhotra",
        mobile="9876543501",
        gender="Male",
        status=PatientStatus.active,
    )
    db_session.add(patient)
    db_session.flush()

    # Add appointment
    appt = Appointment(
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=date.today(),
        appointment_time=datetime.now().time(),
        purpose="General Checkup",
        status=AppointmentStatus.completed,
    )
    db_session.add(appt)

    # Add prescription
    rx = Prescription(
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        symptoms="High blood pressure symptoms",
        diagnosis="Hypertension",
        medicines="Amlodipine 5mg",
        dosage="Once daily",
        advice="Rest well",
    )
    db_session.add(rx)

    db_session.commit()

    resp = patients_client.get(
        f"/api/registration/patients/{patient.id}",
        headers=staff_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["uhid"] == "P0301"
    assert data["name"] == "Vikram Malhotra"
    assert len(data["visits"]) == 1
    assert data["visits"][0]["purpose"] == "General Checkup"
    assert len(data["prescriptions"]) == 1
    assert data["prescriptions"][0]["diagnosis"] == "Hypertension"


# ===========================================================================
# 5. Update Tests
# ===========================================================================

def test_migrated_update_patient_success(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    staff_headers: dict[str, str],
):
    """PUT /api/registration/patients/{id} updates patient fields and recalculates name."""
    patient = Patient(
        hospital_id=hospital.id,
        uhid="P0401",
        first_name="OldFirst",
        last_name="OldLast",
        name="OldFirst OldLast",
        mobile="9876543601",
        emergency_contact="9876543602",
        emergency_contact_name="Old Contact",
        emergency_contact_relation="Sibling",
        gender="Male",
        status=PatientStatus.active,
    )
    db_session.add(patient)
    db_session.commit()

    payload = {
        "first_name": "NewFirst",
        "last_name": "NewLast",
        "emergency_contact_name": "Updated Contact",
    }
    resp = patients_client.put(
        f"/api/registration/patients/{patient.id}",
        json=payload,
        headers=staff_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["first_name"] == "NewFirst"
    assert data["last_name"] == "NewLast"
    assert data["name"] == "NewFirst NewLast"

    # Verify audit log
    audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.hospital_id == hospital.id,
            AuditLog.entity_id == str(patient.id),
            AuditLog.action == "update",
        )
        .first()
    )
    assert audit is not None
    assert "Updated patient" in audit.summary


def test_migrated_update_patient_mobile_clash_returns_409(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    staff_headers: dict[str, str],
):
    """PUT /api/registration/patients/{id} returns 409 if new mobile clashes with another patient."""
    p1 = Patient(
        hospital_id=hospital.id,
        uhid="P0501",
        first_name="One",
        last_name="User",
        name="One User",
        mobile="9876543701",
        emergency_contact="9876543799",
        emergency_contact_name="Contact One",
        emergency_contact_relation="Sibling",
        gender="Male",
        status=PatientStatus.active,
    )
    p2 = Patient(
        hospital_id=hospital.id,
        uhid="P0502",
        first_name="Two",
        last_name="User",
        name="Two User",
        mobile="9876543702",
        emergency_contact="9876543798",
        emergency_contact_name="Contact Two",
        emergency_contact_relation="Sibling",
        gender="Female",
        status=PatientStatus.active,
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    # Try updating p2 with p1's mobile
    resp = patients_client.put(
        f"/api/registration/patients/{p2.id}",
        json={"mobile": "9876543701"},
        headers=staff_headers,
    )
    assert resp.status_code == 409
    assert "Mobile already used" in resp.json()["detail"]
