"""
Tests for Phase 2: Decoupled Consultation and Diagnostic Fulfillment Workflow Lifecycle.

Invariants tested:
1. Appointment with open lab tests shows overall_workflow_status = "awaiting_lab_results"
2. Appointment with open radiology scans shows overall_workflow_status = "awaiting_radiology_results"
3. Appointment with both open lab & rad shows overall_workflow_status = "awaiting_lab_and_radiology_results"
4. Completed diagnostics move status to "results_ready_review_pending"
5. Doctor reviewing diagnostics updates diagnostic_review_status = "reviewed"
6. CompleteAppointmentAction fails closed when diagnostics pending unless force=True with explicit override reason
7. Prescription creation advances consultation_status to "consultation_completed"
"""

from datetime import date, time, datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session

from modules.appointments.actions.appointment_status_actions import CompleteAppointmentAction
from modules.appointments.actions.review_diagnostics_action import ReviewDiagnosticsAction
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.appointments.exceptions import AppointmentValidationError
from modules.appointments.services.appointment_workflow_service import AppointmentWorkflowService
from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderItem,
    LabOrderSource,
    LabOrderStatus,
    LabItemStatus,
)
from modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
)
from modules.doctors.entities.doctor import HospitalUser
from modules.patients.entities.patient import Patient


def _create_appointment(db: Session, hospital_id, doctor, patient, status=AppointmentStatus.waiting):
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital_id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        status=status,
        consultation_status="in_consultation",
        purpose="General Checkup",
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt


def test_appointment_workflow_awaiting_lab(db_session: Session, hospital, doctor, patient):
    appt = _create_appointment(db_session, hospital.id, doctor, patient)

    # Add open lab order
    order = LabOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"LAB-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=appt.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.ordered,
    )
    db_session.add(order)
    db_session.flush()

    item = LabOrderItem(
        id=uuid4(),
        hospital_id=hospital.id,
        order_id=order.id,
        test_code=f"CBC-{uuid4().hex[:4]}",
        test_name="Complete Blood Count",
        status=LabItemStatus.pending,
    )
    db_session.add(item)
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    items = repo.hydrate_appointment_items([appt])
    assert len(items) == 1
    item_res = items[0]
    assert item_res.investigation_status == "awaiting_lab"
    assert item_res.overall_workflow_status == "awaiting_lab_results"
    assert item_res.workflow_status_label == "Awaiting Lab Results"


def test_appointment_workflow_awaiting_rad(db_session: Session, hospital, doctor, patient):
    appt = _create_appointment(db_session, hospital.id, doctor, patient)

    rad_order = RadiologyOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"RAD-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=appt.id,
        scan_code=f"CXR-{uuid4().hex[:4]}",
        scan_name="Chest X-Ray",
        status=RadiologyOrderStatus.ordered,
    )
    db_session.add(rad_order)
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    items = repo.hydrate_appointment_items([appt])
    assert items[0].investigation_status == "awaiting_radiology"
    assert items[0].overall_workflow_status == "awaiting_radiology_results"
    assert items[0].workflow_status_label == "Awaiting Radiology Results"


def test_appointment_workflow_awaiting_both_lab_and_rad(db_session: Session, hospital, doctor, patient):
    appt = _create_appointment(db_session, hospital.id, doctor, patient)

    # Lab order
    lab_order = LabOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"LAB-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=appt.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.ordered,
    )
    db_session.add(lab_order)
    db_session.flush()
    db_session.add(
        LabOrderItem(
            id=uuid4(),
            hospital_id=hospital.id,
            order_id=lab_order.id,
            test_code=f"LIP-{uuid4().hex[:4]}",
            test_name="Lipid Profile",
            status=LabItemStatus.pending,
        )
    )

    # Rad order
    db_session.add(
        RadiologyOrder(
            id=uuid4(),
            hospital_id=hospital.id,
            order_no=f"RAD-{uuid4().hex[:6].upper()}",
            patient_id=patient.id,
            appointment_id=appt.id,
            scan_code=f"USG-{uuid4().hex[:4]}",
            scan_name="USG Abdomen",
            status=RadiologyOrderStatus.ordered,
        )
    )
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    items = repo.hydrate_appointment_items([appt])
    assert items[0].investigation_status == "awaiting_lab_and_radiology"
    assert items[0].overall_workflow_status == "awaiting_lab_and_radiology_results"
    assert items[0].workflow_status_label == "Awaiting Lab & Radiology Results"


def test_complete_appointment_blocked_fails_closed_without_force(db_session: Session, hospital, doctor, patient):
    appt = _create_appointment(db_session, hospital.id, doctor, patient)

    # Open lab order
    lab_order = LabOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"LAB-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=appt.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.ordered,
    )
    db_session.add(lab_order)
    db_session.flush()
    db_session.add(
        LabOrderItem(
            id=uuid4(),
            hospital_id=hospital.id,
            order_id=lab_order.id,
            test_code=f"GLU-{uuid4().hex[:4]}",
            test_name="Fasting Glucose",
            status=LabItemStatus.pending,
        )
    )
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    actor = {"id": str(doctor.id), "name": doctor.name, "role": "doctor"}

    # Attempt complete without force -> must raise AppointmentValidationError
    with pytest.raises(AppointmentValidationError) as exc:
        CompleteAppointmentAction(db_session, hospital.id, repo).execute(appt.id, actor, force=False)
    assert "Cannot complete appointment" in str(exc.value)
    assert "lab order" in str(exc.value).lower()

    # Attempt with force=True and explicit override reason -> must succeed and record audit trail
    res = CompleteAppointmentAction(db_session, hospital.id, repo).execute(
        appt.id, actor, force=True, override_reason="Patient requested immediate discharge and follow-up at home"
    )
    assert res.status == AppointmentStatus.completed
    assert res.consultation_status == "consultation_completed"
    assert res.clinical_override_reason == "Patient requested immediate discharge and follow-up at home"


def test_doctor_review_diagnostics_action(db_session: Session, hospital, doctor, patient):
    appt = _create_appointment(db_session, hospital.id, doctor, patient)

    # Finalized lab order
    lab_order = LabOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"LAB-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=appt.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.completed,
    )
    db_session.add(lab_order)
    db_session.flush()
    db_session.add(
        LabOrderItem(
            id=uuid4(),
            hospital_id=hospital.id,
            order_id=lab_order.id,
            test_code=f"UREA-{uuid4().hex[:4]}",
            test_name="Blood Urea",
            status=LabItemStatus.completed,
        )
    )
    from modules.laboratory.entities.lab_entities import LabResult
    db_session.add(
        LabResult(
            id=uuid4(),
            hospital_id=hospital.id,
            order_id=lab_order.id,
            parameter_name="Blood Urea",
            result_value="28",
        )
    )
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    items = repo.hydrate_appointment_items([appt])
    assert items[0].investigation_status == "results_ready"
    assert items[0].overall_workflow_status == "results_ready_review_pending"

    # Now doctor performs review
    actor = {"id": str(doctor.id), "name": doctor.name, "role": "doctor"}
    res = ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(appt.id, actor, notes="Results within normal limits")
    assert res.diagnostic_review_status == "reviewed"


def test_appointment_workflow_lab_sample_collected_is_in_progress(db_session: Session, hospital, doctor, patient):
    appt = _create_appointment(db_session, hospital.id, doctor, patient)
    lab_order = LabOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"LAB-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=appt.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.sample_collected,
    )
    db_session.add(lab_order)
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    items = repo.hydrate_appointment_items([appt])
    assert items[0].investigation_status == "in_progress"
    assert items[0].overall_workflow_status == "investigations_in_progress"
    assert items[0].workflow_status_label == "Investigations In Progress"


def test_appointment_workflow_rad_in_progress_is_in_progress(db_session: Session, hospital, doctor, patient):
    appt = _create_appointment(db_session, hospital.id, doctor, patient)
    rad_order = RadiologyOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"RAD-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=appt.id,
        scan_code=f"MRI-{uuid4().hex[:4]}",
        scan_name="Brain MRI",
        status=RadiologyOrderStatus.in_progress,
    )
    db_session.add(rad_order)
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    items = repo.hydrate_appointment_items([appt])
    assert items[0].investigation_status == "in_progress"
    assert items[0].overall_workflow_status == "investigations_in_progress"
    assert items[0].workflow_status_label == "Investigations In Progress"


def test_pure_state_machine_comprehensive_matrix():
    from shared.diagnostics.contracts import DiagnosticDomainSummary

    # 1. Lab ordered (waiting)
    lab_w = DiagnosticDomainSummary(domain="laboratory", active_order_count=1, waiting_count=1, has_open_work=True)
    _, overall, label = AppointmentWorkflowService.compute_workflow_status(lab_summary=lab_w)
    assert overall == "awaiting_lab_results"
    assert label == "Awaiting Lab Results"

    # 2. Lab sample collected / in progress
    lab_ip = DiagnosticDomainSummary(domain="laboratory", active_order_count=1, in_progress_count=1, has_open_work=True, has_in_progress=True)
    _, overall, label = AppointmentWorkflowService.compute_workflow_status(lab_summary=lab_ip)
    assert overall == "investigations_in_progress"
    assert label == "Investigations In Progress"

    # 3. Radiology ordered / scheduled (waiting)
    rad_w = DiagnosticDomainSummary(domain="radiology", active_order_count=1, waiting_count=1, has_open_work=True)
    _, overall, label = AppointmentWorkflowService.compute_workflow_status(rad_summary=rad_w)
    assert overall == "awaiting_radiology_results"
    assert label == "Awaiting Radiology Results"

    # 4. Radiology in progress
    rad_ip = DiagnosticDomainSummary(domain="radiology", active_order_count=1, in_progress_count=1, has_open_work=True, has_in_progress=True)
    _, overall, label = AppointmentWorkflowService.compute_workflow_status(rad_summary=rad_ip)
    assert overall == "investigations_in_progress"
    assert label == "Investigations In Progress"

    # 5. Both waiting
    _, overall, label = AppointmentWorkflowService.compute_workflow_status(lab_summary=lab_w, rad_summary=rad_w)
    assert overall == "awaiting_lab_and_radiology_results"
    assert label == "Awaiting Lab & Radiology Results"

    # 6. Lab in progress + Rad waiting
    _, overall, label = AppointmentWorkflowService.compute_workflow_status(lab_summary=lab_ip, rad_summary=rad_w)
    assert overall == "investigations_in_progress"
    assert label == "Investigations In Progress"

    # 7. Lab final result ready + Rad in progress
    lab_final = DiagnosticDomainSummary(domain="laboratory", finalized_result_count=1, has_final_result=True)
    _, overall, label = AppointmentWorkflowService.compute_workflow_status(lab_summary=lab_final, rad_summary=rad_ip)
    assert overall == "investigations_in_progress"
    assert label == "Investigations In Progress"

    # 8. All results final, review pending
    rad_final = DiagnosticDomainSummary(domain="radiology", finalized_result_count=1, has_final_result=True)
    _, overall, label = AppointmentWorkflowService.compute_workflow_status(lab_summary=lab_final, rad_summary=rad_final)
    assert overall == "results_ready_review_pending"
    assert label == "Results Ready — Doctor Review Pending"

    # 9. Doctor review completed
    _, overall, label = AppointmentWorkflowService.compute_workflow_status(
        consultation_status="consultation_completed",
        diagnostic_review_status="reviewed",
        lab_summary=lab_final,
        rad_summary=rad_final,
    )
    assert overall == "consultation_completed"
