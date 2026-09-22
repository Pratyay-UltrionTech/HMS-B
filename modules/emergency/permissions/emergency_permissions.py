"""
Emergency module access control and authorization policies.

Enforces valid hospital context and active staff/doctor roles.
Conforms to UltrionTech-Backend-Template modules/emergency/permissions/ specification.
"""

from typing import Any
from uuid import UUID

from fastapi import Depends

from shared.auth import get_hospital_context, require_permission


def require_emergency_access(user: dict[str, Any] = Depends(require_permission("emergency", "view"))) -> dict[str, Any]:
    """Ensure caller has active emergency view permissions (or is admin)."""
    return user


def require_emergency_edit(user: dict[str, Any] = Depends(require_permission("emergency", "edit"))) -> dict[str, Any]:
    """Ensure caller has active emergency edit permissions (or is admin)."""
    return user


def require_emergency_administer(user: dict[str, Any] = Depends(require_permission("emergency", "administer"))) -> dict[str, Any]:
    """Ensure caller has active emergency medication/order administration permissions (or is admin)."""
    return user


def get_emergency_hospital_context(hospital_id: UUID = Depends(get_hospital_context)) -> UUID:
    """Extract validated tenant UUID."""
    return hospital_id
