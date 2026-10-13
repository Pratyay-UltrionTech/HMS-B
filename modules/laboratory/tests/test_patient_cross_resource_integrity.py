"""
Integration tests for cross-module patient identity integrity (XMOD-009).

Verifies that creating Lab orders, Radiology orders, or Prescriptions
rejects mismatched patient-appointment, patient-admission, and
patient-prescription relationships with HTTP 400.
"""

from datetime import date, time
from uuid import uuid4
import pytest
from fastapi import HTTPException
from tests.conftest import db_session, doctor, hospital, patient

from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.clinical_records.actions.clinical_actions import CreatePrescriptionAction
from modules.clinical_records.contracts.clinical_contracts import PrescriptionCreate
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.laboratory.actions.laboratory_actions import CreateLabOrderAction
from modules.laboratory.contracts.lab_contracts import LabOrderCreate
from modules.laboratory.entities.lab_entities import LabTestCatalog
from modules.patients.entities.patient import Patient
from modules.radiology.actions.radiology_actions import CreateOrdersAction
from modules.radiology.contracts.radiology_contracts import RadOrderCreate
from modules.radiology.entities.radiology_entities import RadiologyScanCatalog


def _create_second_patient(db_session, hospital) -> Patient:
    p2 = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid=f"UHID-TEST-{uuid4().hex[:6]}",
        first_name="Jane",
        last_name="Smith",
        name="Jane Smith",
        mobile=f"98{uuid4().int % 100000000:08d}",
        gender="female",
        age=30,
    )
    db_session.add(p2)
    db_session.flush()
    return p2


def test_lab_order_rejects_mismatched_appointment(db_session, hospital, doctor, patient):
    """Creating a Lab order for Patient A with Appointment B (Patient B) must fail with 400."""
    p2 = _create_second_patient(db_session, hospital)

    # Appointment belongs to Patient 2
    appt = Appointment(
        hospital_id=hospital.id,
        patient_id=p2.id,
        doctor_id=doctor.id,
        appointment_date=date.today(),
        appointment_time=time(11, 0),
        purpose="General checkup",
        status=AppointmentStatus.scheduled,
    )
    db_session.add(appt)

    # Test catalog item
    test_item = LabTestCatalog(
        hospital_id=hospital.id,
        test_code=f"CBC-{uuid4().hex[:4]}",
        test_name="Complete Blood Count",
        department="Hematology",
        price=350.0,
        is_active=True,
    )
    db_session.add(test_item)
    db_session.flush()

    action = CreateLabOrderAction(db_session, hospital.id)
    user_context = {"id": str(doctor.id), "name": "Dr. Test", "role": "doctor"}

    # Attempt to link Patient 1 with Patient 2's appointment
    payload = LabOrderCreate(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_id=appt.id,
        test_ids=[test_item.id],
    )

    with pytest.raises(HTTPException) as exc_info:
        action.execute(payload, user_context)

    assert exc_info.value.status_code == 400
    assert "Appointment does not belong to the selected patient" in str(exc_info.value.detail)


def test_lab_order_rejects_mismatched_admission(db_session, hospital, doctor, patient):
    """Creating a Lab order for Patient A with Admission B (Patient B) must fail with 400."""
    p2 = _create_second_patient(db_session, hospital)

    adm = Admission(
        hospital_id=hospital.id,
        patient_id=p2.id,
        doctor_id=doctor.id,
        status=AdmissionStatus.admitted,
    )
    db_session.add(adm)
    db_session.flush()

    test_item = LabTestCatalog(
        hospital_id=hospital.id,
        test_code=f"BMP-{uuid4().hex[:4]}",
        test_name="Basic Metabolic Panel",
        department="Biochemistry",
        price=500.0,
        is_active=True,
    )
    db_session.add(test_item)
    db_session.flush()

    action = CreateLabOrderAction(db_session, hospital.id)
    user_context = {"id": str(doctor.id), "name": "Dr. Test", "role": "doctor"}

    payload = LabOrderCreate(
        patient_id=patient.id,
        doctor_id=doctor.id,
        admission_id=adm.id,
        test_ids=[test_item.id],
    )

    with pytest.raises(HTTPException) as exc_info:
        action.execute(payload, user_context)

    assert exc_info.value.status_code == 400
    assert "Admission does not belong to the selected patient" in str(exc_info.value.detail)


def test_radiology_order_rejects_mismatched_appointment(db_session, hospital, doctor, patient):
    """Creating a Radiology order for Patient A with Appointment B (Patient B) must fail with 400."""
    p2 = _create_second_patient(db_session, hospital)

    appt = Appointment(
        hospital_id=hospital.id,
        patient_id=p2.id,
        doctor_id=doctor.id,
        appointment_date=date.today(),
        appointment_time=time(14, 0),
        purpose="X-Ray check",
        status=AppointmentStatus.scheduled,
    )
    db_session.add(appt)

    scan_item = RadiologyScanCatalog(
        hospital_id=hospital.id,
        scan_code=f"XRAY-{uuid4().hex[:4]}",
        scan_name="Chest X-Ray PA",
        department="Radiology",
        price=600.0,
        is_active=True,
    )
    db_session.add(scan_item)
    db_session.flush()

    action = CreateOrdersAction(
        db_session,
        hospital.id,
        user={"id": str(doctor.id), "name": "Dr. Test", "role": "doctor"},
    )

    payload = RadOrderCreate(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_id=appt.id,
        scan_ids=[scan_item.id],
    )

    with pytest.raises(HTTPException) as exc_info:
        action.execute(payload)

    assert exc_info.value.status_code == 400
    assert "Appointment does not belong to the selected patient" in str(exc_info.value.detail)


def test_prescription_rejects_mismatched_appointment(db_session, hospital, doctor, patient):
    """Issuing a prescription for Patient A with Appointment B (Patient B) must fail with 400."""
    p2 = _create_second_patient(db_session, hospital)

    appt = Appointment(
        hospital_id=hospital.id,
        patient_id=p2.id,
        doctor_id=doctor.id,
        appointment_date=date.today(),
        appointment_time=time(15, 0),
        purpose="Consultation",
        status=AppointmentStatus.scheduled,
    )
    db_session.add(appt)
    db_session.flush()

    action = CreatePrescriptionAction(db_session)
    actor = {"user_id": str(doctor.id), "role": "doctor"}

    payload = PrescriptionCreate(
        patient_id=patient.id,
        appointment_id=appt.id,
        symptoms="Fever and chills",
        diagnosis="Viral infection",
        medicines="Paracetamol 500mg",
        dosage="TDS x 3 days",
        status="issued",
    )

    with pytest.raises(HTTPException) as exc_info:
        action.execute(
            hospital_id=hospital.id,
            doctor_id=doctor.id,
            payload=payload,
            actor=actor,
        )

    assert exc_info.value.status_code == 400
    assert "Appointment does not belong to the selected patient" in str(exc_info.value.detail)
