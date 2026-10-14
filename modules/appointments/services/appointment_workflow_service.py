"""Canonical Encounter & Appointment Workflow Lifecycle Service.

Conforms to UltrionTech-Backend-Template modules/appointments/services/ specification.
Provides authoritative separation of:
1. Consultation lifecycle (scheduled -> checked_in -> in_consultation -> consultation_completed)
2. Diagnostic investigation lifecycle (not_required -> awaiting_results -> ready_for_review -> reviewed -> diagnostic_attention_required)
3. Derived overall workflow status & display labels:
   - scheduled -> "Scheduled"
   - checked_in -> "Checked In"
   - in_consultation -> "In Consultation"
   - consultation_completed -> "Consultation Completed"
   - awaiting_lab_results -> "Awaiting Lab Results"
   - awaiting_radiology_results -> "Awaiting Radiology Results"
   - awaiting_lab_and_radiology_results -> "Awaiting Lab & Radiology Results"
   - investigations_in_progress -> "Investigations In Progress"
   - results_ready_review_pending -> "Results Ready — Doctor Review Pending"
   - diagnostic_attention_required -> "Investigation Follow-up Required"
   - completed -> "Completed"
   - cancelled -> "Cancelled"
   - no_show -> "No Show"

Consumes semantic summaries from Laboratory and Radiology workflow readers.
Contains zero direct Core table declarations for lab_orders, lab_prescription_requests,
radiology_orders, or rad_prescription_requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.laboratory.services.lab_encounter_workflow_reader import (
    LabEncounterWorkflowReader,
)
from modules.radiology.services.radiology_encounter_workflow_reader import (
    RadiologyEncounterWorkflowReader,
)
from shared.diagnostics.contracts import DiagnosticDomainSummary


@dataclass
class WorkflowSummary:
    consultation_status: str
    investigation_status: str
    overall_workflow_status: str
    workflow_status_label: str
    canonical_review_status: str = "not_required"
    diagnostic_summary: dict[str, Any] = field(default_factory=dict)


def _format_workflow_label(status_code: str) -> str:
    labels = {
        "scheduled": "Scheduled",
        "checked_in": "Checked In",
        "in_consultation": "In Consultation",
        "consultation_completed": "Consultation Completed",
        "awaiting_lab_results": "Awaiting Lab Results",
        "awaiting_radiology_results": "Awaiting Radiology Results",
        "awaiting_lab_and_radiology_results": "Awaiting Lab & Radiology Results",
        "investigations_in_progress": "Investigations In Progress",
        "results_ready_review_pending": "Results Ready — Doctor Review Pending",
        "diagnostic_attention_required": "Investigation Follow-up Required",
        "completed": "Completed",
        "cancelled": "Cancelled",
        "no_show": "No Show",
        "transferred_to_inpatient": "Transferred to Inpatient",
    }
    return labels.get(status_code, status_code.replace("_", " ").title())


class AppointmentWorkflowService:
    """Canonical evaluator for encounter workflow states."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.lab_reader = LabEncounterWorkflowReader(db)
        self.rad_reader = RadiologyEncounterWorkflowReader(db)

    def bulk_get_diagnostic_states(
        self, appointment_ids: list[UUID]
    ) -> tuple[dict[UUID, DiagnosticDomainSummary], dict[UUID, DiagnosticDomainSummary]]:
        """Fetch domain summaries for appointments via Lab & Radiology readers."""
        if not appointment_ids:
            return {}, {}
        lab_summaries = self.lab_reader.bulk_get_for_appointments(self.hospital_id, appointment_ids)
        rad_summaries = self.rad_reader.bulk_get_for_appointments(self.hospital_id, appointment_ids)
        return lab_summaries, rad_summaries

    @staticmethod
    def compute_workflow_status(
        consultation_status: str | None = None,
        diagnostic_review_status: str | None = None,
        lab_summary: DiagnosticDomainSummary | None = None,
        rad_summary: DiagnosticDomainSummary | None = None,
        status: AppointmentStatus | None = None,
    ) -> tuple[str | None, str, str]:
        """Pure evaluator returning (investigation_status, overall_workflow_status, workflow_status_label)."""
        if lab_summary is None:
            lab_summary = DiagnosticDomainSummary(domain="laboratory")
        if rad_summary is None:
            rad_summary = DiagnosticDomainSummary(domain="radiology")

        c_status = consultation_status or "scheduled"
        if status == AppointmentStatus.cancelled:
            c_status = "cancelled"
        elif status == AppointmentStatus.no_show:
            c_status = "no_show"
        elif status == AppointmentStatus.completed:
            c_status = "consultation_completed"
        elif status == AppointmentStatus.scheduled and c_status != "in_consultation":
            c_status = "scheduled"

        has_diagnostics = (
            lab_summary.requested_count
            + lab_summary.active_order_count
            + lab_summary.finalized_result_count
            + lab_summary.cancelled_count
            + lab_summary.unavailable_count
            + rad_summary.requested_count
            + rad_summary.active_order_count
            + rad_summary.finalized_result_count
            + rad_summary.cancelled_count
            + rad_summary.unavailable_count
            + (1 if lab_summary.has_open_work or lab_summary.has_final_result else 0)
            + (1 if rad_summary.has_open_work or rad_summary.has_final_result else 0)
        ) > 0

        open_lab = lab_summary.has_open_work
        open_rad = rad_summary.has_open_work
        has_open = open_lab or open_rad
        has_final_result = lab_summary.has_final_result or rad_summary.has_final_result
        persisted_review = diagnostic_review_status

        attention_required = (
            has_diagnostics
            and not has_open
            and not has_final_result
            and (lab_summary.requires_doctor_attention or rad_summary.requires_doctor_attention)
        )

        all_cancelled = (
            has_diagnostics
            and not has_open
            and not has_final_result
            and not attention_required
            and (lab_summary.cancelled_count > 0 or rad_summary.cancelled_count > 0)
        )

        # Investigation status
        any_in_progress = getattr(lab_summary, "has_in_progress", False) or getattr(rad_summary, "has_in_progress", False)
        inv_status: str | None = None
        if has_diagnostics:
            if any_in_progress:
                inv_status = "in_progress"
            elif open_lab and open_rad:
                inv_status = "awaiting_lab_and_radiology"
            elif open_lab:
                inv_status = "awaiting_lab"
            elif open_rad:
                inv_status = "awaiting_radiology"
            elif has_final_result:
                if persisted_review == "reviewed":
                    inv_status = "reviewed"
                else:
                    inv_status = "results_ready"
            elif all_cancelled:
                inv_status = "cancelled"
            elif attention_required:
                inv_status = "attention_required"
            else:
                inv_status = "not_required"
        else:
            inv_status = None

        # Overall workflow status
        overall = "scheduled"
        if status == AppointmentStatus.cancelled or c_status == "cancelled":
            overall = "cancelled"
        elif status == AppointmentStatus.no_show or c_status == "no_show":
            overall = "no_show"
        elif status == AppointmentStatus.transferred_to_inpatient:
            overall = "transferred_to_inpatient"
        elif any_in_progress:
            overall = "investigations_in_progress"
        elif open_lab and open_rad:
            overall = "awaiting_lab_and_radiology_results"
        elif open_lab:
            overall = "awaiting_lab_results"
        elif open_rad:
            overall = "awaiting_radiology_results"
        elif has_final_result and persisted_review != "reviewed":
            overall = "results_ready_review_pending"
        elif attention_required:
            overall = "diagnostic_attention_required"
        elif (
            status == AppointmentStatus.completed
            or (
                c_status == "consultation_completed"
                and (not has_diagnostics or persisted_review == "reviewed")
            )
        ):
            if status == AppointmentStatus.completed:
                overall = "completed"
            else:
                overall = "consultation_completed"
        elif c_status == "consultation_completed":
            overall = "consultation_completed"
        elif c_status == "in_consultation":
            overall = "in_consultation"
        elif status == AppointmentStatus.waiting or c_status == "waiting":
            overall = "checked_in"
        else:
            overall = "scheduled"

        label = _format_workflow_label(overall)
        return inv_status, overall, label

    def derive_workflow(
        self,
        appt: Appointment,
        lab_summary: DiagnosticDomainSummary | None = None,
        rad_summary: DiagnosticDomainSummary | None = None,
    ) -> WorkflowSummary:
        """Derive consultation_status, investigation_status, overall_workflow_status, and display label."""
        if lab_summary is None or rad_summary is None:
            lab_map, rad_map = self.bulk_get_diagnostic_states([appt.id])
            lab_summary = lab_map.get(appt.id, DiagnosticDomainSummary(domain="laboratory"))
            rad_summary = rad_map.get(appt.id, DiagnosticDomainSummary(domain="radiology"))

        inv_status, overall, label = self.compute_workflow_status(
            consultation_status=getattr(appt, "consultation_status", None),
            diagnostic_review_status=getattr(appt, "diagnostic_review_status", None),
            lab_summary=lab_summary,
            rad_summary=rad_summary,
            status=appt.status,
        )

        open_lab = lab_summary.has_open_work
        open_rad = rad_summary.has_open_work
        has_open = open_lab or open_rad
        has_final_result = lab_summary.has_final_result or rad_summary.has_final_result
        attention_required = (
            not has_open
            and not has_final_result
            and (lab_summary.requires_doctor_attention or rad_summary.requires_doctor_attention)
        )
        persisted_review = getattr(appt, "diagnostic_review_status", None)

        if not (open_lab or open_rad or has_final_result or lab_summary.cancelled_count > 0 or rad_summary.cancelled_count > 0):
            canonical_review_status = "not_required"
        elif has_open:
            canonical_review_status = "awaiting_results"
        elif attention_required:
            canonical_review_status = "attention_required"
        elif has_final_result:
            if persisted_review == "reviewed":
                canonical_review_status = "reviewed"
            else:
                canonical_review_status = "ready_for_review"
        else:
            canonical_review_status = "not_required"

        return WorkflowSummary(
            consultation_status=getattr(appt, "consultation_status", None) or "scheduled",
            investigation_status=inv_status or "not_required",
            overall_workflow_status=overall,
            workflow_status_label=label,
            canonical_review_status=canonical_review_status,
            diagnostic_summary={
                "total_lab": lab_summary.requested_count + lab_summary.active_order_count + lab_summary.finalized_result_count,
                "open_lab": 1 if lab_summary.has_open_work else 0,
                "total_radiology": rad_summary.requested_count + rad_summary.active_order_count + rad_summary.finalized_result_count,
                "open_radiology": 1 if rad_summary.has_open_work else 0,
                "has_open": has_open,
                "has_final_result": has_final_result,
                "requires_attention": attention_required,
                "blockers": lab_summary.blocker_descriptions + rad_summary.blocker_descriptions,
            },
        )


    def bulk_derive_workflows(
        self, appointments: list[Appointment]
    ) -> dict[UUID, WorkflowSummary]:
        """Bulk evaluates workflow summaries for a list of appointments."""
        if not appointments:
            return {}
        appt_ids = [a.id for a in appointments]
        lab_map, rad_map = self.bulk_get_diagnostic_states(appt_ids)
        return {
            a.id: self.derive_workflow(
                a,
                lab_map.get(a.id, DiagnosticDomainSummary(domain="laboratory")),
                rad_map.get(a.id, DiagnosticDomainSummary(domain="radiology")),
            )
            for a in appointments
        }
