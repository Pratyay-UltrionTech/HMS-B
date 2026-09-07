"""
Comprehensive tests for target-native DMS module.

Tests patient file indexing, document uploads/downloads/deletions,
clinical timeline collation, printable complete file generation, and tenant isolation.
"""

import base64
from datetime import date, time
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.appointments.entities.appointment import (
    Appointment,
    AppointmentStatus,
)
from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    PatientDocument,
    PatientDocumentCategory,
    Prescription,
)
from hms_migration.modules.dms.api.dms_api import router as dms_router
from hms_migration.modules.doctors.entities.doctor import HospitalUser, StaffRole
from hms_migration.modules.inpatient.entities.admission import (
    Admission,
    AdmissionStatus,
    Bed,
    Room,
    Ward,
)
from hms_migration.modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderItem,
    LabOrderStatus,
)
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def dms_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def dms_client(dms_db):
    app = FastAPI()
    app.include_router(dms_router, prefix="/api")

    test_hospital_id = uuid4()
    hospital = Hospital(
        id=test_hospital_id,
        hospital_id="HOSP-DMS-01",
        name="Fortis Records Center",
        address="123 Archive Blvd",
        phone="9876543210",
        email="records@fortis.com",
        password_hash="hashed_pw",
    )
    dms_db.add(hospital)
    dms_db.commit()

    test_user = {
        "id": str(uuid4()),
        "sub": "records_clerk",
        "name": "Sarah Miller",
        "role": "records_clerk",
        "staff_role_name": "Medical Records Clerk",
        "hospital_id": str(test_hospital_id),
    }

    app.dependency_overrides[get_transitional_sync_session] = lambda: dms_db
    app.dependency_overrides[get_hospital_context] = lambda: test_hospital_id
    app.dependency_overrides[require_hospital_user] = lambda: test_user

    client = TestClient(app)
    client.test_hospital_id = test_hospital_id
    client.test_user = test_user
    return client


def test_dms_patients_list_and_care_status(dms_client, dms_db):
    h_id = dms_client.test_hospital_id

    # 1. Create OPD patient
    p_opd = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-DMS-OPD",
        name="Anil Kumar",
        mobile="9811111111",
        gender="male",
        age=40,
        status=PatientStatus.active,
    )
    # 2. Create IPD patient
    p_ipd = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-DMS-IPD",
        name="Sunita Sharma",
        mobile="9822222222",
        gender="female",
        age=32,
        status=PatientStatus.admitted,
    )
    dms_db.add_all([p_opd, p_ipd])
    dms_db.commit()

    # List all
    res = dms_client.get("/api/dms/patients")
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 2

    # Filter OPD
    res_opd = dms_client.get("/api/dms/patients?care_status=OPD")
    assert len(res_opd.json()) == 1
    assert res_opd.json()[0]["uhid"] == "UHID-DMS-OPD"

    # Filter IPD
    res_ipd = dms_client.get("/api/dms/patients?care_status=IPD")
    assert len(res_ipd.json()) == 1
    assert res_ipd.json()[0]["uhid"] == "UHID-DMS-IPD"


def test_dms_document_upload_download_delete(dms_client, dms_db):
    h_id = dms_client.test_hospital_id
    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-DOC-01",
        name="Vikram Seth",
        mobile="9833333333",
    )
    dms_db.add(patient)
    dms_db.commit()

    # Base64 test payload
    sample_content = b"PDF dummy test document content"
    b64_content = base64.b64encode(sample_content).decode("utf-8")
    data_url = f"data:application/pdf;base64,{b64_content}"

    # 1. Upload document
    upload_resp = dms_client.post(
        f"/api/dms/patients/{patient.id}/documents",
        json={
            "category": "insurance",
            "title": "Star Health Card",
            "notes": "Valid till 2027",
            "file_name": "insurance_card.pdf",
            "file_data": data_url,
        },
    )
    assert upload_resp.status_code == 201
    doc_data = upload_resp.json()
    assert doc_data["title"] == "Star Health Card"
    assert doc_data["has_file"] is True
    doc_id = doc_data["id"]

    # 2. List documents
    list_resp = dms_client.get(f"/api/dms/patients/{patient.id}/documents")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # 3. Download file
    dl_resp = dms_client.get(f"/api/dms/documents/{doc_id}/file")
    assert dl_resp.status_code == 200
    assert dl_resp.content == sample_content

    # 4. Delete document
    del_resp = dms_client.delete(f"/api/dms/documents/{doc_id}")
    assert del_resp.status_code == 204

    # Verify deleted
    assert len(dms_client.get(f"/api/dms/patients/{patient.id}/documents").json()) == 0


def test_dms_timeline_and_patient_file_assembly(dms_client, dms_db):
    h_id = dms_client.test_hospital_id
    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-FULL-01",
        name="Meera Nair",
        mobile="9844444444",
        age=45,
        gender="female",
    )
    dms_db.add(patient)
    dms_db.commit()

    # Add Appointment
    appt = Appointment(
        id=uuid4(),
        hospital_id=h_id,
        patient_id=patient.id,
        doctor_id=uuid4(),
        appointment_date=date(2026, 9, 1),
        appointment_time=time(10, 0),
        status=AppointmentStatus.scheduled,
        purpose="General Checkup",
    )
    dms_db.add(appt)

    # Add Prescription
    rx = Prescription(
        id=uuid4(),
        hospital_id=h_id,
        patient_id=patient.id,
        doctor_id=uuid4(),
        symptoms="Fever, cough",
        diagnosis="Viral upper respiratory infection",
        medicines="Paracetamol 650mg",
        dosage="TDS x 3 days",
    )
    dms_db.add(rx)

    # Add Lab Order
    lab_order = LabOrder(
        id=uuid4(),
        hospital_id=h_id,
        order_no="LAB-DMS-01",
        patient_id=patient.id,
        status=LabOrderStatus.ordered,
    )
    dms_db.add(lab_order)
    dms_db.commit()

    # 1. Test timeline
    tl_resp = dms_client.get(f"/api/dms/patients/{patient.id}/timeline")
    assert tl_resp.status_code == 200
    events = tl_resp.json()
    assert len(events) >= 4  # registration, appointment, prescription, lab_order
    event_types = [e["event_type"] for e in events]
    assert "registration" in event_types
    assert "appointment" in event_types
    assert "prescription" in event_types
    assert "lab_order" in event_types

    # 2. Test patient file
    file_resp = dms_client.get(f"/api/dms/patients/{patient.id}/file")
    assert file_resp.status_code == 200
    file_data = file_resp.json()
    assert file_data["patient"]["uhid"] == "UHID-FULL-01"
    assert len(file_data["prescriptions"]) == 1
    assert len(file_data["lab_reports"]) == 1

    # 3. Test complete file HTML printable report
    html_resp = dms_client.get(f"/api/dms/patients/{patient.id}/complete-file")
    assert html_resp.status_code == 200
    assert "text/html" in html_resp.headers["content-type"]
    assert "UHID-FULL-01" in html_resp.text
    assert "Viral upper respiratory infection" in html_resp.text


def test_dms_tenant_isolation(dms_client, dms_db):
    h_other = Hospital(
        id=uuid4(),
        hospital_id="HOSP-OTHER-DMS",
        name="Other Medical Center",
        address="999 Border Rd",
        phone="5554443322",
        email="other@hospital.com",
        password_hash="hashed_pw",
    )
    dms_db.add(h_other)
    dms_db.commit()

    other_patient = Patient(
        id=uuid4(),
        hospital_id=h_other.id,
        uhid="UHID-OTHER-01",
        name="Private Person",
        mobile="9855555555",
    )
    dms_db.add(other_patient)
    dms_db.commit()

    # Patient of other hospital cannot be fetched
    res = dms_client.get(f"/api/dms/patients/{other_patient.id}/file")
    assert res.status_code == 404
