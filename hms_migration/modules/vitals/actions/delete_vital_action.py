"""
Action to delete an existing vital reading record.

Handles:
- Vital reading and appointment lookup
- Visit mutation guards
- Audit trail logging via shared audit foundation
- Record deletion and transaction commit
"""

from typing import Any
from uuid import UUID

from hms_migration.modules.vitals.db.vitals_repository import VitalsRepository
from hms_migration.modules.vitals.exceptions.vitals_exceptions import (
    VitalAppointmentNotFoundError,
    VitalReadingNotFoundError,
)
from hms_migration.modules.vitals.validators.vitals_validator import assert_can_mutate_vitals
from hms_migration.shared.audit import write_audit_log


class DeleteVitalAction:
    """Action orchestrating vital reading deletion and audit logging."""

    def __init__(self, repo: VitalsRepository) -> None:
        self.repo = repo

    def execute(
        self,
        vital_id: UUID,
        user: dict[str, Any],
    ) -> None:
        row = self.repo.get_vital_by_id(vital_id)
        if not row:
            raise VitalReadingNotFoundError("Vital reading not found")
        appt = row.appointment
        if not appt or appt.hospital_id != self.repo.hospital_id:
            raise VitalAppointmentNotFoundError("Appointment not found")
        assert_can_mutate_vitals(appt)

        patient_name = row.patient.name if row.patient else str(row.patient_id)
        write_audit_log(
            self.repo.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="delete",
            entity_type="vital_reading",
            entity_id=row.id,
            summary=f"Deleted vital {row.name} for {patient_name}",
        )
        self.repo.delete(row)
        self.repo.commit()
