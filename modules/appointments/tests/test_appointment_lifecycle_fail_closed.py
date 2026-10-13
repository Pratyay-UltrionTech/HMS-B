"""
Tests proving that diagnostic blocker checks FAIL CLOSED on error,
and that radiology prescription requests are authoritatively included in blockers.
"""

from unittest.mock import MagicMock
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session

from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.appointments.exceptions.appointments_exceptions import (
    ClinicalBlockerResolutionError,
)
from modules.appointments.services.appointment_lifecycle import (
    can_complete_appointment,
    complete_appointment_record,
    get_open_clinical_blockers,
)


def test_blocker_query_failure_raises_and_fails_closed():
    """An injected database or service failure during blocker inspection must FAIL CLOSED."""
    mock_db = MagicMock(spec=Session)
    mock_db.execute.side_effect = RuntimeError("Database timeout during blocker check")
    mock_db.query.side_effect = RuntimeError("Database timeout during blocker check")
    hosp_id = uuid4()
    appt_id = uuid4()
    appt = Appointment(id=appt_id, hospital_id=hosp_id, status=AppointmentStatus.scheduled)

    # 1. Direct call to get_open_clinical_blockers raises domain exception
    with pytest.raises(ClinicalBlockerResolutionError) as exc_info:
        get_open_clinical_blockers(mock_db, hosp_id, appt_id)
    assert "Diagnostic safety verification failed" in str(exc_info.value)

    # 2. can_complete_appointment evaluates to False with fail-closed blocker
    ok, blockers = can_complete_appointment(mock_db, hosp_id, appt, force=False)
    assert ok is False
    assert len(blockers) == 1
    assert "Diagnostic safety verification failed" in blockers[0]

    # 3. complete_appointment_record refuses to mark appointment completed
    ok2, blockers2 = complete_appointment_record(mock_db, hosp_id, appt, force=False)
    assert ok2 is False
    assert appt.status != AppointmentStatus.completed


def test_radiology_prescription_request_blocks_completion(db_session, hospital, doctor, patient):
    """A pending radiology prescription request must block appointment completion."""
    from modules.appointments.entities.appointment import Appointment as ApptEntity
    from modules.clinical_records.entities.clinical_record import Prescription
    from modules.radiology.entities.radiology_entities import (
        RadPrescriptionRequest,
        RadPrescriptionRequestStatus,
    )
    from datetime import date, time

    appt = ApptEntity(
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        purpose="Follow up",
        status=AppointmentStatus.scheduled,
    )
    db_session.add(appt)
    db_session.flush()

    rx = Prescription(
        hospital_id=hospital.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_id=appt.id,
        symptoms="Pain",
        diagnosis="Suspected fracture",
        medicines="Paracetamol 500mg",
        dosage="TDS x 3 days",
    )
    db_session.add(rx)
    db_session.flush()

    rad_req = RadPrescriptionRequest(
        hospital_id=hospital.id,
        prescription_id=rx.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_id=appt.id,
        status=RadPrescriptionRequestStatus.pending,
        prescribed_scan_ids=[],
    )
    db_session.add(rad_req)
    db_session.commit()

    blockers = get_open_clinical_blockers(db_session, hospital.id, appt.id)
    assert any("Doctor radiology request pending" in b for b in blockers)

    ok, blockers = can_complete_appointment(db_session, hospital.id, appt, force=False)
    assert ok is False
    assert any("radiology" in b.lower() for b in blockers)
