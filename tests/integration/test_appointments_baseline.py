"""
Automated baseline tests for the legacy appointments router (app.routers.appointment).

Establishes behavioral truth for routes, status transitions, token allocation,
cancellation, and tenant isolation before cutover.
"""

from datetime import date, datetime, time, timedelta
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Appointment,
    AppointmentStatus,
    Hospital,
    HospitalUser,
    Patient,
    StaffRole,
)
from app.routers import appointment


@pytest.fixture(scope="function")
def appointments_app(db_session: Session) -> FastAPI:
    """Test app mounting legacy appointment router."""
    test_app = FastAPI(title="HMS Appointments Baseline Test App")
    test_app.include_router(appointment.router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = _override_get_db
    return test_app


@pytest.fixture(scope="function")
def client(appointments_app: FastAPI) -> TestClient:
    return TestClient(appointments_app)


@pytest.fixture(scope="function")
def doctor_with_role(db_session: Session, hospital: Hospital) -> HospitalUser:
    """Doctor with a properly named role."""
    role = StaffRole(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Doctor",
        description="Medical Doctor",
    )
    db_session.add(role)
    db_session.commit()

    doc = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=role.id,
        name="Dr. Gregory House",
        phone="555-0199",
        email="ghouse@apollocity.com",
        password_hash="dummy_hash",
        specialization="Diagnostics",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


def test_list_doctors_endpoint(client: TestClient, auth_headers: dict[str, str], doctor_with_role: HospitalUser):
    res = client.get("/api/appointments/doctors", headers=auth_headers)
    assert res.status_code == 200
    docs = res.json()
    assert len(docs) >= 1
    assert any(d["name"] == "Dr. Gregory House" for d in docs)


def test_book_appointment_existing_patient(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital: Hospital,
    doctor_with_role: HospitalUser,
    patient: Patient,
):
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor_with_role.id),
        "appointment_date": (date.today() + timedelta(days=2)).isoformat(),
        "appointment_time": "10:00:00",
        "visit_type": "OPD",
        "booking_kind": "future",
        "purpose": "Routine Checkup",
    }
    res = client.post("/api/appointments", json=payload, headers=auth_headers)
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "scheduled"
    assert data["patient_id"] == str(patient.id)
    assert data["doctor_id"] == str(doctor_with_role.id)
    assert data["queue_token"] is None  # future booking has no queue token until check-in


def test_book_appointment_walk_in_booking_kind(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital: Hospital,
    doctor_with_role: HospitalUser,
    patient: Patient,
):
    future_time = (datetime.now() + timedelta(hours=2)).strftime("%H:%M:00")
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor_with_role.id),
        "appointment_date": date.today().isoformat(),
        "appointment_time": future_time,
        "visit_type": "OPD",
        "booking_kind": "walk_in",
        "purpose": "Walk-in Consultation",
    }
    res = client.post("/api/appointments", json=payload, headers=auth_headers)
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "scheduled"  # remains scheduled until check-in / vitals
    assert data["booking_kind"] == "walk_in"


def test_check_in_appointment_lifecycle(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital: Hospital,
    appointment_factory,
):
    appt = appointment_factory(
        status=AppointmentStatus.scheduled,
        appointment_date=date.today(),
        appointment_time=time(14, 0),
    )
    res = client.post(
        f"/api/appointments/{appt.id}/check-in",
        headers=auth_headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "waiting"
    assert data["queue_token"] == 1
    assert data["checked_in_at"] is not None


def test_cancel_appointment_endpoint(
    client: TestClient,
    auth_headers: dict[str, str],
    appointment_factory,
):
    appt = appointment_factory(
        status=AppointmentStatus.scheduled,
        appointment_date=date.today() + timedelta(days=1),
        appointment_time=time(10, 0),
    )
    res = client.post(
        f"/api/appointments/{appt.id}/cancel",
        json={"reason": "Patient requested reschedule"},
        headers=auth_headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "cancelled"


def test_no_show_appointment_endpoint(
    client: TestClient,
    auth_headers: dict[str, str],
    appointment_factory,
):
    appt = appointment_factory(
        status=AppointmentStatus.scheduled,
        appointment_date=date.today(),
        appointment_time=time(9, 0),
    )
    res = client.post(
        f"/api/appointments/{appt.id}/no-show",
        headers=auth_headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "no_show"


def test_reschedule_appointment_endpoint(
    client: TestClient,
    auth_headers: dict[str, str],
    appointment_factory,
):
    appt = appointment_factory(
        status=AppointmentStatus.scheduled,
        appointment_date=date.today() + timedelta(days=1),
        appointment_time=time(9, 30),
    )
    new_date = (date.today() + timedelta(days=3)).isoformat()
    res = client.put(
        f"/api/appointments/{appt.id}/reschedule",
        json={"appointment_date": new_date, "appointment_time": "15:00:00"},
        headers=auth_headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["appointment_date"] == new_date
    assert data["appointment_time"] == "15:00:00"


def test_tenant_isolation_appointments(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital_b: Hospital,
    doctor_b: HospitalUser,
    patient_b: Patient,
    appointment_factory,
):
    """User in Hospital A cannot view or manipulate Hospital B appointments."""
    appt_b = appointment_factory(
        custom_hospital=hospital_b,
        custom_doctor=doctor_b,
        custom_patient=patient_b,
        status=AppointmentStatus.scheduled,
    )
    res = client.post(
        f"/api/appointments/{appt_b.id}/check-in",
        headers=auth_headers,
    )
    assert res.status_code == 404
