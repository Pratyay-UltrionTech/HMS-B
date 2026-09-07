"""Shared auth package."""

from hms_migration.shared.auth.dependencies import (
    get_current_user,
    get_hospital_context,
    get_hospital_uuid,
    require_hospital_admin,
    require_hospital_user,
    require_super_admin,
    security,
)
from hms_migration.shared.auth.jwt import create_access_token, decode_access_token
from hms_migration.shared.auth.security import (
    generate_temp_password,
    hash_password,
    verify_password,
)

__all__ = [
    "create_access_token",
    "decode_access_token",
    "generate_temp_password",
    "hash_password",
    "verify_password",
    "security",
    "get_current_user",
    "require_super_admin",
    "require_hospital_admin",
    "require_hospital_user",
    "get_hospital_context",
    "get_hospital_uuid",
]
