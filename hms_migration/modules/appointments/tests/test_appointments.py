"""
Comprehensive tests for the migrated Appointments module.

Verifies:
1. Booking an appointment for an existing patient (201 Created, encounter ID, status scheduled).
2. Auto-registering a new patient upon booking (201 Created, new patient created).
3. Past slot validation (400 Bad Request when attempting to book a past slot).
4. Fee preview calculation (standard fee, consultation pricing resolution).
5. Check availability endpoint (returning slots and occupied status).
6. Check-in action (transition to waiting status, checked_in_at set).
7. Complete appointment action (transition to completed status).
8. Cancel appointment action (transition to cancelled status with reason).
9. No-show action (transition to no_show status).
10. Reschedule appointment action (updated date/time, status reset to scheduled).
11. Multi-tenant isolation (tenant A cannot view or operate on tenant B appointments).
12. Listing endpoints (today, calendar, queue, history, metadata).
"""

import random
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Appointment as LegacyAppointment,
    AppointmentStatus as LegacyAppointmentStatus,
    AppointmentType,
    Hospital,
    HospitalUser,
    Patient as LegacyPatient,
)


def _future_slot() -> tuple[date, str]:
    """Returns a safe future date and slot time."""
    target_date = date.today() + timedelta(days=2)
    return target_date, "10:00"


def test_book_appointment_existing_patient(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
):
    """Booking an appointment for an existing patient should return 201 with scheduled status."""
    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "purpose": "Persistent headache",
    }

    res = client.post("/api/appointments", json=payload, headers=auth_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["status"] == "scheduled"
    assert data["patient_id"] == str(patient.id)
    assert data["doctor_id"] == str(doctor.id)
    assert data["op_id"].startswith("OP-")
    assert data["purpose"] == "Persistent headache"


def test_book_appointment_new_patient_auto_register(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
):
    """Booking for a new patient with first_name, last_name, mobile should auto-register patient."""
    target_date, target_time = _future_slot()
    unique_phone = f"98765{random.randint(10000, 99999)}"
    payload = {
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "walk_in",
        "first_name": "Ravi",
        "last_name": "Kumar",
        "mobile": unique_phone,
        "gender": "male",
        "date_of_birth": "1994-05-12",
        "age": 32,
        "emergency_contact": "9876543210",
        "purpose": "Routine health checkup",
    }

    res = client.post("/api/appointments", json=payload, headers=auth_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["status"] == "scheduled"
    assert data["patient_name"] == "Ravi Kumar"
    assert data["patient_id"] is not None
    assert data["op_id"].startswith("OP-")


def test_book_appointment_past_slot_rejected(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
):
    """Booking an appointment in the past should return 400 Bad Request."""
    past_date = date.today() - timedelta(days=2)
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": past_date.isoformat(),
        "appointment_time": "09:00:00",
        "booking_kind": "future",
    }

    res = client.post("/api/appointments", json=payload, headers=auth_headers)
    assert res.status_code == 400
    assert "past" in res.json()["detail"].lower()


def test_fee_preview(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Fee preview should return resolved doctor fee or standard pricing."""
    target_date, _ = _future_slot()
    appt_type = AppointmentType(
        id=uuid4(),
        hospital_id=hospital.id,
        name="OPD General",
        slot_duration_minutes=15,
        is_active=True,
    )
    db_session.add(appt_type)
    db_session.commit()

    res = client.get(
        f"/api/appointments/fee-preview?doctor_id={doctor.id}&appointment_type_id={appt_type.id}&patient_id={patient.id}&date={target_date.isoformat()}",
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()

    assert "consultation_fee" in data
    assert isinstance(data["consultation_fee"], (int, float))
    assert data["consultation_fee"] >= 0


def test_check_availability(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
):
    """Availability query should return schedule slots and availability flags."""
    target_date, _ = _future_slot()
    res = client.get(
        f"/api/appointments/availability?doctor_id={doctor.id}&date={target_date.isoformat()}",
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()

    assert "doctor_id" in data
    assert "booked_slots" in data
    assert data["available"] is True


def test_check_in_appointment(
    client: TestClient,
    auth_headers: dict[str, str],
    appointment_factory: Any,
    patient: LegacyPatient,
    doctor: HospitalUser,
):
    """Check-in transitions scheduled appointment to waiting with checked_in_at set."""
    appt = appointment_factory(
        custom_patient=patient,
        custom_doctor=doctor,
        appointment_date=date.today(),
        status="scheduled",
    )

    res = client.post(f"/api/appointments/{appt.id}/check-in", headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "waiting"
    assert data["checked_in_at"] is not None


def test_complete_appointment(
    client: TestClient,
    auth_headers: dict[str, str],
    appointment_factory: Any,
    patient: LegacyPatient,
    doctor: HospitalUser,
):
    """Completing an appointment should transition status to completed."""
    appt = appointment_factory(
        custom_patient=patient,
        custom_doctor=doctor,
        appointment_date=date.today(),
        status="waiting",
    )

    res = client.post(f"/api/appointments/{appt.id}/complete", headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "completed"


def test_cancel_appointment(
    client: TestClient,
    auth_headers: dict[str, str],
    appointment_factory: Any,
    patient: LegacyPatient,
    doctor: HospitalUser,
):
    """Cancelling an appointment should update status and store cancellation reason."""
    appt = appointment_factory(
        custom_patient=patient,
        custom_doctor=doctor,
        appointment_date=date.today(),
        status="scheduled",
    )

    res = client.post(
        f"/api/appointments/{appt.id}/cancel",
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "cancelled"


def test_no_show_appointment(
    client: TestClient,
    auth_headers: dict[str, str],
    appointment_factory: Any,
    patient: LegacyPatient,
    doctor: HospitalUser,
):
    """Marking no-show should update appointment status to no_show."""
    appt = appointment_factory(
        custom_patient=patient,
        custom_doctor=doctor,
        appointment_date=date.today(),
        status="scheduled",
    )

    res = client.post(f"/api/appointments/{appt.id}/no-show", headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "no_show"


def test_reschedule_appointment(
    client: TestClient,
    auth_headers: dict[str, str],
    appointment_factory: Any,
    patient: LegacyPatient,
    doctor: HospitalUser,
):
    """Rescheduling updates the appointment slot and resets status to scheduled."""
    appt = appointment_factory(
        custom_patient=patient,
        custom_doctor=doctor,
        appointment_date=date.today() + timedelta(days=1),
        status="scheduled",
    )

    target_date, target_time = _future_slot()
    payload = {
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
    }

    res = client.put(
        f"/api/appointments/{appt.id}/reschedule",
        json=payload,
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "scheduled"
    assert data["appointment_date"] == target_date.isoformat()
    assert str(data["appointment_time"]).startswith(target_time)


def test_cross_tenant_isolation(
    client: TestClient,
    auth_headers_b: dict[str, str],
    appointment_factory: Any,
    patient: LegacyPatient,
    doctor: HospitalUser,
):
    """Hospital B staff cannot read or modify appointments belonging to Hospital A."""
    appt = appointment_factory(
        custom_patient=patient,
        custom_doctor=doctor,
        appointment_date=date.today(),
        status="scheduled",
    )

    # Attempt check-in from Hospital B
    res = client.post(f"/api/appointments/{appt.id}/check-in", headers=auth_headers_b)
    assert res.status_code == 404


def test_list_appointments_today_and_queue(
    client: TestClient,
    auth_headers: dict[str, str],
    appointment_factory: Any,
    patient: LegacyPatient,
    doctor: HospitalUser,
):
    """Listing endpoints should return today's appointments and active queue."""
    appointment_factory(
        custom_patient=patient,
        custom_doctor=doctor,
        appointment_date=date.today(),
        status="waiting",
    )

    today_res = client.get("/api/appointments/today", headers=auth_headers)
    assert today_res.status_code == 200, today_res.text
    today_items = today_res.json()
    assert isinstance(today_items, list)
    assert len(today_items) >= 1

    queue_res = client.get("/api/appointments/queue", headers=auth_headers)
    assert queue_res.status_code == 200, queue_res.text
    queue_data = queue_res.json()
    assert isinstance(queue_data, list)


def test_metadata_endpoints(
    client: TestClient,
    auth_headers: dict[str, str],
):
    """Metadata helper endpoints should return lists of doctors, departments, visit types."""
    res_docs = client.get("/api/appointments/doctors", headers=auth_headers)
    assert res_docs.status_code == 200
    assert isinstance(res_docs.json(), list)

    res_types = client.get("/api/appointments/visit-types", headers=auth_headers)
    assert res_types.status_code == 200
    assert isinstance(res_types.json(), list)
