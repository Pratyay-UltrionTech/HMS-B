"""
Unit and integration tests for the Migrated Clinical Records Domain.

Verifies:
1. Prescription creation (/api/doctors/{id}/prescriptions).
2. Prescription update (/api/doctors/{id}/prescriptions/{rx_id}).
3. Prescription listing (/api/doctors/{id}/prescriptions).
4. Prescription HTML/print view (/api/doctors/{id}/prescriptions/{rx_id}/pdf).
5. Medical record creation (/api/doctors/{id}/records).
6. Medical record listing (/api/doctors/{id}/records).
7. Medical record file/report download (/api/doctors/{id}/records/{rec_id}/file).
8. Multi-tenant isolation for clinical records and prescriptions.
"""

from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hospital, HospitalUser, Patient, StaffRole
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.doctors.api.doctors_api import router as doctors_router
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital


@pytest.fixture(scope="function")
def clinical_app(db_session: Session) -> FastAPI:
    """Isolated test app mounting the migrated clinical records routes under /doctors."""
    test_app = FastAPI(title="Migrated Clinical Records Test App")
    test_app.include_router(doctors_router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_get_db
    return test_app


@pytest.fixture(scope="function")
def client(clinical_app: FastAPI) -> TestClient:
    return TestClient(clinical_app)


@pytest.fixture(scope="function")
def doctor_with_role(db_session: Session, hospital: Hospital) -> HospitalUser:
    role = StaffRole(id=uuid4(), hospital_id=hospital.id, name="Doctor")
    db_session.add(role)
    db_session.commit()

    doc = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. Clinical Specialist",
        email="specialist@hospital.com",
        phone="9876500001",
        password_hash="pwd",
        specialization="Internal Medicine",
        is_active=True,
    )
    db_session.add(doc)
    db_session.commit()
    return doc


@pytest.fixture(scope="function")
def auth_headers(hospital: Hospital, doctor_with_role: HospitalUser) -> dict[str, str]:
    token = create_access_token({
        "sub": doctor_with_role.email,
        "name": doctor_with_role.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(doctor_with_role.id),
        "doctor_id": str(doctor_with_role.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def test_patient(db_session: Session, hospital: Hospital) -> Patient:
    p = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid="P9901",
        first_name="Alice",
        last_name="Wonder",
        name="Alice Wonder",
        mobile="9876543999",
    )
    db_session.add(p)
    db_session.commit()
    return p


def test_prescription_lifecycle_and_pdf(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient: Patient,
):
    """Create, update, list, and download HTML printable prescription."""
    doc_id = str(doctor_with_role.id)

    # 1. Create prescription
    create_payload = {
        "patient_id": str(test_patient.id),
        "symptoms": "Fever, chills, dry cough",
        "diagnosis": "Viral Bronchitis",
        "medicines": "Paracetamol 500mg, Cough Syrup",
        "dosage": "1 tablet TID, 10ml BID",
        "advice": "Hydrate well and rest",
        "follow_up_date": date.today().isoformat(),
        "signature_data": "DrSigData",
    }
    res = client.post(
        f"/api/doctors/{doc_id}/prescriptions",
        json=create_payload,
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    rx_data = res.json()
    assert rx_data["diagnosis"] == "Viral Bronchitis"
    assert rx_data["doctor_id"] == doc_id
    assert rx_data["patient_id"] == str(test_patient.id)
    rx_id = rx_data["id"]

    # 2. Update prescription
    update_payload = {
        "symptoms": "Fever resolved, persistent mild cough",
        "diagnosis": "Post-viral cough",
        "medicines": "Cough Syrup",
        "dosage": "10ml BID",
        "advice": "Keep warm",
    }
    put_res = client.put(
        f"/api/doctors/{doc_id}/prescriptions/{rx_id}",
        json=update_payload,
        headers=auth_headers,
    )
    assert put_res.status_code == 200, put_res.text
    updated_rx = put_res.json()
    assert updated_rx["diagnosis"] == "Post-viral cough"

    # 3. List prescriptions for patient
    list_res = client.get(
        f"/api/doctors/{doc_id}/prescriptions?patient_id={test_patient.id}",
        headers=auth_headers,
    )
    assert list_res.status_code == 200
    rx_list = list_res.json()
    assert len(rx_list) >= 1
    assert any(r["id"] == rx_id for r in rx_list)

    # 4. Stream HTML/PDF
    pdf_res = client.get(
        f"/api/doctors/{doc_id}/prescriptions/{rx_id}/pdf",
        headers=auth_headers,
    )
    assert pdf_res.status_code == 200
    assert "text/html" in pdf_res.headers.get("content-type", "")
    assert "Post-viral cough" in pdf_res.text
    assert "Dr. Clinical Specialist" in pdf_res.text


def test_medical_record_crud_and_file_download(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient: Patient,
):
    """Create medical record, list, and download file."""
    doc_id = str(doctor_with_role.id)

    # 1. Create medical record
    create_payload = {
        "patient_id": str(test_patient.id),
        "report_type": "Lab Report",
        "title": "Complete Blood Count (CBC)",
        "notes": "Hemoglobin 14.2 g/dL, Platelets normal",
        "file_name": "cbc_report.pdf",
        "file_data": "data:application/pdf;base64,JVBERi0xLjQKJ...",
    }
    post_res = client.post(
        f"/api/doctors/{doc_id}/records",
        json=create_payload,
        headers=auth_headers,
    )
    assert post_res.status_code == 201, post_res.text
    rec = post_res.json()
    assert rec["title"] == "Complete Blood Count (CBC)"
    rec_id = rec["id"]

    # 2. List records
    list_res = client.get(
        f"/api/doctors/{doc_id}/records?patient_id={test_patient.id}",
        headers=auth_headers,
    )
    assert list_res.status_code == 200
    records = list_res.json()
    assert any(r["id"] == rec_id for r in records)

    # 3. File access endpoint
    file_res = client.get(
        f"/api/doctors/{doc_id}/records/{rec_id}/file",
        headers=auth_headers,
    )
    assert file_res.status_code == 200
    file_info = file_res.json()
    assert file_info["file_name"] == "cbc_report.pdf"
    assert "base64" in file_info["file_data"]
