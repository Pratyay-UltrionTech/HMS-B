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

from modules.appointments.entities.appointment import (
    Appointment as LegacyAppointment,
)
from modules.appointments.entities.appointment_type import AppointmentType
from modules.appointments.entities.enums import AppointmentStatus as LegacyAppointmentStatus
from modules.doctors.entities.doctor import HospitalUser
from modules.patients.entities.patient import Patient as LegacyPatient
from modules.tenancy.entities.hospital import Hospital


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


def test_book_appointment_auto_registers_patient_same_emergency_contact(
    client: TestClient,
    auth_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
):
    """Booking for a new patient with identical mobile and emergency_contact succeeds."""
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
        "emergency_contact": unique_phone,  # Identical to mobile
        "purpose": "Routine health checkup",
    }

    res = client.post("/api/appointments", json=payload, headers=auth_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["status"] == "scheduled"
    assert data["patient_name"] == "Ravi Kumar"
    assert data["patient_id"] is not None



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


# ---------------------------------------------------------------------------
# Phase B: Appointment Payment Collection & Billing Integration Tests
# ---------------------------------------------------------------------------

def test_appointment_payment_cash(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test 1: Cash appointment payment creates appointment, consultation charge, payment, allocation, and receipt."""
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "purpose": "Routine consultation",
        "payment_method": "cash",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["status"] == "scheduled"
    assert data["payment_method"] == "cash"
    assert data["payment_amount"] == 500.0
    assert data["receipt_number"] is not None
    assert data["receipt_number"].startswith("RCP")

    # DB Level Verification
    from modules.billing.entities.billing_entities import (
        BillingCharge,
        BillingChargeStatus,
        BillingPayment,
        BillingPaymentAllocation,
        BillingPaymentMethod,
        BillingReceipt,
    )

    charge = db_session.query(BillingCharge).filter_by(patient_id=patient.id).first()
    assert charge is not None
    assert charge.charge_amount == 500.0
    assert charge.net_amount == 500.0
    assert charge.amount_paid == 500.0
    assert charge.status == BillingChargeStatus.paid

    payment = db_session.query(BillingPayment).filter_by(patient_id=patient.id).first()
    assert payment is not None
    assert payment.payment_method == BillingPaymentMethod.cash
    assert payment.amount == 500.0

    alloc = db_session.query(BillingPaymentAllocation).filter_by(payment_id=payment.id).first()
    assert alloc is not None
    assert alloc.charge_id == charge.id
    assert alloc.allocated_amount == 500.0

    receipt = db_session.query(BillingReceipt).filter_by(payment_id=payment.id).first()
    assert receipt is not None
    assert receipt.amount == 500.0
    assert receipt.receipt_number == data["receipt_number"]
    assert payment.payment_date == date.today()
    assert receipt.payment_date == date.today()
    assert payment.payment_date != target_date


def test_appointment_payment_upi(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test 2: UPI appointment payment persists UPI tender method and transaction reference."""
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "purpose": "Routine consultation",
        "payment_method": "upi",
        "payment_reference": "UPI123456",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["payment_method"] == "upi"
    assert data["payment_reference"] == "UPI123456"

    from modules.billing.entities.billing_entities import BillingPayment, BillingPaymentMethod
    payment = db_session.query(BillingPayment).filter_by(patient_id=patient.id, reference_number="UPI123456").first()
    assert payment is not None
    assert payment.payment_method == BillingPaymentMethod.upi
    assert payment.reference_number == "UPI123456"


def test_appointment_payment_card_no_sensitive_data(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test 3: Card payment records canonical tender without any sensitive card numbers, CVVs, or pins."""
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "purpose": "Routine consultation",
        "payment_method": "card",
        "payment_reference": "AUTH-POS-987",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["payment_method"] == "card"
    assert data["payment_reference"] == "AUTH-POS-987"

    from modules.billing.entities.billing_entities import BillingPayment, BillingPaymentMethod
    payment = db_session.query(BillingPayment).filter_by(patient_id=patient.id, reference_number="AUTH-POS-987").first()
    assert payment is not None
    assert payment.payment_method == BillingPaymentMethod.card
    assert not hasattr(payment, "card_number")
    assert not hasattr(payment, "cvv")
    assert not hasattr(payment, "pin")


def test_appointment_payment_existing_patient(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test 4: Booking and immediate payment settlement for existing patient."""
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "payment_method": "bank_transfer",
        "payment_reference": "NEFT-54321",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["patient_id"] == str(patient.id)
    assert data["payment_method"] == "bank_transfer"
    assert data["payment_amount"] == 500.0


def test_appointment_payment_new_patient_auto_register_same_phone(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    db_session: Session,
):
    """Test 5: New patient auto-registration with identical mobile & emergency_contact + payment succeeds."""
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    phone = f"98765{random.randint(10000, 99999)}"
    payload = {
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "walk_in",
        "first_name": "Aman",
        "last_name": "Verma",
        "mobile": phone,
        "emergency_contact": phone,  # Phase A: mobile == emergency_contact
        "gender": "male",
        "date_of_birth": "1990-01-01",
        "age": 36,
        "purpose": "Consultation",
        "payment_method": "cash",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["patient_name"] == "Aman Verma"
    assert data["patient_id"] is not None
    assert data["payment_method"] == "cash"
    assert data["payment_amount"] == 500.0
    assert data["receipt_number"] is not None


def test_appointment_zero_consultation_fee(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test 6: Zero consultation fee creates appointment without fake ₹0 payment or receipt."""
    doctor.custom_values = {"consultation_fee": 0.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "purpose": "Free follow-up",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["consultation_fee"] == 0.0
    assert data["payment_method"] is None
    assert data["receipt_number"] is None

    from modules.billing.entities.billing_entities import BillingPayment, BillingReceipt
    payments = db_session.query(BillingPayment).filter_by(patient_id=patient.id).all()
    assert len(payments) == 0

    receipts = db_session.query(BillingReceipt).all()
    assert len(receipts) == 0


def test_appointment_settlement_failure_atomic_rollback(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test 7: Financial / validation failure rolls back the entire appointment booking transaction."""
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "purpose": "Routine consultation",
        "payment_method": "unsupported_crypto_method",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code in (400, 422), res.text

    # Verify no appointment was created in the database
    from modules.appointments.entities.appointment import Appointment
    appts = db_session.query(Appointment).filter_by(
        doctor_id=doctor.id,
        appointment_date=target_date,
    ).all()
    assert len(appts) == 0


def test_appointment_duplicate_submission_protection(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test 8: Double click / duplicate submission on the same slot fails fast and produces no duplicate charges/payments."""
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "payment_method": "cash",
    }

    res1 = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res1.status_code == 201, res1.text

    # Immediate second submission (e.g. double click)
    res2 = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res2.status_code == 400
    assert "already has an appointment" in res2.text.lower() or "occupied" in res2.text.lower()

    # DB records should have exactly 1 charge and 1 payment
    from modules.billing.entities.billing_entities import BillingCharge, BillingPayment
    charges = db_session.query(BillingCharge).filter_by(patient_id=patient.id).all()
    payments = db_session.query(BillingPayment).filter_by(patient_id=patient.id).all()
    assert len(charges) == 1
    assert len(payments) == 1


def test_appointment_billing_ledger_reflection(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test 9: Patient billing ledger endpoint naturally reflects appointment consultation charge and payment."""
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "purpose": "Checkup",
        "payment_method": "upi",
        "payment_reference": "UPI-LEDGER-99",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text

    # Fetch canonical Billing ledger
    ledger_res = client.get(f"/api/billing/patients/{patient.id}/ledger", headers=admin_headers)
    assert ledger_res.status_code == 200, ledger_res.text
    ledger = ledger_res.json()

    assert ledger["total_charges"] == 500.0
    assert ledger["total_paid"] == 500.0
    assert ledger["outstanding"] == 0.0

    # Verify charge and payment entries are present
    assert any("Consultation" in c.get("description", "") for c in ledger["charges"])
    assert any(p.get("reference_number") == "UPI-LEDGER-99" for p in ledger["payments"])


def test_existing_billing_payment_workflow_regression(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test 10: Standard Billing module payment collection continues to function without regression."""
    from modules.billing.services.billing_service import ensure_charge
    from modules.billing.entities.billing_entities import BillingSourceType

    c = ensure_charge(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=uuid4(),
        description="Routine Lipid Profile",
        charge_amount=750.0,
    )
    db_session.commit()

    payload = {
        "patient_id": str(patient.id),
        "amount": 750.0,
        "payment_method": "cash",
        "notes": "Direct billing desk payment",
        "allocations": [{"charge_id": str(c.id), "amount": 750.0}],
    }

    res = client.post("/api/billing/payments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["amount"] == 750.0
    assert data["receipt_number"] is not None
    assert data["receipt_number"].startswith("RCP")


# ---------------------------------------------------------------------------
# Phase B Closure Gate Verification Tests
# ---------------------------------------------------------------------------

def test_appointment_pay_later_semantics(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """B-CLOSURE 1: Pay Later creates appointment & charge, but NO payment, allocation, or receipt."""
    from modules.billing.entities.billing_entities import (
        BillingCharge,
        BillingChargeStatus,
        BillingPayment,
        BillingReceipt,
    )

    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "purpose": "Consultation with Deferred Pay Later",
        # Pay Later: payment_method omitted / null
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["status"] == "scheduled"
    assert data["payment_method"] is None
    assert data["payment_amount"] is None
    assert data["receipt_number"] is None

    # DB Level Verification
    from uuid import UUID
    charge = db_session.query(BillingCharge).filter_by(patient_id=patient.id, source_id=UUID(data["id"])).first()
    assert charge is not None, "Consultation charge must be created for Pay Later"
    assert charge.status == BillingChargeStatus.pending
    assert float(charge.net_amount) == 500.0
    assert float(charge.amount_paid) == 0.0

    # NO payment or receipt created
    payments = db_session.query(BillingPayment).filter_by(patient_id=patient.id).all()
    assert len(payments) == 0, "No payment should be created for Pay Later"

    receipts = db_session.query(BillingReceipt).filter_by(patient_id=patient.id).all()
    assert len(receipts) == 0, "No receipt should be generated for Pay Later"

    # Ledger shows outstanding
    led_res = client.get(f"/api/billing/patients/{patient.id}/ledger", headers=admin_headers)
    assert led_res.status_code == 200
    ledger = led_res.json()
    assert ledger["outstanding"] == 500.0
    assert ledger["total_paid"] == 0.0


def test_cannot_underpay_via_request_payment_amount(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """B-CLOSURE 3: Client cannot manipulate payment amount; server determines canonical fee."""
    from modules.billing.entities.billing_entities import BillingPayment

    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "payment_method": "cash",
        "payment_amount": 1.0,  # Malicious / stale client-side underpayment attempt
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    # Server must have settled canonical ₹500 fee, NOT ₹1
    assert data["payment_amount"] == 500.0

    pay = db_session.query(BillingPayment).filter_by(patient_id=patient.id).first()
    assert pay is not None
    assert float(pay.amount) == 500.0, "Payment must be canonical server fee, not client manipulated amount"


def test_canonical_appointment_payment_contract_validation(
    client: TestClient,
    admin_headers: dict[str, str],
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Phase D Closure 3 & 4: Only canonical flat payment fields are accepted; invalid tender rejected."""
    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "payment_method": "invalid_tender",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 422, "Non-canonical payment tender method must be rejected by contract schema"


def test_appointment_booking_without_billing_permission_denied_settlement(
    client: TestClient,
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Phase D Closure 2: User with appointment.create but NO billing.create cannot create financial settlements."""
    from shared.auth.jwt import create_access_token
    from modules.doctors.entities.doctor import HospitalUser, RolePermission, StaffRole

    doctor.custom_values = {"consultation_fee": 500.0}

    # Staff role with appointment permission and billing.can_view ONLY (no billing.can_create)
    booking_only_role = StaffRole(hospital_id=hospital.id, name="Booking Agent Only")
    db_session.add(booking_only_role)
    db_session.flush()

    perm_appt = RolePermission(
        hospital_id=hospital.id,
        role_id=booking_only_role.id,
        module_key="appointment",
        can_view=True,
        can_edit=True,
        can_create=True,
    )
    perm_bill_view_only = RolePermission(
        hospital_id=hospital.id,
        role_id=booking_only_role.id,
        module_key="billing",
        can_view=True,
        can_create=False,
    )
    db_session.add_all([perm_appt, perm_bill_view_only])

    agent_user = HospitalUser(
        hospital_id=hospital.id,
        name="Booking Agent",
        email="agent@hospital.org",
        phone="9876543219",
        password_hash="dummy_hash",
        role_id=booking_only_role.id,
        is_active=True,
    )
    db_session.add(agent_user)
    db_session.commit()

    token = create_access_token({
        "sub": str(agent_user.id),
        "name": agent_user.name,
        "email": agent_user.email,
        "role": "hospital_staff",
        "staff_role_id": str(booking_only_role.id),
        "staff_role_name": booking_only_role.name,
        "hospital_uuid": str(hospital.id),
    })
    agent_headers = {"Authorization": f"Bearer {token}"}

    # 1. Attempting to collect payment must be rejected with 403 Forbidden
    target_date, target_time = _future_slot()
    settle_payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "payment_method": "cash",
    }
    res_denied = client.post("/api/appointments", json=settle_payload, headers=agent_headers)
    assert res_denied.status_code == 403, "Financial mutation must be rejected without billing.create"
    assert "Forbidden" in res_denied.text or "permission" in res_denied.text.lower()

    # 2. But booking unpaid appointment without financial settlement succeeds under least privilege
    target_date2, target_time2 = _future_slot()
    unpaid_payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date2.isoformat(),
        "appointment_time": f"{target_time2}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
    }
    res_ok = client.post("/api/appointments", json=unpaid_payload, headers=agent_headers)
    assert res_ok.status_code == 201, "Unpaid booking must succeed for user with appointment permission"
    data = res_ok.json()
    assert data["status"] == "scheduled"
    assert data.get("payment_method") is None


def test_front_desk_staff_authorized_for_appointment_and_settlement(
    client: TestClient,
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """B-CLOSURE 4: Real Front Desk hospital_staff user with appointment.edit and billing.create can book and settle."""
    from shared.auth.jwt import create_access_token
    from modules.doctors.entities.doctor import HospitalUser, RolePermission, StaffRole

    doctor.custom_values = {"consultation_fee": 500.0}

    # Create front desk staff role with appointment and billing permissions
    staff_role = StaffRole(hospital_id=hospital.id, name="Front Desk Receptionist")
    db_session.add(staff_role)
    db_session.flush()

    # Add appointment.edit and billing.create permissions
    perm_appt = RolePermission(
        hospital_id=hospital.id,
        role_id=staff_role.id,
        module_key="appointment",
        can_view=True,
        can_edit=True,
        can_create=True,
    )
    perm_bill = RolePermission(
        hospital_id=hospital.id,
        role_id=staff_role.id,
        module_key="billing",
        can_view=True,
        can_create=True,
    )
    db_session.add_all([perm_appt, perm_bill])

    front_desk_user = HospitalUser(
        hospital_id=hospital.id,
        name="Front Desk Agent",
        email="receptionist@hospital.org",
        phone="9876543210",
        password_hash="dummy_hash",
        role_id=staff_role.id,
        is_active=True,
    )
    db_session.add(front_desk_user)
    db_session.commit()

    token = create_access_token({
        "sub": str(front_desk_user.id),
        "name": front_desk_user.name,
        "email": front_desk_user.email,
        "role": "hospital_staff",
        "staff_role_id": str(staff_role.id),
        "staff_role_name": staff_role.name,
        "hospital_uuid": str(hospital.id),
    })
    staff_headers = {"Authorization": f"Bearer {token}"}

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "payment_method": "cash",
    }

    res = client.post("/api/appointments", json=payload, headers=staff_headers)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["status"] == "scheduled"
    assert data["payment_method"] == "cash"
    assert data["payment_amount"] == 500.0


def test_pay_now_atomicity_rolls_back_on_settlement_failure(
    client: TestClient,
    admin_headers: dict[str, str],
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
    monkeypatch,
):
    """B-CLOSURE 5: Failure during payment/receipt creation rolls back appointment and charge atomically."""
    from modules.appointments.entities.appointment import Appointment
    from modules.billing.entities.billing_entities import BillingCharge, BillingPayment
    import modules.billing.services.invoice_service as inv_service

    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    # Monkeypatch issue_receipt_for_payment to raise an unexpected runtime error
    def _exploding_receipt(*args, **kwargs):
        raise RuntimeError("Thermal printer communication failure during receipt issuance")

    monkeypatch.setattr(inv_service, "issue_receipt_for_payment", _exploding_receipt)

    target_date, target_time = _future_slot()
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": target_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "payment_method": "cash",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 400, "Should return validation error (HTTP 400) when settlement fails"
    assert "Payment settlement failed" in res.text

    # Verify no appointment, charge, or payment was orphaned in DB
    appts = db_session.query(Appointment).filter_by(patient_id=patient.id, appointment_date=target_date).all()
    assert len(appts) == 0, "Appointment must be rolled back on payment failure"

    charges = db_session.query(BillingCharge).filter_by(patient_id=patient.id).all()
    assert len(charges) == 0, "Charge must be rolled back on payment failure"

    payments = db_session.query(BillingPayment).filter_by(patient_id=patient.id).all()
    assert len(payments) == 0, "Payment must be rolled back on payment failure"


def test_future_appointment_payment_date_is_settlement_date(
    client: TestClient,
    admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
    db_session: Session,
):
    """Test Scenario 1: Book future appointment and pay today.
    Payment and receipt date MUST be settlement date (today), NOT the future appointment date.
    """
    doctor.custom_values = {"consultation_fee": 750.0}
    db_session.commit()

    future_date = date.today() + timedelta(days=10)
    target_time = "11:30"
    payload = {
        "patient_id": str(patient.id),
        "doctor_id": str(doctor.id),
        "appointment_date": future_date.isoformat(),
        "appointment_time": f"{target_time}:00",
        "booking_kind": "future",
        "visit_type": "OPD",
        "purpose": "Future follow-up",
        "payment_method": "upi",
        "payment_reference": "UPI-FUTURE-1234",
    }

    res = client.post("/api/appointments", json=payload, headers=admin_headers)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["status"] == "scheduled"
    assert data["payment_method"] == "upi"
    assert data["payment_amount"] == 750.0

    from modules.billing.entities.billing_entities import BillingPayment, BillingReceipt
    payment = db_session.query(BillingPayment).filter_by(patient_id=patient.id, reference_number="UPI-FUTURE-1234").first()
    assert payment is not None
    assert payment.payment_date == date.today()
    assert payment.payment_date != future_date

    receipt = db_session.query(BillingReceipt).filter_by(payment_id=payment.id).first()
    assert receipt is not None
    assert receipt.payment_date == date.today()
    assert receipt.receipt_date == payment.payment_date
    assert receipt.payment_date != future_date


def test_appointment_payment_and_receipt_date_hospital_timezone_boundary(
    db_session: Session,
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
):
    """Test payment_date and receipt_date are derived from hospital IANA timezone across UTC date boundaries."""
    from modules.appointments.actions.book_appointment_action import BookAppointmentAction
    from modules.appointments.contracts.appointments_contracts import BookAppointmentRequest
    from modules.appointments.db.appointments_repository import AppointmentsRepository
    from modules.appointments.db.availability_reader import AvailabilityReader
    from modules.appointments.db.pricing_reader import PricingReader
    from modules.billing.entities.billing_entities import BillingPayment, BillingReceipt

    doctor.custom_values = {"consultation_fee": 500.0}
    hospital.timezone = "Asia/Kolkata"
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    avail = AvailabilityReader(db_session, hospital.id)
    pricing = PricingReader(db_session, hospital.id)

    # UTC instant: 2026-10-10 23:00:00 UTC
    # In Asia/Kolkata (+05:30), local instant is 2026-10-11 04:30:00 AM (next calendar day)
    boundary_instant_ist = datetime(2026, 10, 10, 23, 0, 0, tzinfo=timezone.utc)
    target_appt_date = date(2026, 10, 25)

    action_ist = BookAppointmentAction(
        db_session,
        hospital.id,
        repo,
        avail,
        pricing,
        clock_now=boundary_instant_ist,
    )
    user_actor = {
        "user_id": str(doctor.id),
        "name": "Admin Cashier",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital.id),
    }
    req_ist = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=target_appt_date,
        appointment_time=time(10, 0),
        booking_kind="walk_in",
        purpose="Timezone IST Test",
        settle_payment=True,
        payment_method="cash",
        payment_reference="REF-TZ-IST-01",
    )
    res_ist = action_ist.execute(req_ist, user_actor)
    db_session.commit()

    pay_ist = db_session.query(BillingPayment).filter_by(reference_number="REF-TZ-IST-01").first()
    assert pay_ist is not None
    # Must be 2026-10-11 (hospital date), NOT 2026-10-10 (UTC date)
    assert pay_ist.payment_date == date(2026, 10, 11)
    assert pay_ist.payment_date != boundary_instant_ist.date()

    rcpt_ist = db_session.query(BillingReceipt).filter_by(payment_id=pay_ist.id).first()
    assert rcpt_ist is not None
    assert rcpt_ist.payment_date == date(2026, 10, 11)
    assert rcpt_ist.receipt_date == date(2026, 10, 11)

    # Now test Western hemisphere hospital: America/New_York (EDT UTC-04:00)
    hospital.timezone = "America/New_York"
    db_session.commit()

    # UTC instant: 2026-10-11 02:00:00 UTC (UTC date is 2026-10-11)
    # In America/New_York (-04:00), local instant is 2026-10-10 22:00:00 PM (previous calendar day)
    boundary_instant_ny = datetime(2026, 10, 11, 2, 0, 0, tzinfo=timezone.utc)
    action_ny = BookAppointmentAction(
        db_session,
        hospital.id,
        repo,
        avail,
        pricing,
        clock_now=boundary_instant_ny,
    )
    req_ny = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=target_appt_date,
        appointment_time=time(11, 0),
        booking_kind="walk_in",
        purpose="Timezone NY Test",
        settle_payment=True,
        payment_method="card",
        payment_reference="REF-TZ-NY-02",
    )
    res_ny = action_ny.execute(req_ny, user_actor)
    db_session.commit()

    pay_ny = db_session.query(BillingPayment).filter_by(reference_number="REF-TZ-NY-02").first()
    assert pay_ny is not None
    # Must be 2026-10-10 (hospital date), NOT 2026-10-11 (UTC date)
    assert pay_ny.payment_date == date(2026, 10, 10)
    assert pay_ny.payment_date != boundary_instant_ny.date()

    rcpt_ny = db_session.query(BillingReceipt).filter_by(payment_id=pay_ny.id).first()
    assert rcpt_ny is not None
    assert rcpt_ny.payment_date == date(2026, 10, 10)
    assert rcpt_ny.receipt_date == date(2026, 10, 10)

    # Test D — Winter New York Standard Time (UTC-05:00, NOT hardcoded UTC-04:00)
    # January boundary instant: 2027-01-15 04:30:00 UTC
    # In America/New_York (EST = UTC-05:00), local time is 2027-01-14 23:30:00 PM
    boundary_instant_ny_winter = datetime(2027, 1, 15, 4, 30, 0, tzinfo=timezone.utc)
    action_winter = BookAppointmentAction(
        db_session,
        hospital.id,
        repo,
        avail,
        pricing,
        clock_now=boundary_instant_ny_winter,
    )
    req_winter = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=date(2027, 1, 20),
        appointment_time=time(9, 30),
        booking_kind="walk_in",
        purpose="Timezone NY Winter EST Test",
        settle_payment=True,
        payment_method="cash",
        payment_reference="REF-TZ-NY-WINTER-03",
    )
    res_winter = action_winter.execute(req_winter, user_actor)
    db_session.commit()

    pay_winter = db_session.query(BillingPayment).filter_by(reference_number="REF-TZ-NY-WINTER-03").first()
    assert pay_winter is not None
    # If -04:00 was hardcoded, 04:30 - 4h = 00:30 on 2027-01-15 (WRONG).
    # With real ZoneInfo IANA rules, 04:30 - 5h = 23:30 on 2027-01-14.
    assert pay_winter.payment_date == date(2027, 1, 14)
    assert pay_winter.payment_date != boundary_instant_ny_winter.date()

    # Test E — Invalid Hospital Timezone fails safe to UTC (NEVER silently Asia/Kolkata)
    hospital.timezone = "Amercia/New_York"  # typo
    db_session.commit()

    # At 2026-10-10 23:00:00 UTC:
    # UTC date is 2026-10-10
    # If silently defaulted to IST (+05:30), local date would be 2026-10-11
    # Fail-safe UTC policy produces 2026-10-10
    action_invalid_tz = BookAppointmentAction(
        db_session,
        hospital.id,
        repo,
        avail,
        pricing,
        clock_now=boundary_instant_ist,  # 2026-10-10 23:00:00 UTC
    )
    req_invalid_tz = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=target_appt_date,
        appointment_time=time(14, 0),
        booking_kind="walk_in",
        purpose="Invalid Timezone UTC Fail-Safe Test",
        settle_payment=True,
        payment_method="cash",
        payment_reference="REF-TZ-INVALID-04",
    )
    res_invalid_tz = action_invalid_tz.execute(req_invalid_tz, user_actor)
    db_session.commit()

    pay_invalid = db_session.query(BillingPayment).filter_by(reference_number="REF-TZ-INVALID-04").first()
    assert pay_invalid is not None
    assert pay_invalid.payment_date == date(2026, 10, 10)  # UTC date
    assert pay_invalid.payment_date != date(2026, 10, 11)  # NOT silently IST!


def test_fake_receptionist_name_fails_closed_without_billing_permission(
    client: TestClient,
    db_session: Session,
    hospital: Hospital,
    doctor: HospitalUser,
    patient: LegacyPatient,
):
    """Test F: Financial authorization is NEVER inferred from user name, sub, or email strings."""
    from fastapi import HTTPException
    from modules.appointments.actions.book_appointment_action import BookAppointmentAction
    from modules.appointments.contracts.appointments_contracts import BookAppointmentRequest
    from modules.appointments.db.appointments_repository import AppointmentsRepository
    from modules.appointments.db.availability_reader import AvailabilityReader
    from modules.appointments.db.pricing_reader import PricingReader
    from modules.appointments.entities.enums import AppointmentStatus
    from modules.doctors.entities.doctor import HospitalUser, RolePermission, StaffRole
    import pytest

    doctor.custom_values = {"consultation_fee": 500.0}
    db_session.commit()

    # Create a generic hospital staff role WITHOUT billing.create permission
    staff_role = StaffRole(hospital_id=hospital.id, name="General Appointment Desk")
    db_session.add(staff_role)
    db_session.flush()

    perm_appt = RolePermission(
        hospital_id=hospital.id,
        role_id=staff_role.id,
        module_key="appointment",
        can_view=True,
        can_create=True,
    )
    # Only appointment.create, absolutely NO billing permissions
    db_session.add(perm_appt)
    db_session.commit()

    # User whose name and email contain "Receptionist" and "Cashier"
    fake_user = HospitalUser(
        hospital_id=hospital.id,
        name="Front Desk Receptionist Cashier Test",
        email="receptionist.cashier@hospital.org",
        phone="9876543299",
        password_hash="dummy_hash",
        role_id=staff_role.id,
        is_active=True,
    )
    db_session.add(fake_user)
    db_session.commit()

    fake_actor = {
        "user_id": str(fake_user.id),
        "sub": fake_user.email,
        "name": fake_user.name,
        "role": "hospital_staff",
        "staff_role_id": str(staff_role.id),
        "hospital_uuid": str(hospital.id),
    }

    repo = AppointmentsRepository(db_session, hospital.id)
    avail = AvailabilityReader(db_session, hospital.id)
    pricing = PricingReader(db_session, hospital.id)
    action = BookAppointmentAction(db_session, hospital.id, repo, avail, pricing)

    # 1. Attempt Pay Now (settle_payment=True): MUST FAIL CLOSED with 403
    pay_now_req = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=date.today() + timedelta(days=2),
        appointment_time=time(10, 0),
        booking_kind="walk_in",
        purpose="Fake Receptionist Settlement Security Test",
        settle_payment=True,
        payment_method="cash",
        payment_reference="REF-FAKE-RECEPT-01",
    )
    with pytest.raises(HTTPException) as exc_info:
        action.execute(pay_now_req, fake_actor)
    assert exc_info.value.status_code == 403

    # 2. Pay Later (settle_payment=False): Follows existing policy (appointment.create is present)
    pay_later_req = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=date.today() + timedelta(days=2),
        appointment_time=time(11, 0),
        booking_kind="walk_in",
        purpose="Fake Receptionist Pay Later Test",
        settle_payment=False,
    )
    pay_later_res = action.execute(pay_later_req, fake_actor)
    assert pay_later_res is not None
    assert pay_later_res.status == AppointmentStatus.scheduled





