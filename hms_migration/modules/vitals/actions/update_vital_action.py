"""
Action to update an existing vital reading record.

Handles:
- Vital reading and appointment lookup
- Visit mutation guards
- Input validation and whitespace stripping
- Actor attribution and recorded_at timestamp update
- Audit trail logging via shared audit foundation
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from hms_migration.modules.vitals.contracts.vitals_contracts import (
    VitalItemUpdate,
    VitalReadingResponse,
    serialize_vital_reading,
)
from hms_migration.modules.vitals.db.vitals_repository import VitalsRepository
from hms_migration.modules.vitals.exceptions.vitals_exceptions import (
    VitalAppointmentNotFoundError,
    VitalReadingNotFoundError,
    VitalValidationError,
)
from hms_migration.modules.vitals.validators.vitals_validator import assert_can_mutate_vitals
from hms_migration.shared.audit import write_audit_log


class UpdateVitalAction:
    """Action orchestrating vital reading update and audit logging."""

    def __init__(self, repo: VitalsRepository) -> None:
        self.repo = repo

    def execute(
        self,
        vital_id: UUID,
        payload: VitalItemUpdate,
        user: dict[str, Any],
    ) -> VitalReadingResponse:
        row = self.repo.get_vital_by_id(vital_id)
        if not row:
            raise VitalReadingNotFoundError("Vital reading not found")
        appt = row.appointment
        if not appt or appt.hospital_id != self.repo.hospital_id:
            raise VitalAppointmentNotFoundError("Appointment not found")
        assert_can_mutate_vitals(appt)

        if payload.name is not None:
            row.name = payload.name.strip()
        if payload.result is not None:
            row.result = payload.result.strip()
        if payload.suitable_range is not None:
            row.suitable_range = payload.suitable_range.strip()

        if not row.name or not row.result:
            raise VitalValidationError("Each vital needs name and result")

        row.recorded_by_name = str(user.get("name") or row.recorded_by_name or "Staff")
        row.recorded_at = datetime.now(timezone.utc)

        patient_name = row.patient.name if row.patient else str(row.patient_id)
        write_audit_log(
            self.repo.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="update",
            entity_type="vital_reading",
            entity_id=row.id,
            summary=f"Updated vital {row.name} for {patient_name}",
        )
        self.repo.commit()
        self.repo.refresh(row)

        reloaded = self.repo.get_vital_by_id(row.id)
        return serialize_vital_reading(reloaded or row)
