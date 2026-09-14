"""
Action to query vital readings by appointment or patient.
"""

from uuid import UUID

from modules.vitals.contracts.vitals_contracts import (
    VitalReadingResponse,
    serialize_vital_reading,
)
from modules.vitals.db.vitals_repository import VitalsRepository
from modules.vitals.validators.vitals_validator import validate_list_vitals_query


class ListVitalsAction:
    """Action that validates filter params and lists vital readings."""

    def __init__(self, repo: VitalsRepository) -> None:
        self.repo = repo

    def execute(
        self,
        appointment_id: UUID | None = None,
        patient_id: UUID | None = None,
    ) -> list[VitalReadingResponse]:
        validate_list_vitals_query(appointment_id, patient_id)
        rows = self.repo.list_vitals(appointment_id=appointment_id, patient_id=patient_id)
        return [serialize_vital_reading(r) for r in rows]
