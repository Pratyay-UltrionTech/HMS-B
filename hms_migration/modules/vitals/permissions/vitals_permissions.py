"""
Module-specific authorization policies for Vitals.

Isolates Vitals access control: requires an active hospital staff or admin user,
enforces tenant scoping, and rejects super_admin or missing hospital context.
Consumes shared/auth foundation.
"""

from typing import Any
from uuid import UUID

from fastapi import Depends

from hms_migration.shared.auth import get_hospital_context, require_hospital_user


def require_vitals_access(user: dict[str, Any] = Depends(require_hospital_user)) -> dict[str, Any]:
    """
    Ensure the current user has permission to access and mutate vitals.
    Restricts access to active hospital staff and hospital admins.
    """
    return user


def get_vitals_hospital_context(hospital_id: UUID = Depends(get_hospital_context)) -> UUID:
    """
    Extract the validated hospital UUID from the user's token context.
    Ensures all database queries are isolated to the caller's tenant.
    """
    return hospital_id
