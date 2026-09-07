"""
Comprehensive unit and integration tests for Tenancy and Hospitals domain.
Tests:
- POST /hospitals
- GET /hospitals
- GET /hospitals/{hospital_uuid}
- DELETE /hospitals/{hospital_uuid}
- GET /hospitals/me/dashboard
- GET /hospitals/me/role-dashboard (doctor, nurse, reception, lab, radiology, ot, billing, admin)
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.beds.entities.bed import Bed, Room, Ward, WardType
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingPayment,
    BillingSourceType,
)
from hms_migration.modules.doctors.entities.doctor import HospitalUser, StaffRole
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.laboratory.entities.lab_entities import LabOrder, LabOrderStatus
from hms_migration.modules.masters.entities.organization_entities import Department
from hms_migration.modules.ot.entities.ot_entities import OtRoom, OtSurgery, OtSurgeryStatus
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus
from hms_migration.modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
)
from hms_migration.modules.tenancy.api.tenancy_api import router as tenancy_router
from hms_migration.modules.tenancy.entities.hospital import Hospital, PlanType
from hms_migration.shared.auth import (
    get_hospital_context,
    require_hospital_user,
    require_super_admin,
)


@pytest.fixture
def tenancy_db():
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
def tenancy_client(tenancy_db):
    hospital_id = uuid4()
    admin_user_id = uuid4()

    hospital = Hospital(
        id=hospital_id,
        hospital_id="HOSP-TENANT-01",
        name="Tenancy Test Hospital",
        address="456 Healthcare Blvd",
        phone="9876543210",
        email="info@tenancytest.com",
        password_hash="fakehash",
        plan=PlanType.basic,
        is_active=True,
    )
    tenancy_db.add(hospital)
    tenancy_db.commit()

    app = FastAPI()
    app.include_router(tenancy_router)

    app.dependency_overrides[get_transitional_sync_session] = lambda: tenancy_db
    app.dependency_overrides[get_hospital_context] = lambda: hospital_id
    app.dependency_overrides[require_super_admin] = lambda: {"sub": "superadmin@hms.com", "role": "super_admin"}

    current_user = {
        "user_id": str(admin_user_id),
        "hospital_id": str(hospital_id),
        "role": "hospital_admin",
        "staff_role_name": "Hospital Admin",
        "name": "Super Hospital Admin",
    }
    app.dependency_overrides[require_hospital_user] = lambda: current_user

    client = TestClient(app)
    client.hospital_id = hospital_id
    client.admin_user_id = admin_user_id
    client.app = app
    return client


def test_hospitals_crud(tenancy_client):
    # 1. Create hospital
    payload = {
        "name": "Apex Medicare",
        "address": "789 Medical Lane",
        "phone": "9812345678",
        "email": "admin@apexmedicare.com",
        "plan": "premium",
    }
    create_res = tenancy_client.post("/hospitals", json=payload)
    assert create_res.status_code == 201
    hosp = create_res.json()
    assert hosp["name"] == "Apex Medicare"
    assert "generated_password" in hosp
    hosp_id = hosp["id"]

    # 2. Duplicate email returns 409
    dup_res = tenancy_client.post("/hospitals", json=payload)
    assert dup_res.status_code == 409

    # 3. List hospitals
    list_res = tenancy_client.get("/hospitals")
    assert list_res.status_code == 200
    hosps = list_res.json()
    assert any(h["id"] == hosp_id for h in hosps)

    # 4. Search hospital
    search_res = tenancy_client.get("/hospitals", params={"search": "Apex"})
    assert search_res.status_code == 200
    assert len(search_res.json()) >= 1

    # 5. Get hospital by ID
    get_res = tenancy_client.get(f"/hospitals/{hosp_id}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Apex Medicare"

    # 6. Delete hospital
    del_res = tenancy_client.delete(f"/hospitals/{hosp_id}")
    assert del_res.status_code == 204

    # Verify 404 after delete
    get_after = tenancy_client.get(f"/hospitals/{hosp_id}")
    assert get_after.status_code == 404


def test_hospital_dashboard(tenancy_client, tenancy_db):
    h_id = tenancy_client.hospital_id

    # Create dummy patient, appointment, admission
    patient = Patient(
        id=uuid4(),
        hospital_id=h_id,
        uhid="UHID-001",
        name="John Patient",
        gender="male",
        date_of_birth=date(1990, 1, 1),
        mobile="9876543210",
        status=PatientStatus.active,
    )
    role = StaffRole(id=uuid4(), hospital_id=h_id, name="General Physician")
    doctor = HospitalUser(
        id=uuid4(),
        hospital_id=h_id,
        role_id=role.id,
        name="Dr. Smith",
        phone="9876543211",
        email="dr.smith@test.com",
        password_hash="hash",
        is_active=True,
    )
    appt = Appointment(
        id=uuid4(),
        hospital_id=h_id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        purpose="Regular Checkup",
        status=AppointmentStatus.scheduled,
    )
    tenancy_db.add_all([patient, role, doctor, appt])
    tenancy_db.commit()

    res = tenancy_client.get("/hospitals/me/dashboard")
    assert res.status_code == 200
    dash = res.json()
    assert dash["name"] == "Tenancy Test Hospital"
    assert dash["patient_count"] >= 1
    assert dash["appointments_today"] >= 1
    assert len(dash["upcoming_appointments"]) >= 1


def test_role_dashboards(tenancy_client, tenancy_db):
    h_id = tenancy_client.hospital_id

    # Test Doctor persona
    doc_id = uuid4()
    tenancy_client.app.dependency_overrides[require_hospital_user] = lambda: {
        "user_id": str(doc_id),
        "hospital_id": str(h_id),
        "role": "hospital_staff",
        "staff_role_name": "Senior Doctor",
        "name": "Dr. Sarah",
    }
    doc_res = tenancy_client.get("/hospitals/me/role-dashboard")
    assert doc_res.status_code == 200
    assert doc_res.json()["persona"] == "doctor"

    # Test Nurse persona
    tenancy_client.app.dependency_overrides[require_hospital_user] = lambda: {
        "user_id": str(uuid4()),
        "hospital_id": str(h_id),
        "role": "hospital_staff",
        "staff_role_name": "Head Nurse",
        "name": "Nurse Joy",
    }
    nurse_res = tenancy_client.get("/hospitals/me/role-dashboard")
    assert nurse_res.status_code == 200
    assert nurse_res.json()["persona"] == "nurse"

    # Test Reception persona
    tenancy_client.app.dependency_overrides[require_hospital_user] = lambda: {
        "user_id": str(uuid4()),
        "hospital_id": str(h_id),
        "role": "hospital_staff",
        "staff_role_name": "Front Desk Receptionist",
        "name": "Alex Reception",
    }
    rec_res = tenancy_client.get("/hospitals/me/role-dashboard")
    assert rec_res.status_code == 200
    assert rec_res.json()["persona"] == "reception"

    # Test Lab persona
    tenancy_client.app.dependency_overrides[require_hospital_user] = lambda: {
        "user_id": str(uuid4()),
        "hospital_id": str(h_id),
        "role": "hospital_staff",
        "staff_role_name": "Laboratory Technician",
        "name": "Sam Lab",
    }
    lab_res = tenancy_client.get("/hospitals/me/role-dashboard")
    assert lab_res.status_code == 200
    assert lab_res.json()["persona"] == "lab"

    # Test Radiology persona
    tenancy_client.app.dependency_overrides[require_hospital_user] = lambda: {
        "user_id": str(uuid4()),
        "hospital_id": str(h_id),
        "role": "hospital_staff",
        "staff_role_name": "Radiology Specialist",
        "name": "Taylor Rad",
    }
    rad_res = tenancy_client.get("/hospitals/me/role-dashboard")
    assert rad_res.status_code == 200
    assert rad_res.json()["persona"] == "radiology"

    # Test OT persona
    tenancy_client.app.dependency_overrides[require_hospital_user] = lambda: {
        "user_id": str(uuid4()),
        "hospital_id": str(h_id),
        "role": "hospital_staff",
        "staff_role_name": "OT Staff Incharge",
        "name": "Morgan OT",
    }
    ot_res = tenancy_client.get("/hospitals/me/role-dashboard")
    assert ot_res.status_code == 200
    assert ot_res.json()["persona"] == "ot"

    # Test Billing persona
    tenancy_client.app.dependency_overrides[require_hospital_user] = lambda: {
        "user_id": str(uuid4()),
        "hospital_id": str(h_id),
        "role": "hospital_staff",
        "staff_role_name": "Cashier Billing Officer",
        "name": "Jordan Billing",
    }
    bill_res = tenancy_client.get("/hospitals/me/role-dashboard")
    assert bill_res.status_code == 200
    assert bill_res.json()["persona"] == "billing"

    # Test Admin persona
    tenancy_client.app.dependency_overrides[require_hospital_user] = lambda: {
        "user_id": str(uuid4()),
        "hospital_id": str(h_id),
        "role": "hospital_admin",
        "name": "Admin User",
    }
    admin_res = tenancy_client.get("/hospitals/me/role-dashboard")
    assert admin_res.status_code == 200
    assert admin_res.json()["persona"] == "admin"
