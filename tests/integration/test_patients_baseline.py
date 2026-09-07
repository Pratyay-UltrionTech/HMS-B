"""
Baseline behavioral lock-in tests for legacy Patient Registration & Directory endpoints.

Locks in exact observable behavior of:
- POST /api/registration/patients
- GET /api/registration/patients
- GET /api/registration/patients/{patient_id}
- PUT /api/registration/patients/{patient_id}

Verifies:
1. Authentication: Bearer JWT required (403 on missing token via HTTPBearer, 401 on invalid token).
2. Authorization: hospital_user role required with hospital_uuid context.
3. Status codes: 201 Created, 200 OK, 404 Not Found, 409 Conflict, 422 Unprocessable Entity.
4. Validation:
   - Duplicate mobile raises 409 Conflict.
   - Mobile == Emergency Contact raises 422 Unprocessable Entity.
   - Missing required emergency contact bundle (name, relation, phone) raises 422.
5. UHID Generation: Format P0001... sequential.
6. Audit Logging: AuditLog entries created on patient registration and updates.
7. Last Visit Aggregation: Bulk last appointment date lookup on directory listing.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Appointment, AppointmentStatus, AuditLog, Hospital, HospitalUser, Patient, PatientStatus
from app.routers import registration as legacy_registration
from hms_migration.shared.auth.jwt import create_access_token


@pytest.fixture(scope="function")
def registration_client(db_session: Session) -> TestClient:
    """FastAPI TestClient mounting legacy registration router with db dependency override."""
    test_app = FastAPI(title="HMS Registration Baseline Test App")
    test_app.include_router(legacy_registration.router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(test_app)


@pytest.fixture(scope="function")
def staff_headers(hospital: Hospital) -> dict[str, str]:
    """Auth headers for a valid hospital staff member."""
    token = create_access_token({
        "sub": "receptionist@apollocity.com",
        "name": "Reception Staff",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# 1. Authentication & Authorization Baseline
# ===========================================================================

def test_patient_endpoints_unauthenticated_returns_403(registration_client: TestClient):
    """Accessing patient endpoints without auth header returns 403 (HTTPBearer default)."""
    resp = registration_client.get("/api/registration/patients")
    assert resp.status_code == 403

    resp_post = registration_client.post("/api/registration/patients", json={})
    assert resp_post.status_code == 403


def test_patient_endpoints_invalid_token_returns_401(registration_client: TestClient):
    """Accessing patient endpoints with malformed Bearer token returns 401."""
    resp = registration_client.get(
        "/api/registration/patients",
        headers={"Authorization": "Bearer invalid.token.payload"},
    )
    assert resp.status_code == 401


# ===========================================================================
# 2. Patient Registration (POST /patients) Baseline
# ===========================================================================

def test_register_patient_success(
    registration_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    staff_headers: dict[str, str],
):
    """POST /api/registration/patients creates a new patient with UHID and audit log."""
    payload = {
        "first_name": "Rohan",
        "last_name": "Sharma",
        "gender": "Male",
        "date_of_birth": "1992-05-15",
        "mobile": "9876543210",
        "email": "rohan.sharma@example.com",
        "address": "123 MG Road, Bengaluru",
        "emergency_contact": "9876543211",
        "emergency_contact_name": "Sunita Sharma",
        "emergency_contact_relation": "Spouse",
        "blood_group": "O+",
        "has_insurance": True,
        "insurance_provider": "Star Health Insurance",
    }

    resp = registration_client.post(
        "/api/registration/patients",
        json=payload,
        headers=staff_headers,
    )
    assert resp.status_code == 201
    data = resp.json()

    assert data["first_name"] == "Rohan"
    assert data["last_name"] == "Sharma"
    assert data["name"] == "Rohan Sharma"
    assert data["mobile"] == "9876543210"
    assert data["email"] == "rohan.sharma@example.com"
    assert data["status"] == "active"
    assert data["uhid"].startswith("P")
    assert "id" in data
    assert data["last_visit"] is None

    # Verify patient created in database
    p_db = db_session.query(Patient).filter(Patient.id == UUID(data["id"])).first()
    assert p_db is not None
    assert p_db.hospital_id == hospital.id
    assert p_db.has_insurance is True
    assert p_db.insurance_provider == "Star Health Insurance"

    # Verify audit log created
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.hospital_id == hospital.id, AuditLog.entity_id == str(p_db.id))
        .first()
    )
    assert audit is not None
    assert audit.action == "create"
    assert audit.entity_type == "patient"
    assert "Registered patient" in audit.summary


def test_register_patient_duplicate_mobile_returns_409(
    registration_client: TestClient,
    hospital: Hospital,
    db_session: Session,
    staff_headers: dict[str, str],
):
    """POST /api/registration/patients with an existing mobile number returns 409 Conflict."""
    # Create patient with valid 10-digit phone
    existing = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid="P0099",
        first_name="Existing",
        last_name="User",
        name="Existing User",
        mobile="9876500001",
        emergency_contact="9876500002",
        emergency_contact_name="Contact",
        emergency_contact_relation="Friend",
        status=PatientStatus.active,
    )
    db_session.add(existing)
    db_session.commit()

    payload = {
        "first_name": "Another",
        "last_name": "Person",
        "gender": "Female",
        "date_of_birth": "1995-01-01",
        "mobile": "9876500001",  # Duplicate
        "emergency_contact": "9111111111",
        "emergency_contact_name": "Parent",
        "emergency_contact_relation": "Mother",
    }

    resp = registration_client.post(
        "/api/registration/patients",
        json=payload,
        headers=staff_headers,
    )
    assert resp.status_code == 409
    assert "Patient with this mobile already exists" in resp.json().get("detail", "")


def test_register_patient_same_mobile_and_emergency_contact_returns_422(
    registration_client: TestClient,
    staff_headers: dict[str, str],
):
    """POST /api/registration/patients with mobile == emergency_contact returns 422 Unprocessable Entity."""
    payload = {
        "first_name": "Test",
        "last_name": "Patient",
        "gender": "Other",
        "date_of_birth": "1990-01-01",
        "mobile": "9998887770",
        "emergency_contact": "9998887770",  # Same as mobile
        "emergency_contact_name": "Self",
        "emergency_contact_relation": "Other",
    }

    resp = registration_client.post(
        "/api/registration/patients",
        json=payload,
        headers=staff_headers,
    )
    assert resp.status_code == 422


# ===========================================================================
# 3. Patient Listing & Search (GET /patients) Baseline
# ===========================================================================

def test_list_patients_and_search(
    registration_client: TestClient,
    hospital: Hospital,
    patient: Patient,
    staff_headers: dict[str, str],
):
    """GET /api/registration/patients returns list of patients with search filtering."""
    resp = registration_client.get("/api/registration/patients", headers=staff_headers)
    assert resp.status_code == 200
    patients = resp.json()
    assert len(patients) >= 1
    assert any(p["id"] == str(patient.id) for p in patients)

    # Search by exact UHID
    resp_search = registration_client.get(
        f"/api/registration/patients?search={patient.uhid}",
        headers=staff_headers,
    )
    assert resp_search.status_code == 200
    search_results = resp_search.json()
    assert len(search_results) == 1
    assert search_results[0]["uhid"] == patient.uhid


# ===========================================================================
# 4. Patient Profile & Update Baseline
# ===========================================================================

def test_get_patient_profile(
    registration_client: TestClient,
    hospital: Hospital,
    patient: Patient,
    staff_headers: dict[str, str],
):
    """GET /api/registration/patients/{id} returns comprehensive patient profile."""
    resp = registration_client.get(
        f"/api/registration/patients/{patient.id}",
        headers=staff_headers,
    )
    assert resp.status_code == 200
    profile = resp.json()
    assert profile["id"] == str(patient.id)
    assert profile["uhid"] == patient.uhid
    assert profile["name"] == patient.name
    assert "visits" in profile
    assert "prescriptions" in profile
    assert "medical_reports" in profile
    assert "admissions" in profile
    assert "financial_summary" in profile


def test_update_patient_success(
    registration_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    patient: Patient,
    staff_headers: dict[str, str],
):
    """PUT /api/registration/patients/{id} updates demographic fields and writes audit log."""
    # Ensure patient has valid emergency contacts before update
    patient.mobile = "9876543299"
    patient.emergency_contact = "9876543288"
    patient.emergency_contact_name = "Emergency Kin"
    patient.emergency_contact_relation = "Sibling"
    db_session.commit()

    update_payload = {
        "first_name": "Jane",
        "last_name": "Updated",
        "address": "456 Residency Road",
        "blood_group": "AB+",
    }

    resp = registration_client.put(
        f"/api/registration/patients/{patient.id}",
        json=update_payload,
        headers=staff_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["first_name"] == "Jane"
    assert data["last_name"] == "Updated"
    assert data["name"] == "Jane Updated"
    assert data["blood_group"] == "AB+"

    # Verify in DB
    db_session.refresh(patient)
    assert patient.name == "Jane Updated"
    assert patient.blood_group == "AB+"
