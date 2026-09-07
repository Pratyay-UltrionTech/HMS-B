"""
Validation rules for vitals operations.

Contains pure validation functions that enforce clinical visit state rules,
query parameter requirements, and non-empty string constraints.
Free from FastAPI HTTP framework exceptions.
"""

from uuid import UUID

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.vitals.exceptions.vitals_exceptions import (
    VitalMutationNotAllowedError,
    VitalValidationError,
)


def validate_list_vitals_query(
    appointment_id: UUID | None,
    patient_id: UUID | None,
) -> None:
    """Enforce that at least one query filter (appointment_id or patient_id) is provided."""
    if not appointment_id and not patient_id:
        raise VitalValidationError("Provide appointment_id or patient_id")


def validate_vital_item_strings(
    name: str | None,
    result: str | None,
) -> tuple[str, str]:
    """Validate that name and result strings are non-empty after stripping whitespace."""
    stripped_name = (name or "").strip()
    stripped_result = (result or "").strip()
    if not stripped_name or not stripped_result:
        raise VitalValidationError("Each vital needs name and result")
    return stripped_name, stripped_result


def assert_can_mutate_vitals(appt: Appointment) -> None:
    """Verify that the appointment visit is in a state where vitals can be captured or changed."""
    if appt.status in {AppointmentStatus.cancelled, AppointmentStatus.no_show}:
        raise VitalMutationNotAllowedError("Cannot change vitals on this visit")
    if appt.status == AppointmentStatus.completed:
        raise VitalMutationNotAllowedError("Visit is already completed")
    if appt.status == AppointmentStatus.transferred_to_inpatient:
        raise VitalMutationNotAllowedError("Visit was transferred to inpatient")
