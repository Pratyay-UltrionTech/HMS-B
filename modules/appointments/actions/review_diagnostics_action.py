"""Action to review finalized diagnostic results for an outpatient appointment encounter.

Conforms to UltrionTech-Backend-Template modules/appointments/actions/ specification.
Transitions encounter from results_ready_review_pending -> reviewed -> completed.
Enforces:
1. No active/open diagnostic investigations.
2. At least one actual finalized diagnostic result (cancelled/unavailable only cannot be reviewed).
3. Authoritative capture of reviewing doctor identity, timestamp, and clinical review notes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.appointments.contracts.appointments_contracts import AppointmentListItem
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.entities.enums import AppointmentStatus
from modules.appointments.exceptions.appointments_exceptions import (
    AppointmentNotFoundError,
    AppointmentValidationError,
)
from modules.appointments.services.appointment_lifecycle import (
    get_open_clinical_blockers,
)
from modules.laboratory.services.lab_encounter_workflow_reader import (
    LabEncounterWorkflowReader,
)
from modules.radiology.services.radiology_encounter_workflow_reader import (
    RadiologyEncounterWorkflowReader,
)
from shared.audit import write_audit_log
from shared.sync import stage_sync_event


class ReviewDiagnosticsAction:
    """Action for a doctor to review finalized lab/radiology results and close the encounter."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo

    def execute(
        self,
        appointment_id: UUID,
        user: dict[str, Any],
        notes: str | None = None,
    ) -> AppointmentListItem:
        appt = self.repo.get_by_id(appointment_id)
        if not appt:
            raise AppointmentNotFoundError("Appointment not found")

        # 1. Fail closed if there are still active unfulfilled diagnostic orders
        blockers = get_open_clinical_blockers(self.db, self.hospital_id, appt.id)
        if blockers:
            raise AppointmentValidationError(
                f"Cannot close encounter: investigations are still open ({'; '.join(blockers)})"
            )

        # 2. Must require at least one actual finalized diagnostic result
        lab_summary = LabEncounterWorkflowReader(self.db).get_for_appointment(
            self.hospital_id, appt.id
        )
        rad_summary = RadiologyEncounterWorkflowReader(self.db).get_for_appointment(
            self.hospital_id, appt.id
        )
        has_final = lab_summary.has_final_result or rad_summary.has_final_result
        if not has_final:
            raise AppointmentValidationError(
                "Cannot mark diagnostics reviewed: no finalized diagnostic results exist for this appointment."
            )

        # 3. Authoritatively record review metadata
        actor_name = str(user.get("name") or user.get("sub") or "Doctor")
        appt.diagnostic_review_status = "reviewed"
        appt.diagnostics_reviewed_by = actor_name
        appt.diagnostics_reviewed_at = datetime.now(timezone.utc)
        if notes and notes.strip():
            appt.diagnostic_review_notes = notes.strip()

        # 4. If consultation was completed, mark appointment completed
        if appt.consultation_status in ("consultation_completed", "in_consultation"):
            appt.consultation_status = "consultation_completed"
            appt.status = AppointmentStatus.completed

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="CLINICAL_DIAGNOSTICS_REVIEWED",
            entity_type="appointment",
            entity_id=appt.id,
            summary=f"Doctor {actor_name} reviewed finalized diagnostic results for appointment {appt.id}",
            details={"notes": notes.strip() if notes else None},
        )
        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="appointment",
            action="review_diagnostics",
            entity_id=appt.id,
        )
        self.db.commit()
        self.db.refresh(appt)

        items = self.repo.hydrate_appointment_items([appt])
        return items[0]
