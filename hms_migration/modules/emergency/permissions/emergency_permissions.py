"""
Emergency module access control and authorization policies.

Enforces valid hospital context and active staff/doctor roles.
Conforms to UltrionTech-Backend-Template modules/emergency/permissions/ specification.
"""

from typing import Any
from uuid import UUID

from fastapi import Depends

from hms_migration.shared.auth import get_hospital_context, require_hospital_user


def require_emergency_access(user: dict[str, Any] = Depends(require_hospital_user)) -> dict[str, Any]:
    """Ensure caller has active hospital user credentials."""
    return user


def get_emergency_hospital_context(hospital_id: UUID = Depends(get_hospital_context)) -> UUID:
    """Extract validated tenant UUID."""
    return hospital_id
