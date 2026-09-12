"""
Unit and integration tests for Feature 16: Active Patient Allergy Tracking & Interception.

Tests:
1. Add drug & food allergies to patient
2. List patient active allergies
3. Intercept unsafe medication (positive allergy alert)
4. Verify safe medication passes without alert
5. Deactivate allergy record
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.models import Hospital, Patient
from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.patients.api.allergy_api import router as allergy_router
from hms_migration.modules.patients.entities.allergy import PatientAllergy
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, doctor, hospital, patient


@pytest.fixture(scope="function")
def allergy_app(db_session: Session) -> FastAPI:
    test_app = FastAPI(title="Allergy Test App")
    test_app.include_router(allergy_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(allergy_app: FastAPI) -> TestClient:
    return TestClient(allergy_app)


@pytest.fixture(scope="function")
def staff_headers(hospital: Hospital) -> dict[str, str]:
    token = create_access_token(
        data={
            "sub": "doctor.sharma@hospital.test",
            "name": "Dr. Sharma",
            "email": "doctor.sharma@hospital.test",
            "role": "hospital_staff",
            "staff_role_name": "doctor",
            "hospital_uuid": str(hospital.id),
            "user_id": str(uuid.uuid4()),
        }
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Hospital-ID": str(hospital.id),
    }


def test_feature_16_patient_allergy_lifecycle_and_interception(
    client: TestClient, db_session: Session, staff_headers: dict[str, str], patient: Patient
):
    Base.metadata.create_all(bind=db_session.get_bind())

    # 1. Add Drug Allergy (Penicillin)
    add_payload = {
        "allergen": "Penicillin",
        "allergen_type": "drug",
        "severity": "severe",
        "reaction": "Anaphylaxis and respiratory distress",
        "diagnosed_date": "2024-03-15",
        "notes": "Experienced severe shock after IV ampicillin in 2024",
    }
    res = client.post(f"/api/patients/{patient.id}/allergies", json=add_payload, headers=staff_headers)
    assert res.status_code == 201, res.text
    allergy_data = res.json()
    assert allergy_data["allergen"] == "Penicillin"
    assert allergy_data["severity"] == "severe"
    assert allergy_data["is_active"] is True
    allergy_id = allergy_data["id"]

    # 2. List Allergies
    res = client.get(f"/api/patients/{patient.id}/allergies", headers=staff_headers)
    assert res.status_code == 200
    allergies = res.json()
    assert len(allergies) == 1
    assert allergies[0]["id"] == allergy_id

    # 3. Interception Check: Unsafe Medication (Cross-match with Penicillin)
    unsafe_check = {
        "medicine_name": "Penicillin V Potassium 250mg Tab",
        "generic_name": "Phenoxymethylpenicillin",
    }
    res = client.post(f"/api/patients/{patient.id}/check-allergy-alert", json=unsafe_check, headers=staff_headers)
    assert res.status_code == 200
    alert = res.json()
    assert alert["has_conflict"] is True
    assert alert["matched_allergen"] == "Penicillin"
    assert alert["severity"] == "severe"
    assert alert["requires_clinical_override"] is True
    assert "CRITICAL ALLERGY ALERT" in alert["message"]

    # 4. Interception Check: Safe Medication (Paracetamol)
    safe_check = {
        "medicine_name": "Paracetamol 650mg Tab",
        "generic_name": "Acetaminophen",
    }
    res = client.post(f"/api/patients/{patient.id}/check-allergy-alert", json=safe_check, headers=staff_headers)
    assert res.status_code == 200
    safe_alert = res.json()
    assert safe_alert["has_conflict"] is False

    # 5. Deactivate Allergy
    res = client.delete(f"/api/patients/allergies/{allergy_id}", headers=staff_headers)
    assert res.status_code == 200
    deactivated = res.json()
    assert deactivated["is_active"] is False

    # 6. Interception Check after deactivation (No conflict for active allergies)
    res = client.post(f"/api/patients/{patient.id}/check-allergy-alert", json=unsafe_check, headers=staff_headers)
    assert res.status_code == 200
    assert res.json()["has_conflict"] is False
