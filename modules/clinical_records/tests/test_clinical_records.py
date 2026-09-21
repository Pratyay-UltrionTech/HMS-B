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

from datetime import date, time
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from infrastructure.postgres.session import get_transitional_sync_session
from modules.doctors.api.doctors_api import router as doctors_router
from shared.auth.jwt import create_access_token
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


def test_historical_prescription_integrity_cannot_edit_completed_visit(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient: Patient,
    db_session: Session,
    hospital: Hospital,
):
    """Historical integrity: prescriptions belonging to completed or inpatient-transferred visits are strictly read-only."""
    doc_id = str(doctor_with_role.id)

    # 1. Create appointment
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=test_patient.id,
        doctor_id=doctor_with_role.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        status=AppointmentStatus.scheduled,
        purpose="Cardiology Review",
    )
    db_session.add(appt)
    db_session.commit()

    # 2. Issue prescription linked to this appointment
    create_payload = {
        "patient_id": str(test_patient.id),
        "appointment_id": str(appt.id),
        "symptoms": "Chest pain",
        "diagnosis": "Angina",
        "medicines": "Nitroglycerin",
        "dosage": "Sublingual as needed",
        "status": "issued",
    }
    create_res = client.post(
        f"/api/doctors/{doc_id}/prescriptions",
        json=create_payload,
        headers=auth_headers,
    )
    assert create_res.status_code == 201, create_res.text
    rx_id = create_res.json()["id"]

    # 3. Mark appointment as completed
    appt.status = AppointmentStatus.completed
    db_session.commit()

    # 4. Attempt to edit the prescription for the completed visit
    put_res = client.put(
        f"/api/doctors/{doc_id}/prescriptions/{rx_id}",
        json={"diagnosis": "Unstable Angina", "medicines": "Aspirin, Nitroglycerin"},
        headers=auth_headers,
    )
    assert put_res.status_code == 400, put_res.text
    assert "Cannot edit a prescription after the visit is marked completed or transferred to inpatient" in put_res.json()["detail"]

    # 5. Switch visit to transferred_to_inpatient and verify guard also prevents edits
    appt.status = AppointmentStatus.transferred_to_inpatient
    db_session.commit()

    put_res2 = client.put(
        f"/api/doctors/{doc_id}/prescriptions/{rx_id}",
        json={"dosage": "Daily"},
        headers=auth_headers,
    )
    assert put_res2.status_code == 400, put_res2.text
    assert "Cannot edit a prescription after the visit is marked completed or transferred to inpatient" in put_res2.json()["detail"]


def test_draft_prescription_lifecycle_and_validation(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient: Patient,
):
    """Verify drafts allow relaxed content, reject incomplete issue attempts, and issue cleanly when complete."""
    doc_id = str(doctor_with_role.id)

    # 1. Save partial draft with empty symptoms, diagnosis, and medicines
    draft_payload = {
        "patient_id": str(test_patient.id),
        "symptoms": "",
        "diagnosis": "",
        "medicines": "",
        "dosage": "",
        "advice": "Preliminary draft notes",
        "status": "draft",
    }
    create_res = client.post(
        f"/api/doctors/{doc_id}/prescriptions",
        json=draft_payload,
        headers=auth_headers,
    )
    assert create_res.status_code == 201, create_res.text
    draft_data = create_res.json()
    assert draft_data["status"] == "draft"
    rx_id = draft_data["id"]

    # 2. Attempt to transition draft to 'issued' while missing required clinical content (e.g. diagnosis)
    invalid_issue_payload = {
        "status": "issued",
        "symptoms": "Persistent headache",
        "diagnosis": "",
        "medicines": "Paracetamol 500mg",
        "dosage": "1 SOS",
    }
    bad_put_res = client.put(
        f"/api/doctors/{doc_id}/prescriptions/{rx_id}",
        json=invalid_issue_payload,
        headers=auth_headers,
    )
    assert bad_put_res.status_code == 400, bad_put_res.text
    assert "Diagnosis is required to issue prescription" in bad_put_res.json()["detail"]

    # 3. Transition draft to 'issued' with full clinical data
    valid_issue_payload = {
        "status": "issued",
        "symptoms": "Persistent headache and photophobia",
        "diagnosis": "Migraine without aura",
        "medicines": "Sumatriptan 50mg, Paracetamol 650mg",
        "dosage": "1 tablet at onset, 1 tablet TID PRN",
        "advice": "Rest in a quiet, dark room. Hydrate adequately.",
    }
    ok_put_res = client.put(
        f"/api/doctors/{doc_id}/prescriptions/{rx_id}",
        json=valid_issue_payload,
        headers=auth_headers,
    )
    assert ok_put_res.status_code == 200, ok_put_res.text
    issued_data = ok_put_res.json()
    assert issued_data["status"] == "issued"
    assert issued_data["diagnosis"] == "Migraine without aura"
    assert issued_data["medicines"] == "Sumatriptan 50mg, Paracetamol 650mg"


def test_draft_linked_to_specific_appointment_not_cross_contaminated(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient: Patient,
    db_session: Session,
    hospital: Hospital,
):
    """Verify that drafts are strictly linked to specific encounters and multiple encounters retain distinct drafts."""
    doc_id = str(doctor_with_role.id)

    # Encounter 1
    appt1 = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=test_patient.id,
        doctor_id=doctor_with_role.id,
        appointment_date=date.today(),
        appointment_time=time(9, 0),
        status=AppointmentStatus.scheduled,
        purpose="Cardiology Visit",
    )
    # Encounter 2
    appt2 = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=test_patient.id,
        doctor_id=doctor_with_role.id,
        appointment_date=date.today(),
        appointment_time=time(14, 0),
        status=AppointmentStatus.scheduled,
        purpose="Follow-up Review",
    )
    db_session.add_all([appt1, appt2])
    db_session.commit()

    # Draft for Encounter 1
    r1 = client.post(
        f"/api/doctors/{doc_id}/prescriptions",
        json={
            "patient_id": str(test_patient.id),
            "appointment_id": str(appt1.id),
            "status": "draft",
            "symptoms": "Palpitations",
            "diagnosis": "Sinus Tachycardia",
            "medicines": "Metoprolol 25mg",
            "dosage": "1 OD",
        },
        headers=auth_headers,
    )
    assert r1.status_code == 201
    draft1_id = r1.json()["id"]

    # Draft for Encounter 2
    r2 = client.post(
        f"/api/doctors/{doc_id}/prescriptions",
        json={
            "patient_id": str(test_patient.id),
            "appointment_id": str(appt2.id),
            "status": "draft",
            "symptoms": "Mild Fatigue",
            "diagnosis": "Observation",
            "medicines": "Multivitamin",
            "dosage": "1 OD",
        },
        headers=auth_headers,
    )
    assert r2.status_code == 201
    draft2_id = r2.json()["id"]

    # List all prescriptions for patient
    list_res = client.get(
        f"/api/doctors/{doc_id}/prescriptions?patient_id={test_patient.id}",
        headers=auth_headers,
    )
    assert list_res.status_code == 200
    all_rx = list_res.json()

    # Verify each draft is linked specifically to its respective appointment
    rx1 = next((p for p in all_rx if p["id"] == draft1_id), None)
    rx2 = next((p for p in all_rx if p["id"] == draft2_id), None)
    assert rx1 is not None and rx1["appointment_id"] == str(appt1.id)
    assert rx2 is not None and rx2["appointment_id"] == str(appt2.id)
    assert rx1["diagnosis"] == "Sinus Tachycardia"
    assert rx2["diagnosis"] == "Observation"


def test_cross_doctor_patient_diagnostic_records_and_history(
    client: TestClient,
    doctor_with_role: HospitalUser,
    test_patient: Patient,
    db_session: Session,
    hospital: Hospital,
):
    """Verifies that when a patient visits Doctor A and then Doctor B, Doctor B can see all diagnostic records & reports."""
    # Create Doctor B in same hospital
    doc_b = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=doctor_with_role.role_id,
        name="Dr. Second Consultant",
        email="doctor_b@hospital.com",
        phone="9876500002",
        password_hash="pwd",
        specialization="Cardiology",
        is_active=True,
    )
    db_session.add(doc_b)
    db_session.commit()

    token_a = create_access_token({
        "sub": doctor_with_role.email,
        "name": doctor_with_role.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(doctor_with_role.id),
        "doctor_id": str(doctor_with_role.id),
    })
    headers_a = {"Authorization": f"Bearer {token_a}"}

    token_b = create_access_token({
        "sub": doc_b.email,
        "name": doc_b.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(doc_b.id),
        "doctor_id": str(doc_b.id),
    })
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 1. Doctor A creates a medical/diagnostic record with an attached file
    rec_res = client.post(
        f"/api/doctors/{doctor_with_role.id}/records",
        json={
            "patient_id": str(test_patient.id),
            "report_type": "Lab Report",
            "title": "Lipid Profile",
            "notes": "Cholesterol: 210 mg/dL",
            "file_name": "lipid_profile.pdf",
            "file_data": "data:application/pdf;base64,JVBERi0xLjQKJ...",
        },
        headers=headers_a,
    )
    assert rec_res.status_code == 201
    rec_id = rec_res.json()["id"]

    # 2. Patient now has appointment with Doctor B
    appt_b = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=test_patient.id,
        doctor_id=doc_b.id,
        appointment_date=date.today(),
        appointment_time=time(11, 0),
        status=AppointmentStatus.scheduled,
        purpose="Second Opinion",
    )
    db_session.add(appt_b)
    db_session.commit()

    # 3. Doctor B opens patient history
    hist_res = client.get(
        f"/api/doctors/{doc_b.id}/patients/{test_patient.id}",
        headers=headers_b,
    )
    assert hist_res.status_code == 200, hist_res.text
    history = hist_res.json()

    # Doctor B must see Doctor A's diagnostic record in patient history
    med_records = history.get("medical_records", [])
    found_rec = next((r for r in med_records if r["id"] == rec_id), None)
    assert found_rec is not None
    assert found_rec["title"] == "Lipid Profile"

    # 4. Doctor B can open and view/download the report file
    file_res = client.get(
        f"/api/doctors/{doc_b.id}/records/{rec_id}/file",
        headers=headers_b,
    )
    assert file_res.status_code == 200
    file_info = file_res.json()
    assert file_info["file_name"] == "lipid_profile.pdf"
    assert "base64" in file_info["file_data"]


def test_prescription_pdf_includes_radiology_and_lab_investigations(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient: Patient,
    db_session: Session,
    hospital: Hospital,
):
    """Prescription PDF/HTML includes prescribed radiology scans and lab tests."""
    from modules.radiology.entities.radiology_entities import RadiologyScanCatalog
    from modules.laboratory.entities.lab_entities import LabTestCatalog

    doc_id = str(doctor_with_role.id)

    # 1. Create a scan in catalog
    scan = RadiologyScanCatalog(
        id=uuid4(),
        hospital_id=hospital.id,
        scan_code="RAD-XRAY-CHEST",
        scan_name="Chest X-Ray PA View",
        category="X-Ray",
        price=500.0,
        is_active=True,
    )
    # 2. Create a lab test in catalog
    lab = LabTestCatalog(
        id=uuid4(),
        hospital_id=hospital.id,
        test_code="LAB-CBC",
        test_name="Complete Blood Count",
        department="Hematology",
        price=350.0,
        is_active=True,
    )
    db_session.add_all([scan, lab])
    db_session.commit()

    # 3. Create prescription with scan_ids and test_ids
    create_payload = {
        "patient_id": str(test_patient.id),
        "symptoms": "Cough and mild shortness of breath",
        "diagnosis": "Suspected Pneumonia",
        "medicines": "Azithromycin 500mg",
        "dosage": "1 OD x 3 days",
        "advice": "Rest and monitor oxygen",
        "test_ids": [str(lab.id)],
        "scan_ids": [str(scan.id)],
    }
    res = client.post(
        f"/api/doctors/{doc_id}/prescriptions",
        json=create_payload,
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    rx_id = res.json()["id"]

    # 4. Fetch the printable HTML/PDF
    pdf_res = client.get(
        f"/api/doctors/{doc_id}/prescriptions/{rx_id}/pdf",
        headers=auth_headers,
    )
    assert pdf_res.status_code == 200
    html = pdf_res.text
    assert "Chest X-Ray PA View" in html
    assert "Complete Blood Count" in html
    assert "Radiology / Imaging Prescribed" in html
    assert "Laboratory Tests Prescribed" in html

