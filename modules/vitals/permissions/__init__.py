"""Vitals module authorization policies."""

from modules.vitals.permissions.vitals_permissions import (
    get_vitals_hospital_context,
    require_vitals_access,
)

__all__ = [
    "get_vitals_hospital_context",
    "require_vitals_access",
]
