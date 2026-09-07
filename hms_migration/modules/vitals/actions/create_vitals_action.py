"""
Action to batch record vital readings for an outpatient visit.

Handles:
- Appointment existence and mutation validation
- Item name and result whitespace validation
- Persistence of vital readings
- Scheduled → Waiting (checked-in) appointment transition
- Sequential queue-token generation
- Audit trail logging via shared audit foundation
"""

from typing import Any

from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.appointments.services.appointment_lifecycle import mark_in_progress
from hms_migration.modules.vitals.entities.vital_reading import VitalReading

from hms_migration.modules.vitals.contracts.vitals_contracts import (
    VitalBatchCreate,
    VitalReadingResponse,
    serialize_vital_reading,
)
from hms_migration.modules.vitals.db.vitals_repository import VitalsRepository
from hms_migration.modules.vitals.exceptions.vitals_exceptions import (
    VitalAppointmentNotFoundError,
)
from hms_migration.modules.vitals.validators.vitals_validator import (
    assert_can_mutate_vitals,
    validate_vital_item_strings,
)
from hms_migration.shared.audit import write_audit_log


class CreateVitalsAction:
    """Action orchestrating vital readings creation and appointment check-in."""

    def __init__(self, repo: VitalsRepository) -> None:
        self.repo = repo

    def execute(
        self,
        payload: VitalBatchCreate,
        user: dict[str, Any],
    ) -> list[VitalReadingResponse]:
        appt = self.repo.appointment_reader.get_appointment_by_id(payload.appointment_id)
        if not appt:
            raise VitalAppointmentNotFoundError("Appointment not found")
        assert_can_mutate_vitals(appt)

        actor = str(user.get("name") or "Staff")
        created: list[VitalReading] = []
        for item in payload.items:
            name, result = validate_vital_item_strings(item.name, item.result)
            suitable = (item.suitable_range or "").strip()
            row = VitalReading(
                hospital_id=self.repo.hospital_id,
                appointment_id=appt.id,
                patient_id=appt.patient_id,
                name=name,
                suitable_range=suitable,
                result=result,
                recorded_by_name=actor,
            )
            self.repo.add(row)
            created.append(row)

        self.repo.flush()

        # Vitals recorded → Checked in (waiting)
        if appt.status == AppointmentStatus.scheduled:
            mark_in_progress(appt, assign_checked_in=True)
            if not appt.queue_token:
                appt.queue_token = self.repo.appointment_reader.get_next_queue_token(
                    appt.doctor_id,
                    appt.appointment_date,
                )

        patient_name = appt.patient.name if appt.patient else str(appt.patient_id)
        vitals_summary = ", ".join(r.name for r in created)
        write_audit_log(
            self.repo.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="vital_reading",
            entity_id=created[0].id,
            summary=f"Vitals recorded for {patient_name}: {vitals_summary} → Checked in",
        )
        self.repo.commit()

        rows = self.repo.get_vitals_by_ids([r.id for r in created])
        return [serialize_vital_reading(r) for r in rows]
