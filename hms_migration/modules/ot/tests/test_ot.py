"""
Unit and integration tests for Operation Theatre (OT) domain.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
from hms_migration.modules.beds.entities.bed import Bed, Room, Ward, WardType
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    Prescription,
)
from hms_migration.modules.doctors.entities.doctor import HospitalUser, StaffRole
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.masters.entities.organization_entities import Department, Wing
from hms_migration.modules.ot.api.ot_api import router as ot_router
from hms_migration.modules.ot.entities.ot_entities import (
    OtPriority,
    OtRoom,
    OtSurgery,
    OtSurgeryStatus,
)
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def ot_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def ot_client(ot_db):
    app = FastAPI()
    app.include_router(ot_router, prefix="/api")

    hospital_id = uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id="HOSP-OT-01",
        name="Apex Surgical Hospital",
        address="456 Surgery Rd",
        phone="9876543211",
        email="ot@hospital.com",
        password_hash="fakehash",
        is_active=True,
    )
    ot_db.add(hospital)

    # Department
    dept = Department(
        id=uuid4(),
        hospital_id=hospital_id,
        name="General Surgery",
        code="SURG",
        is_active=True,
    )
    ot_db.add(dept)

    # OT Room
    ot_room = OtRoom(
        id=uuid4(),
        hospital_id=hospital_id,
        department_id=dept.id,
        code="OT-1",
        name="Major OT 1",
        base_ot_charge=15000.0,
        is_active=True,
    )
    ot_db.add(ot_room)

    # Role & Doctor
    role = StaffRole(
        id=uuid4(),
        hospital_id=hospital_id,
        name="surgeon",
        description="Surgeon Role",
        is_active=True,
    )
    ot_db.add(role)

    surgeon_id = uuid4()
    surgeon = HospitalUser(
        id=surgeon_id,
        hospital_id=hospital_id,
        role_id=role.id,
        name="Dr. Arjun Roy",
        phone="9876543212",
        email="arjun.roy@hospital.com",
        password_hash="fakehash",
        is_active=True,
    )
    ot_db.add(surgeon)

    # Ward, Room, Bed for IPD admission prerequisite
    ward = Ward(
        id=uuid4(),
        hospital_id=hospital_id,
        name="Surgical Ward",
        ward_type=WardType.general,
        is_active=True,
    )
    ot_db.add(ward)
    room = Room(
        id=uuid4(),
        hospital_id=hospital_id,
        ward_id=ward.id,
        room_code="101",
        name="Room 101",
        bed_count=1,
        is_active=True,
    )
    ot_db.add(room)
    bed = Bed(
        id=uuid4(),
        hospital_id=hospital_id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="B-101",
        is_active=True,
    )
    ot_db.add(bed)
    ot_db.commit()

    def override_get_db():
        yield ot_db

    def override_get_hospital_context():
        return hospital_id

    def override_require_hospital_user():
        return {
            "sub": str(surgeon_id),
            "hospital_id": str(hospital_id),
            "name": "Dr. Arjun Roy",
            "role": "doctor",
            "staff_role_name": "Chief Surgeon",
        }

    app.dependency_overrides[get_transitional_sync_session] = override_get_db
    app.dependency_overrides[get_hospital_context] = override_get_hospital_context
    app.dependency_overrides[require_hospital_user] = override_require_hospital_user

    client = TestClient(app)
    client.test_hospital_id = hospital_id
    client.test_surgeon_id = surgeon_id
    client.test_dept_id = dept.id
    client.test_room_id = ot_room.id
    client.test_ward_id = ward.id
    client.test_room_id_bed = room.id
    client.test_bed_id = bed.id
    return client


def test_surgery_booking_validations_and_billing_charge(ot_client, ot_db):
    h_id = ot_client.test_hospital_id
    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-OT-001",
        name="Amitabh Sengupta",
        mobile="9811111111",
        age=60,
        gender="male",
    )
    ot_db.add(patient)
    ot_db.commit()

    payload = {
        "patient_id": str(patient.id),
        "surgeon_id": str(ot_client.test_surgeon_id),
        "surgery_type": "Laparoscopic Cholecystectomy",
        "surgery_category": "General",
        "priority": "elective",
        "department_id": str(ot_client.test_dept_id),
        "ot_room_id": str(ot_client.test_room_id),
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "duration_minutes": 90,
    }

    # 1. Booking fails if patient has no active IPD admission
    resp_no_ipd = ot_client.post("/api/ot/surgeries", json=payload)
    assert resp_no_ipd.status_code == 400
    assert "Transfer the patient to IPD" in resp_no_ipd.json()["detail"]

    # Add active admission
    adm = Admission(
        id=uuid4(),
        hospital_id=h_id,
        patient_id=patient.id,
        ward_id=ot_client.test_ward_id,
        room_id=ot_client.test_room_id_bed,
        bed_id=ot_client.test_bed_id,
        doctor_id=ot_client.test_surgeon_id,
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    ot_db.add(adm)
    ot_db.commit()

    # 2. Booking fails if patient has no prescription
    resp_no_rx = ot_client.post("/api/ot/surgeries", json=payload)
    assert resp_no_rx.status_code == 400
    assert "Write a prescription" in resp_no_rx.json()["detail"]

    # Add prescription
    rx = Prescription(
        id=uuid4(),
        hospital_id=h_id,
        patient_id=patient.id,
        doctor_id=ot_client.test_surgeon_id,
        symptoms="Abdominal pain, gallstones",
        diagnosis="Cholelithiasis",
        medicines="Ceftriaxone 1g IV",
        dosage="OD x 3 days",
    )
    ot_db.add(rx)
    ot_db.commit()

    # 3. Successful booking
    resp_ok = ot_client.post("/api/ot/surgeries", json=payload)
    assert resp_ok.status_code == 201
    surgery_data = resp_ok.json()
    surgery_id = surgery_data["id"]
    assert surgery_data["status"] == "scheduled"
    assert surgery_data["ot_charge_amount"] == 15000.0

    # Verify target billing charge created
    charge = (
        ot_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.source_id == UUID(surgery_id),
            BillingCharge.source_type == BillingSourceType.ot,
        )
        .first()
    )
    assert charge is not None
    assert charge.charge_amount == 15000.0
    assert charge.status == BillingChargeStatus.pending

    # 4. Room conflict prevention: try to book overlapping slot in same room
    overlap_payload = dict(payload)
    overlap_payload["scheduled_at"] = (
        datetime.fromisoformat(payload["scheduled_at"]) + timedelta(minutes=30)
    ).isoformat()
    resp_conflict = ot_client.post("/api/ot/surgeries", json=overlap_payload)
    assert resp_conflict.status_code == 409
    assert "already booked" in resp_conflict.json()["detail"]


def test_surgery_full_lifecycle_and_medical_records(ot_client, ot_db):
    h_id = ot_client.test_hospital_id
    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-OT-002",
        name="Sunita Deshmukh",
        mobile="9822222221",
    )
    adm = Admission(
        id=uuid4(),
        hospital_id=h_id,
        patient_id=patient.id,
        ward_id=ot_client.test_ward_id,
        room_id=ot_client.test_room_id_bed,
        bed_id=ot_client.test_bed_id,
        doctor_id=ot_client.test_surgeon_id,
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    rx = Prescription(
        id=uuid4(),
        hospital_id=h_id,
        patient_id=patient.id,
        doctor_id=ot_client.test_surgeon_id,
        symptoms="Right lower quadrant pain",
        diagnosis="Acute appendicitis",
        medicines="Amoxicillin-Clavulanate 1.2g",
        dosage="TDS",
    )
    ot_db.add_all([patient, adm, rx])
    ot_db.commit()

    # Book surgery
    booked = ot_client.post(
        "/api/ot/surgeries",
        json={
            "patient_id": str(patient.id),
            "surgeon_id": str(ot_client.test_surgeon_id),
            "surgery_type": "Appendectomy",
            "department_id": str(ot_client.test_dept_id),
            "ot_room_id": str(ot_client.test_room_id),
            "scheduled_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "duration_minutes": 60,
        },
    ).json()
    surgery_id = booked["id"]

    # 1. Confirm surgery
    conf_resp = ot_client.post(f"/api/ot/surgeries/{surgery_id}/confirm")
    assert conf_resp.status_code == 200
    assert conf_resp.json()["status"] == "confirmed"

    # 2. Reschedule surgery
    new_time = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    resched_resp = ot_client.post(
        f"/api/ot/surgeries/{surgery_id}/reschedule",
        json={"scheduled_at": new_time, "duration_minutes": 75},
    )
    assert resched_resp.status_code == 200
    assert resched_resp.json()["duration_minutes"] == 75

    # 3. Start surgery
    start_resp = ot_client.post(f"/api/ot/surgeries/{surgery_id}/start")
    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "in_progress"

    # 4. Save operation notes & attachments
    sample_pdf = "data:application/pdf;base64," + base64.b64encode(b"dummy_ot_report").decode("ascii")
    notes_resp = ot_client.post(
        f"/api/ot/surgeries/{surgery_id}/notes",
        json={
            "pre_op_diagnosis": "Acute appendicitis with localized peritonitis",
            "procedure_performed": "Laparoscopic Appendectomy",
            "findings": "Inflamed appendix adherent to cecum, no perforation.",
            "implants_used": "Endoloop suture x 2",
            "post_op_instructions": "NPO for 6 hours, IV fluids, analgesics",
            "shifted_to": "PACU / Post-op Recovery",
            "ot_report_file_name": "op_note.pdf",
            "ot_report_file_data": sample_pdf,
        },
    )
    assert notes_resp.status_code == 200
    notes_data = notes_resp.json()
    assert notes_data["status"] == "completed"
    assert notes_data["has_notes"] is True
    assert notes_data["has_ot_report"] is True

    # 5. Check Medical Record synchronization
    med_rec = (
        ot_db.query(MedicalRecord)
        .filter(
            MedicalRecord.hospital_id == h_id,
            MedicalRecord.patient_id == patient.id,
            MedicalRecord.report_type == "OT",
        )
        .first()
    )
    assert med_rec is not None
    assert "Laparoscopic Appendectomy" in med_rec.notes

    # 6. HTML summary view
    html_resp = ot_client.get(f"/api/ot/surgeries/{surgery_id}/summary-view")
    assert html_resp.status_code == 200
    assert "text/html" in html_resp.headers["content-type"]
    assert "Laparoscopic Appendectomy" in html_resp.text

    # 7. File streaming
    file_resp = ot_client.get(f"/api/ot/surgeries/{surgery_id}/file/report")
    assert file_resp.status_code == 200
    assert file_resp.content == b"dummy_ot_report"


def test_surgery_cancellation_and_billing_cancellation(ot_client, ot_db):
    h_id = ot_client.test_hospital_id
    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-OT-003",
        name="Radha Krishna",
        mobile="9833333333",
    )
    adm = Admission(
        id=uuid4(),
        hospital_id=h_id,
        patient_id=patient.id,
        ward_id=ot_client.test_ward_id,
        room_id=ot_client.test_room_id_bed,
        bed_id=ot_client.test_bed_id,
        doctor_id=ot_client.test_surgeon_id,
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    rx = Prescription(
        id=uuid4(),
        hospital_id=h_id,
        patient_id=patient.id,
        doctor_id=ot_client.test_surgeon_id,
        symptoms="Inguinal swelling",
        diagnosis="Inguinal hernia",
        medicines="Paracetamol 500mg",
        dosage="PRN",
    )
    ot_db.add_all([patient, adm, rx])
    ot_db.commit()

    booked = ot_client.post(
        "/api/ot/surgeries",
        json={
            "patient_id": str(patient.id),
            "surgeon_id": str(ot_client.test_surgeon_id),
            "surgery_type": "Hernia Repair",
            "department_id": str(ot_client.test_dept_id),
            "ot_room_id": str(ot_client.test_room_id),
            "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        },
    ).json()
    surgery_id = booked["id"]

    charge = (
        ot_db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == h_id,
            BillingCharge.source_id == UUID(surgery_id),
        )
        .first()
    )
    assert charge.status == BillingChargeStatus.pending

    # Cancel surgery
    c_resp = ot_client.post(f"/api/ot/surgeries/{surgery_id}/cancel")
    assert c_resp.status_code == 200
    assert c_resp.json()["status"] == "cancelled"

    # Verify charge was cancelled
    ot_db.refresh(charge)
    assert charge.status == BillingChargeStatus.cancelled


def test_ot_calendar_and_dashboard(ot_client, ot_db):
    h_id = ot_client.test_hospital_id
    today = date.today()

    # Dashboard check
    dash_resp = ot_client.get("/api/ot/dashboard")
    assert dash_resp.status_code == 200
    assert "todays_surgeries" in dash_resp.json()

    # Calendar check
    cal_resp = ot_client.get(
        f"/api/ot/calendar?date_from={today.isoformat()}&date_to={(today + timedelta(days=7)).isoformat()}"
    )
    assert cal_resp.status_code == 200
    assert isinstance(cal_resp.json(), list)
