"""
Appointment authorization and tenancy permission policies.

Conforms to UltrionTech-Backend-Template modules/appointments/permissions/ specification.
"""

from typing import Any
from uuid import UUID

from fastapi import Depends

from hms_migration.shared.auth import get_hospital_context, require_hospital_user


def get_appointment_context(
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> tuple[dict[str, Any], UUID]:
    """Extract authenticated staff user context and tenant hospital UUID."""
    return user, hospital_id
