"""
Comprehensive regression tests for Cross-Module Closure Invariants (Fix 1, 3, 4, 5, 10, 12).

Verifies:
1. Architecture invariant: 0 raw Core table declarations in appointments workflow/lifecycle.
2. All 13 diagnostic workflow status combinations.
3. Request-level booking idempotency: sequential replay, payload conflict (409).
4. Review diagnostics requirements: fails without finalized results, captures reviewer & notes.
5. Force completion clinical override: requires explicit non-empty override reason.
"""

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session

from modules.appointments.actions.appointment_status_actions import CompleteAppointmentAction
from modules.appointments.actions.book_appointment_action import BookAppointmentAction
from modules.appointments.actions.review_diagnostics_action import ReviewDiagnosticsAction
from modules.appointments.contracts.appointments_contracts import (
    BookAppointmentRequest,
    RescheduleRequest,
)
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.db.availability_reader import AvailabilityReader
from modules.appointments.db.pricing_reader import PricingReader
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.appointments.exceptions import (
    AppointmentConflictError,
    AppointmentValidationError,
)
from modules.appointments.services.appointment_workflow_service import AppointmentWorkflowService
from modules.laboratory.entities.lab_entities import (
    LabItemStatus,
    LabOrder,
    LabOrderItem,
    LabOrderSource,
    LabOrderStatus,
)
from modules.patients.entities.patient import Patient
from modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
)
from decimal import Decimal
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingPayment,
    BillingPaymentAllocation,
    BillingPaymentMethod,
    BillingReceipt,
    BillingReceiptStatus,
    BillingSourceType,
    ConsultationPricing,
)
from modules.billing.entities.idempotency import IdempotencyRecord
from modules.billing.services.idempotency_service import IdempotencyService
from modules.billing.services.invoice_service import issue_receipt, receipt_html
from modules.billing.services.receipt_financial_context import build_receipt_financial_context
from shared.diagnostics.contracts import DiagnosticDomainSummary
from shared.exceptions.base import ConflictError
from tests.conftest import db_session, doctor, hospital, patient



# ===========================================================================
# 1. ARCHITECTURE INVARIANT TEST
# ===========================================================================

def test_architecture_no_raw_core_table_declarations_in_appointments():
    """Assert zero raw Core table declarations (_lab_orders, etc.) in appointments workflow/lifecycle."""
    repo_root = Path(__file__).resolve().parents[3]
    workflow_svc_path = repo_root / "modules" / "appointments" / "services" / "appointment_workflow_service.py"
    lifecycle_path = repo_root / "modules" / "appointments" / "services" / "appointment_lifecycle.py"

    for file_path in [workflow_svc_path, lifecycle_path]:
        assert file_path.exists(), f"File {file_path} not found"
        content = file_path.read_text(encoding="utf-8")

        # Must not declare raw SQLAlchemy table instances for other modules
        assert "_lab_orders = table(" not in content, f"Raw _lab_orders found in {file_path.name}"
        assert "_rad_orders = table(" not in content, f"Raw _rad_orders found in {file_path.name}"
        assert "_rad_prescription_requests = table(" not in content, f"Raw _rad_prescription_requests in {file_path.name}"

        # Must consume domain workflow readers
        assert "LabEncounterWorkflowReader" in content, f"LabEncounterWorkflowReader missing from {file_path.name}"
        assert "RadiologyEncounterWorkflowReader" in content, f"RadiologyEncounterWorkflowReader missing from {file_path.name}"


# ===========================================================================
# 2. 13 DIAGNOSTIC WORKFLOW COMBINATIONS TEST
# ===========================================================================

def test_diagnostic_workflow_all_13_deterministic_combinations():
    """Verify all 13 combinations of Lab & Radiology summaries produce deterministic statuses."""
    # (lab_summary, rad_summary, expected_inv_status, expected_wf_status, expected_label)
    cases = [
        # 1. Both None -> No investigations
        (
            DiagnosticDomainSummary(),
            DiagnosticDomainSummary(),
            None,
            "in_consultation",
            "In Consultation",
        ),
        # 2. Lab open, Rad None -> Awaiting lab
        (
            DiagnosticDomainSummary(total_orders=1, open_orders=1, has_open_investigations=True),
            DiagnosticDomainSummary(),
            "awaiting_lab",
            "awaiting_lab_results",
            "Awaiting Lab Results",
        ),
        # 3. Lab None, Rad open -> Awaiting radiology
        (
            DiagnosticDomainSummary(),
            DiagnosticDomainSummary(total_orders=1, open_orders=1, has_open_investigations=True),
            "awaiting_radiology",
            "awaiting_radiology_results",
            "Awaiting Radiology Results",
        ),
        # 4. Both open -> Awaiting lab & radiology
        (
            DiagnosticDomainSummary(total_orders=1, open_orders=1, has_open_investigations=True),
            DiagnosticDomainSummary(total_orders=1, open_orders=1, has_open_investigations=True),
            "awaiting_lab_and_radiology",
            "awaiting_lab_and_radiology_results",
            "Awaiting Lab & Radiology Results",
        ),
        # 5. Lab ready (has final), Rad None -> Results ready
        (
            DiagnosticDomainSummary(total_orders=1, completed_orders=1, has_final_result=True),
            DiagnosticDomainSummary(),
            "results_ready",
            "results_ready_review_pending",
            "Results Ready (Review Pending)",
        ),
        # 6. Lab None, Rad ready (has final) -> Results ready
        (
            DiagnosticDomainSummary(),
            DiagnosticDomainSummary(total_orders=1, completed_orders=1, has_final_result=True),
            "results_ready",
            "results_ready_review_pending",
            "Results Ready (Review Pending)",
        ),
        # 7. Both ready (has final) -> Results ready
        (
            DiagnosticDomainSummary(total_orders=1, completed_orders=1, has_final_result=True),
            DiagnosticDomainSummary(total_orders=1, completed_orders=1, has_final_result=True),
            "results_ready",
            "results_ready_review_pending",
            "Results Ready (Review Pending)",
        ),
        # 8. Lab ready, Rad open -> Awaiting radiology (open blocker takes precedence)
        (
            DiagnosticDomainSummary(total_orders=1, completed_orders=1, has_final_result=True),
            DiagnosticDomainSummary(total_orders=1, open_orders=1, has_open_investigations=True),
            "awaiting_radiology",
            "awaiting_radiology_results",
            "Awaiting Radiology Results",
        ),
        # 9. Lab open, Rad ready -> Awaiting lab (open blocker takes precedence)
        (
            DiagnosticDomainSummary(total_orders=1, open_orders=1, has_open_investigations=True),
            DiagnosticDomainSummary(total_orders=1, completed_orders=1, has_final_result=True),
            "awaiting_lab",
            "awaiting_lab_results",
            "Awaiting Lab Results",
        ),
        # 10. Lab cancelled, Rad None -> Cancelled (no final, no open)
        (
            DiagnosticDomainSummary(total_orders=1, cancelled_orders=1),
            DiagnosticDomainSummary(),
            "cancelled",
            "in_consultation",
            "In Consultation",
        ),
        # 11. Lab None, Rad cancelled -> Cancelled
        (
            DiagnosticDomainSummary(),
            DiagnosticDomainSummary(total_orders=1, cancelled_orders=1),
            "cancelled",
            "in_consultation",
            "In Consultation",
        ),
        # 12. Both cancelled -> Cancelled
        (
            DiagnosticDomainSummary(total_orders=1, cancelled_orders=1),
            DiagnosticDomainSummary(total_orders=1, cancelled_orders=1),
            "cancelled",
            "in_consultation",
            "In Consultation",
        ),
        # 13. Lab ready & already reviewed in appointment -> Reviewed
        (
            DiagnosticDomainSummary(total_orders=1, completed_orders=1, has_final_result=True),
            DiagnosticDomainSummary(total_orders=1, completed_orders=1, has_final_result=True),
            "reviewed",
            "consultation_completed",
            "Consultation Completed",
        ),
    ]

    for idx, (lab, rad, exp_inv, exp_wf, exp_lbl) in enumerate(cases):
        # Case 13 tests diagnostic_review_status = "reviewed"
        rev_status = "reviewed" if idx == 12 else None
        consult_status = "consultation_completed" if idx == 12 else "in_consultation"

        inv_st, wf_st, wf_lbl = AppointmentWorkflowService.compute_workflow_status(
            consultation_status=consult_status,
            diagnostic_review_status=rev_status,
            lab_summary=lab,
            rad_summary=rad,
        )
        assert inv_st == exp_inv, f"Case {idx + 1} failed inv_status: got {inv_st}, expected {exp_inv}"
        assert wf_st == exp_wf, f"Case {idx + 1} failed wf_status: got {wf_st}, expected {exp_wf}"
        assert wf_lbl == exp_lbl, f"Case {idx + 1} failed label: got {wf_lbl}, expected {exp_lbl}"


# ===========================================================================
# 3. REQUEST-LEVEL BOOKING IDEMPOTENCY TEST
# ===========================================================================

def test_booking_idempotency_sequential_replay_and_conflict(db_session: Session, hospital, doctor, patient):
    """Sequential duplicate returns original cached response; payload mismatch returns 409."""
    repo = AppointmentsRepository(db_session, hospital.id)
    avail = AvailabilityReader(db_session, hospital.id)
    pricing = PricingReader(db_session, hospital.id)
    action = BookAppointmentAction(db_session, hospital.id, repo, avail, pricing)

    user = {"sub": "desk@test.com", "name": "Front Desk", "role": "receptionist"}
    idemp_key = f"IDEMP-BOOK-{uuid4().hex[:8]}"

    payload1 = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=date.today() + timedelta(days=1),
        appointment_time=time(14, 0),
        booking_kind="walk_in",
        purpose="Cardiology Review",
        payment_method="cash",
    )

    # 1. First execution creates appointment
    res1 = action.execute(payload1, user, idempotency_key=idemp_key)
    assert res1.id is not None
    assert res1.op_id is not None

    # 2. Sequential duplicate with same key and payload returns same appointment
    res2 = action.execute(payload1, user, idempotency_key=idemp_key)
    assert res2.id == res1.id
    assert res2.op_id == res1.op_id

    # 3. Payload mismatch with same key raises ConflictError (HTTP 409)
    payload_diff = BookAppointmentRequest(
        patient_id=patient.id,
        doctor_id=doctor.id,
        appointment_date=date.today(),
        appointment_time=time(16, 0),  # Different time
        booking_kind="walk_in",
        purpose="Cardiology Review",
    )
    with pytest.raises(ConflictError) as exc:
        action.execute(payload_diff, user, idempotency_key=idemp_key)
    assert "Idempotency conflict" in str(exc.value)




# ===========================================================================
# 4. REVIEW DIAGNOSTICS & CLINICAL OVERRIDE TESTS
# ===========================================================================

def test_review_diagnostics_requires_finalized_results_and_records_audit(db_session: Session, hospital, doctor, patient):
    """Reviewing diagnostics fails without finalized results, and authoritatively records reviewer metadata."""
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        status=AppointmentStatus.waiting,
        consultation_status="in_consultation",
        purpose="Checkup",
    )
    db_session.add(appt)
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    doc_actor = {"id": str(doctor.id), "name": "Dr. Sarah", "role": "doctor"}

    # 1. Review with no diagnostic orders at all must fail
    with pytest.raises(AppointmentValidationError) as exc1:
        ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(appt.id, doc_actor)
    assert "no finalized diagnostic results exist" in str(exc1.value)

    # 2. Add an open (unfinalized) order -> must fail with open blockers
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
    db_session.commit()

    with pytest.raises(AppointmentValidationError) as exc2:
        ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(appt.id, doc_actor)
    assert "investigations are still open" in str(exc2.value)

    # 3. Mark items/order completed but WITHOUT LabResult -> must still fail
    lab_order.status = LabOrderStatus.completed
    lab_order.completed_at = datetime.now(timezone.utc)
    item = LabOrderItem(
        id=uuid4(),
        hospital_id=hospital.id,
        order_id=lab_order.id,
        test_code="CBC-001",
        test_name="CBC",
        status=LabItemStatus.completed,
    )
    db_session.add(item)
    db_session.commit()

    with pytest.raises(AppointmentValidationError) as exc3:
        ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(appt.id, doc_actor)
    assert "investigations are still open" in str(exc3.value) or "no finalized diagnostic results exist" in str(exc3.value)

    # 4. Save authoritative LabResult -> review succeeds and sets authoritative reviewer metadata
    from modules.laboratory.entities.lab_entities import LabResult

    res = LabResult(
        id=uuid4(),
        hospital_id=hospital.id,
        order_id=lab_order.id,
        order_item_id=item.id,
        parameter_name="Hemoglobin",
        result_value="14.2 g/dL",
        reference_range="13.0 - 17.0",
    )
    db_session.add(res)
    db_session.commit()

    reviewed = ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(
        appt.id, doc_actor, notes="Hemoglobin normal, platelet count adequate."
    )
    assert reviewed.diagnostic_review_status == "reviewed"
    assert reviewed.diagnostics_reviewed_by == "Dr. Sarah"
    assert reviewed.diagnostic_review_notes == "Hemoglobin normal, platelet count adequate."
    assert reviewed.diagnostics_reviewed_at is not None


def test_complete_appointment_override_reason_validation(db_session: Session, hospital, doctor, patient):
    """Force completion requires an explicit non-empty override reason."""
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=date.today(),
        appointment_time=time(11, 0),
        status=AppointmentStatus.waiting,
        consultation_status="in_consultation",
        purpose="Checkup",
    )
    db_session.add(appt)

    # Open unfulfilled lab order
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
    db_session.commit()

    repo = AppointmentsRepository(db_session, hospital.id)
    doc_actor = {"id": str(doctor.id), "name": "Dr. Sarah", "role": "doctor"}

    # 1. Force complete with empty reason must fail
    with pytest.raises(AppointmentValidationError) as exc:
        CompleteAppointmentAction(db_session, hospital.id, repo).execute(
            appt.id, doc_actor, force=True, override_reason="   "
        )
    assert "override reason is required" in str(exc.value)

    # 2. Force complete with valid reason records override metadata
    completed = CompleteAppointmentAction(db_session, hospital.id, repo).execute(
        appt.id, doc_actor, force=True, override_reason="Patient left AMA with pending labs"
    )
    assert completed.status == AppointmentStatus.completed
    assert completed.clinical_override_reason == "Patient left AMA with pending labs"
    assert completed.clinical_override_actor == "Dr. Sarah"
    assert completed.clinical_override_at is not None


# ===========================================================================
# 5. DIAGNOSTIC LIFECYCLE CLOSURE INTEGRATION TEST
# ===========================================================================

def test_closure1_diagnostic_lifecycle_integration_scenarios(db_session: Session, hospital, doctor, patient):
    """Verify all 10 required diagnostic lifecycle scenarios from Closure 1 specification."""
    repo = AppointmentsRepository(db_session, hospital.id)
    workflow_svc = AppointmentWorkflowService(db_session, hospital.id)
    doc_actor = {"id": str(doctor.id), "name": "Dr. Sarah", "role": "doctor"}

    slot_counter = [0]

    def make_appt():
        slot_counter[0] += 5
        a = Appointment(
            id=uuid4(),
            hospital_id=hospital.id,
            doctor_id=doctor.id,
            patient_id=patient.id,
            appointment_date=date.today(),
            appointment_time=time(10 + (slot_counter[0] // 60), slot_counter[0] % 60),
            status=AppointmentStatus.waiting,
            consultation_status="in_consultation",
            purpose="Diagnostic Follow-up",
        )
        db_session.add(a)
        db_session.commit()
        return a

    # Scenario 1: Lab ordered -> Awaiting Lab Results
    a1 = make_appt()
    lo1 = LabOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"LAB-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=a1.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.ordered,
    )
    db_session.add(lo1)
    db_session.commit()
    wf1 = workflow_svc.derive_workflow(a1)
    assert wf1.overall_workflow_status == "awaiting_lab_results"
    assert wf1.workflow_status_label == "Awaiting Lab Results"

    # Scenario 2: Lab processing/items completed but no LabResult -> Awaiting Lab Results (NOT Results Ready)
    lo1.status = LabOrderStatus.completed
    lo1.completed_at = datetime.now(timezone.utc)
    item1 = LabOrderItem(
        id=uuid4(),
        hospital_id=hospital.id,
        order_id=lo1.id,
        test_code="CBC-001",
        test_name="CBC",
        status=LabItemStatus.completed,
    )
    db_session.add(item1)
    db_session.commit()
    wf2 = workflow_svc.derive_workflow(a1)
    assert wf2.overall_workflow_status == "awaiting_lab_results"
    assert wf2.workflow_status_label == "Awaiting Lab Results"
    with pytest.raises(AppointmentValidationError):
        ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(a1.id, doc_actor)

    # Scenario 3: Final LabResult saved -> Results Ready — Review Pending
    from modules.laboratory.entities.lab_entities import LabResult
    res1 = LabResult(
        id=uuid4(),
        hospital_id=hospital.id,
        order_id=lo1.id,
        order_item_id=item1.id,
        parameter_name="Hemoglobin",
        result_value="14.0 g/dL",
    )
    db_session.add(res1)
    db_session.commit()
    wf3 = workflow_svc.derive_workflow(a1)
    assert wf3.overall_workflow_status == "results_ready_review_pending"
    assert wf3.workflow_status_label == "Results Ready (Review Pending)"

    # Scenario 4: Radiology scheduled -> Awaiting Radiology Results
    a4 = make_appt()
    ro4 = RadiologyOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"RAD-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=a4.id,
        scan_code="CXR-001",
        scan_name="Chest X-Ray",
        status=RadiologyOrderStatus.scheduled,
    )
    db_session.add(ro4)
    db_session.commit()
    wf4 = workflow_svc.derive_workflow(a4)
    assert wf4.overall_workflow_status == "awaiting_radiology_results"
    assert wf4.workflow_status_label == "Awaiting Radiology Results"

    # Scenario 5: Scan completed, report absent -> Awaiting Radiology Results (NOT Results Ready)
    ro4.status = RadiologyOrderStatus.completed
    ro4.completed_at = datetime.now(timezone.utc)
    db_session.commit()
    wf5 = workflow_svc.derive_workflow(a4)
    assert wf5.overall_workflow_status == "awaiting_radiology_results"
    assert wf5.workflow_status_label == "Awaiting Radiology Results"
    with pytest.raises(AppointmentValidationError):
        ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(a4.id, doc_actor)

    # Scenario 6: Final radiology report saved -> Results Ready — Review Pending
    ro4.findings = "Lungs clear, no focal consolidation."
    ro4.impression = "Normal chest radiograph."
    db_session.commit()
    wf6 = workflow_svc.derive_workflow(a4)
    assert wf6.overall_workflow_status == "results_ready_review_pending"
    assert wf6.workflow_status_label == "Results Ready (Review Pending)"

    # Scenario 7: Lab final + Radiology report pending -> Awaiting Radiology Results
    a7 = make_appt()
    lo7 = LabOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"LAB-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=a7.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.completed,
    )
    db_session.add(lo7)
    res7 = LabResult(
        id=uuid4(),
        hospital_id=hospital.id,
        order_id=lo7.id,
        parameter_name="WBC",
        result_value="7.2",
    )
    db_session.add(res7)
    ro7 = RadiologyOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"RAD-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=a7.id,
        scan_code="USG-001",
        scan_name="USG Abdomen",
        status=RadiologyOrderStatus.completed,  # scan completed, report pending
    )
    db_session.add(ro7)
    db_session.commit()
    wf7 = workflow_svc.derive_workflow(a7)
    assert wf7.overall_workflow_status == "awaiting_radiology_results"
    assert wf7.workflow_status_label == "Awaiting Radiology Results"
    # Review must fail because radiology report is still pending
    with pytest.raises(AppointmentValidationError):
        ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(a7.id, doc_actor)

    # Scenario 8: Both final -> Results Ready — Review Pending
    ro7.findings = "Normal abdominal organs."
    ro7.impression = "Unremarkable USG."
    db_session.commit()
    wf8 = workflow_svc.derive_workflow(a7)
    assert wf8.overall_workflow_status == "results_ready_review_pending"
    assert wf8.workflow_status_label == "Results Ready (Review Pending)"

    # Scenario 9: All cancelled/unavailable -> Investigation Follow-up Required, NOT Results Ready
    a9 = make_appt()
    lo9 = LabOrder(
        id=uuid4(),
        hospital_id=hospital.id,
        order_no=f"LAB-{uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        appointment_id=a9.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.cancelled,
    )
    db_session.add(lo9)
    db_session.commit()
    wf9 = workflow_svc.derive_workflow(a9)
    assert wf9.overall_workflow_status == "diagnostic_attention_required"
    assert wf9.workflow_status_label == "Investigation Follow-up Required"
    with pytest.raises(AppointmentValidationError):
        ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(a9.id, doc_actor)

    # Scenario 10: Results reviewed -> Completed
    rev10 = ReviewDiagnosticsAction(db_session, hospital.id, repo).execute(
        a7.id, doc_actor, notes="All investigations reviewed and acceptable."
    )
    assert rev10.status == AppointmentStatus.completed
    assert rev10.diagnostic_review_status == "reviewed"


# ===========================================================================
# 6. CONCURRENT IDEMPOTENCY CLAIM-OR-REPLAY PROTOCOL
# ===========================================================================

def test_concurrent_idempotency_claim_or_replay_protocol(
    db_session: Session, hospital, doctor, patient
):
    """
    Simulates 2 concurrent sessions with identical idempotency key.
    Asserts:
    1. First session claims the key; concurrent second session raises ConflictError (in progress).
    2. After first session commits, second session cleanly replays the cached result.
    3. Exact database counts: appointments=1, charges=1, payments=1, allocations=1, receipts=1, idempotency records=1.
    """
    from tests.conftest import TestingSessionLocal
    from modules.appointments.entities.appointment_type import AppointmentType

    session1 = db_session
    session2 = TestingSessionLocal()
    try:
        appt_type = AppointmentType(
            id=uuid4(),
            hospital_id=hospital.id,
            name="General Consultation",
            slot_duration_minutes=15,
        )
        session1.add(appt_type)

        doctor.custom_values = {"consultation_fee": 500.0}
        session1.add(doctor)
        session1.commit()

        action1 = BookAppointmentAction(
            session1,
            hospital.id,
            AppointmentsRepository(session1, hospital.id),
            AvailabilityReader(session1, hospital.id),
            PricingReader(session1, hospital.id),
        )
        action2 = BookAppointmentAction(
            session2,
            hospital.id,
            AppointmentsRepository(session2, hospital.id),
            AvailabilityReader(session2, hospital.id),
            PricingReader(session2, hospital.id),
        )

        idemp_key = f"CONCURR-{uuid4().hex[:8]}"
        req_payload = BookAppointmentRequest(
            doctor_id=doctor.id,
            patient_id=patient.id,
            appointment_date=date.today() + timedelta(days=1),
            appointment_time=time(14, 30),
            purpose="Consultation",
            visit_type="OPD",
            appointment_type_id=appt_type.id,
            payment_method="cash",
            payment_reference="CASH-CONCURR",
        )
        actor = {
            "id": str(doctor.id),
            "name": "Super Admin",
            "role": "super_admin",
            "hospital_uuid": str(hospital.id),
        }

        # Session 1 claims the key upfront
        idemp_svc1 = IdempotencyService(session1)
        req_hash = idemp_svc1.compute_request_hash(req_payload)
        claimed1, cached1 = idemp_svc1.claim_or_replay(
            hospital.id, "appointment_booking", idemp_key, req_hash
        )
        assert claimed1 is True
        assert cached1 is None
        session1.commit()

        # Session 2 attempts concurrent claim while Session 1 is in-flight (response_code == 0)
        idemp_svc2 = IdempotencyService(session2)
        with pytest.raises(ConflictError, match="already in progress"):
            idemp_svc2.claim_or_replay(
                hospital.id, "appointment_booking", idemp_key, req_hash
            )

        # Clear preliminary probe claim row so action1.execute claims and completes end-to-end
        session1.query(IdempotencyRecord).filter(
            IdempotencyRecord.hospital_id == hospital.id,
            IdempotencyRecord.idempotency_key == idemp_key,
        ).delete()
        session1.commit()

        # Session 1 completes booking mutations and commits
        booked1 = action1.execute(req_payload, actor, idempotency_key=idemp_key)
        assert booked1.id is not None
        assert booked1.receipt_id is not None

        # Session 2 executes with the same idempotency key: cleanly replays existing booking
        booked2 = action2.execute(req_payload, actor, idempotency_key=idemp_key)
        assert booked2.id == booked1.id
        assert booked2.receipt_id == booked1.receipt_id

        # ASSERT EXACT DATABASE COUNTS
        assert session1.query(Appointment).filter(Appointment.hospital_id == hospital.id).count() == 1
        assert session1.query(BillingCharge).filter(BillingCharge.hospital_id == hospital.id).count() == 1
        assert session1.query(BillingPayment).filter(BillingPayment.hospital_id == hospital.id).count() == 1
        assert session1.query(BillingPaymentAllocation).filter(BillingPaymentAllocation.hospital_id == hospital.id).count() == 1
        assert session1.query(BillingReceipt).filter(BillingReceipt.hospital_id == hospital.id).count() == 1
        assert (
            session1.query(IdempotencyRecord)
            .filter(
                IdempotencyRecord.hospital_id == hospital.id,
                IdempotencyRecord.idempotency_key == idemp_key,
            )
            .count()
            == 1
        )
    finally:
        session2.close()


# ===========================================================================
# 7. HISTORICAL RECEIPT REPRINT BALANCE IMMUTABILITY
# ===========================================================================

def test_historical_receipt_reprint_balance_immutability(
    db_session: Session, hospital, patient
):
    """
    Verifies that subsequent patient charges do not mutate the historical reprinted
    balance of a previously issued receipt.
    """
    from modules.billing.services.billing_service import patient_ledger_totals

    # 1. Post initial charge of 500.00
    chg1 = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        description="General OPD Consultation",
        quantity=Decimal("1.0"),
        unit_price=Decimal("500.00"),
        charge_amount=Decimal("500.00"),
        net_amount=Decimal("500.00"),
        amount_paid=Decimal("500.00"),
        status=BillingChargeStatus.paid,
    )
    db_session.add(chg1)
    db_session.flush()

    # 2. Post payment of 500.00 and allocate to chg1
    pmt1 = BillingPayment(
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=Decimal("500.00"),
        payment_method=BillingPaymentMethod.cash,
        payment_date=date.today(),
        reference_number="CASH-500",
        received_by_name="Cashier 1",
    )
    db_session.add(pmt1)
    db_session.flush()

    alloc1 = BillingPaymentAllocation(
        hospital_id=hospital.id,
        payment_id=pmt1.id,
        charge_id=chg1.id,
        allocated_amount=Decimal("500.00"),
    )
    db_session.add(alloc1)
    db_session.flush()

    # 3. Issue receipt: snapshots are frozen at issuance
    rcpt = issue_receipt(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=500.00,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        payment_id=pmt1.id,
        collected_by_name="Cashier 1",
    )
    db_session.commit()

    assert rcpt.account_balance_after_snapshot is not None
    assert float(rcpt.account_balance_after_snapshot) == 0.0
    assert float(rcpt.patient_outstanding_after_snapshot) == 0.0

    # 4. Subsequent new charge posted for patient (₹1200 days later)
    chg2 = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        description="Subsequent Inpatient Service Charge",
        quantity=Decimal("1.0"),
        unit_price=Decimal("1200.00"),
        charge_amount=Decimal("1200.00"),
        net_amount=Decimal("1200.00"),
        amount_paid=Decimal("0.00"),
        status=BillingChargeStatus.pending,
    )
    db_session.add(chg2)
    db_session.commit()

    # Dynamic live ledger now reflects the new charge
    live_ledger = patient_ledger_totals(db_session, hospital.id, patient.id)
    assert float(live_ledger["outstanding"]) == 1200.0

    # 5. REPRINT historical receipt: context must remain frozen at 0.00
    ctx = build_receipt_financial_context(db_session, hospital.id, rcpt)
    assert ctx.account_balance_after_payment == 0.0
    assert ctx.patient_overall_outstanding_after_payment == 0.0

    # 6. Render receipt HTML: must render ₹0.00 and NOT reflect 1,200.00
    html = receipt_html(rcpt, hospital, patient, financial_context=ctx)
    assert "₹0.00" in html
    assert "1,200.00" not in html


# ===========================================================================
# 8. FAIL-OPEN FALLBACKS REMOVED ON LEDGER AND WORKFLOW ERRORS
# ===========================================================================

def test_fail_open_fallbacks_removed_on_ledger_and_workflow_errors(
    db_session: Session, hospital, doctor, patient, monkeypatch
):
    """
    Verifies that ledger and workflow errors result in explicit 'unavailable' statuses,
    never falling open to default 0.0, 'cleared', or 'Scheduled'.
    """
    repo = AppointmentsRepository(db_session, hospital.id)
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=date.today(),
        appointment_time=time(10, 0),
        purpose="Consultation",
        visit_type="OPD",
        status=AppointmentStatus.scheduled,
        consultation_fee=Decimal("500.00"),
    )
    db_session.add(appt)
    db_session.commit()

    # Case A: Normal hydration succeeds
    items = repo.hydrate_appointment_items([appt])
    assert len(items) == 1
    assert items[0].ledger_status in ("unbilled", "cleared", "outstanding")

    # Case B: Ledger calculation throws exception
    import modules.billing.services.billing_service as bs

    def blow_up_ledger(*args, **kwargs):
        raise RuntimeError("Ledger DB connection timeout")

    monkeypatch.setattr(bs, "patient_ledger_totals_bulk", blow_up_ledger)
    items_err = repo.hydrate_appointment_items([appt])
    assert items_err[0].ledger_status == "unavailable"
    assert items_err[0].ledger_outstanding is None
    assert items_err[0].ledger_total_charges is None
    assert items_err[0].ledger_total_paid is None

    # Case C: Workflow resolution throws exception
    import modules.appointments.services.appointment_workflow_service as ws

    def blow_up_workflow(*args, **kwargs):
        raise RuntimeError("Workflow service down")

    monkeypatch.setattr(
        ws.AppointmentWorkflowService, "bulk_derive_workflows", blow_up_workflow
    )
    items_wf_err = repo.hydrate_appointment_items([appt])
    assert items_wf_err[0].overall_workflow_status == "workflow_unavailable"
    assert items_wf_err[0].workflow_status_label == "Clinical Status Unavailable"
    assert items_wf_err[0].consultation_status == "unavailable"
    assert items_wf_err[0].investigation_status == "unavailable"


