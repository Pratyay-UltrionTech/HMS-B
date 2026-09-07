"""
Unit and integration tests for MIS (Management Information System) reporting domain.
"""

from __future__ import annotations

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
from hms_migration.modules.appointments.entities.appointment import Appointment, AppointmentStatus
from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
from hms_migration.modules.beds.entities.bed import Bed, Room, Ward, WardType
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
    ConsultationPricing,
)
from hms_migration.modules.clinical_records.entities.clinical_record import (
    MedicalRecord,
    Prescription,
)
from hms_migration.modules.doctors.entities.doctor import HospitalUser, StaffRole
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.masters.entities.organization_entities import Department, Wing
from hms_migration.modules.mis.api.mis_api import router as mis_router
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def mis_db():
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
def mis_client(mis_db):
    hospital_id = uuid4()
    user_id = uuid4()

    hospital = Hospital(
        id=hospital_id,
        hospital_id="HOSP-MIS-001",
        name="MIS Test Hospital",
        address="123 Hospital St",
        phone="1234567890",
        email="admin@mistest.com",
        password_hash="fakehash",
        is_active=True,
    )
    role = StaffRole(
        id=uuid4(),
        hospital_id=hospital_id,
        name="doctor",
    )
    user = HospitalUser(
        id=user_id,
        hospital_id=hospital_id,
        role_id=role.id,
        name="Doctor MIS",
        phone="1234567890",
        email="doctor@mistest.com",
        password_hash="fakehash",
        is_active=True,
    )
    dept = Department(
        id=uuid4(),
        hospital_id=hospital_id,
        name="Cardiology",
        code="CARD",
    )

    mis_db.add_all([hospital, role, dept, user])
    mis_db.commit()

    app = FastAPI()
    app.include_router(mis_router)

    app.dependency_overrides[get_transitional_sync_session] = lambda: mis_db
    app.dependency_overrides[get_hospital_context] = lambda: hospital_id
    app.dependency_overrides[require_hospital_user] = lambda: {
        "user_id": str(user_id),
        "hospital_id": str(hospital_id),
        "role": "doctor",
        "email": "doctor@mistest.com",
    }

    client = TestClient(app)
    client.hospital_id = hospital_id
    client.user_id = user_id
    client.dept_id = dept.id
    return client


def test_mis_filter_doctors_and_departments(mis_client, mis_db):
    res_docs = mis_client.get("/mis/filters/doctors")
    assert res_docs.status_code == 200
    docs = res_docs.json()
    assert len(docs) == 1
    assert docs[0]["name"] == "Doctor MIS"

    res_depts = mis_client.get("/mis/filters/departments")
    assert res_depts.status_code == 200
    depts = res_depts.json()
    assert len(depts) == 1
    assert depts[0]["name"] == "Cardiology"


def test_mis_patient_reports(mis_client, mis_db):
    patient = Patient(
        id=uuid4(),
        hospital_id=mis_client.hospital_id,
        uhid="P0001",
        first_name="Jane",
        last_name="Doe",
        name="Jane Doe",
        gender="female",
        date_of_birth=date(1990, 5, 15),
        mobile="9876543210",
        status=PatientStatus.active,
    )
    mis_db.add(patient)
    mis_db.commit()

    res = mis_client.get("/mis/patients")
    assert res.status_code == 200
    data = res.json()
    assert "metrics" in data
    assert any(m["metric"] == "Total Patients" and m["count"] >= 1 for m in data["metrics"])


def test_mis_appointment_reports(mis_client, mis_db):
    patient = Patient(
        id=uuid4(),
        hospital_id=mis_client.hospital_id,
        uhid="P0002",
        first_name="John",
        last_name="Smith",
        name="John Smith",
        gender="male",
        mobile="9876543211",
        status=PatientStatus.active,
    )
    mis_db.add(patient)
    mis_db.flush()

    appt = Appointment(
        id=uuid4(),
        hospital_id=mis_client.hospital_id,
        patient_id=patient.id,
        doctor_id=mis_client.user_id,
        appointment_date=date.today(),
        appointment_time=datetime.now(timezone.utc).time(),
        purpose="General Consultation",
        status=AppointmentStatus.scheduled,
    )
    mis_db.add(appt)
    mis_db.commit()

    res = mis_client.get("/mis/appointments")
    assert res.status_code == 200
    data = res.json()
    assert "metrics" in data
    assert "by_doctor" in data
    assert any(m["metric"] == "Total Appointments (Range)" and m["count"] >= 1 for m in data["metrics"])
    assert len(data["by_doctor"]) >= 1
    assert data["by_doctor"][0]["name"] == "Doctor MIS"


def test_mis_bed_reports(mis_client, mis_db):
    ward = Ward(
        id=uuid4(),
        hospital_id=mis_client.hospital_id,
        name="General Ward",
        ward_type=WardType.general,
    )
    mis_db.add(ward)
    mis_db.flush()

    room = Room(
        id=uuid4(),
        hospital_id=mis_client.hospital_id,
        ward_id=ward.id,
        room_code="R101",
    )
    mis_db.add(room)
    mis_db.flush()

    bed = Bed(
        id=uuid4(),
        hospital_id=mis_client.hospital_id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="B101",
        is_occupied=False,
    )
    mis_db.add(bed)
    mis_db.commit()

    res = mis_client.get("/mis/beds")
    assert res.status_code == 200
    data = res.json()
    assert "metrics" in data
    assert "by_ward" in data
    assert any(m["metric"] == "Total Beds" and m["count"] >= 1 for m in data["metrics"])
    assert any(m["metric"] == "Available Beds" and m["count"] >= 1 for m in data["metrics"])
    assert len(data["by_ward"]) >= 1
    assert data["by_ward"][0]["ward_name"] == "General Ward"


def test_mis_doctor_reports(mis_client, mis_db):
    res = mis_client.get("/mis/doctors")
    assert res.status_code == 200
    data = res.json()
    assert "metrics" in data
    assert "doctors" in data
    assert any(m["metric"] == "Doctors" and m["count"] >= 1 for m in data["metrics"])
    assert len(data["doctors"]) >= 1
    assert data["doctors"][0]["doctor_name"] == "Doctor MIS"


def test_mis_daily_summary(mis_client, mis_db):
    res = mis_client.get("/mis/daily-summary")
    assert res.status_code == 200
    data = res.json()
    assert "summary_date" in data
    assert "appointments" in data
    assert "occupied_beds" in data
    assert "revenue" in data
    assert "metrics" in data
