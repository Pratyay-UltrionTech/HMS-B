"""
FastAPI authentication and authorization dependencies.

Conforms to UltrionTech-Backend-Template shared/auth/ specification.
Preserves 100% behavioral equivalence with OG HMS-B endpoint security.
Admin functionality (super_admin, hospital_admin) remains frozen and fully bypassed.
"""

import time
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from config.settings import Settings, get_settings

security = HTTPBearer()

# In-memory TTL cache of role permissions keyed by (hospital_uuid, role_id).
# Each entry maps module_key -> action_dict and carries timestamp.
_PERMISSION_CACHE_TTL = 60  # seconds
_role_permission_cache: dict[tuple[str, str], tuple[float, dict[str, dict[str, bool]]]] = {}


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Validate Bearer JWT and return user claims payload."""
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc

    role = payload.get("role")
    if role not in {"super_admin", "hospital_admin", "hospital_staff"}:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token role",
        )
    return payload


def require_super_admin(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Require super_admin role."""
    if user.get("role") != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin access required",
        )
    return user


def require_hospital_user(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Require hospital_admin or hospital_staff role with non-empty hospital_uuid."""
    if user.get("role") not in {"hospital_admin", "hospital_staff"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hospital access required",
        )
    if not user.get("hospital_uuid"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hospital context missing from token",
        )
    return user


def require_hospital_admin(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Require hospital_admin role with non-empty hospital_uuid."""
    if user.get("role") != "hospital_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hospital admin access required",
        )
    if not user.get("hospital_uuid"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hospital context missing from token",
        )
    return user


def get_hospital_context(
    user: dict[str, Any] = Depends(get_current_user),
) -> UUID:
    """Extract and parse hospital_uuid from user claims for clinical modules."""
    if user.get("role") not in {"hospital_admin", "hospital_staff"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hospital access required",
        )
    try:
        return UUID(str(user["hospital_uuid"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid hospital context in token",
        ) from exc


def get_hospital_uuid(
    user: dict[str, Any] = Depends(get_current_user),
) -> UUID:
    """Extract hospital_uuid for hospital_admin only."""
    if user.get("role") != "hospital_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hospital admin access required",
        )
    try:
        return UUID(str(user["hospital_uuid"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid hospital context in token",
        ) from exc


from shared.auth.registry import ACTION_FLAG_MAP, MODULE_REGISTRY, parse_action
from shared.auth.service import AuthDecision, AuthReasonCode, authorization


# ── Role-permission enforcement with multiple-role union & granular actions ───

def invalidate_role_permission_cache(hospital_id: str | UUID, role_id: str | UUID) -> None:
    """Drop the cached permissions for a single (hospital, role) pair."""
    authorization.invalidate_cache(hospital_id, role_id)
    _role_permission_cache.pop((str(hospital_id), str(role_id)), None)


def _load_role_permissions(db, hospital_uuid: str, role_id: str) -> dict[str, dict[str, bool]]:
    """Query RolePermission rows for a (hospital, role) and return module-key map."""
    return authorization._load_role_permissions(db, hospital_uuid, role_id)


def _get_cached_role_permissions(db, hospital_uuid: str, role_id: str) -> dict[str, dict[str, bool]]:
    """Return role permissions, populating the TTL cache on miss or expiry."""
    return authorization.get_role_permissions(db, hospital_uuid, role_id)


ACTION_KEY_MAP = ACTION_FLAG_MAP


def require_permission(module_key: str, action: str = "view"):
    """
    FastAPI dependency enforcing granular role-permission on a route.

    Semantics:
      - super_admin / hospital_admin bypass permission checks (100% full access).
      - hospital_staff aggregates permissions across ALL assigned roles (union).
      - Action flags checked against union of permissions:
          can_view, can_edit, can_create, can_delete, can_approve, can_validate,
          can_release, can_dispense, can_refund, can_cancel, can_administer.
      - Delegates to canonical AuthorizationService.
    """
    from infrastructure.postgres.session import get_transitional_sync_session

    def _dependency(
        user: dict[str, Any] = Depends(require_hospital_user),
        db: Any = Depends(get_transitional_sync_session),
    ) -> dict[str, Any]:
        return authorization.require(user, (module_key, action), resource=None, db=db)

    return _dependency


def assert_practitioner_identity(current_user: dict[str, Any], target_doctor_id: UUID | str) -> None:
    """
    Assert that the authenticated user is either an Admin (bypass) or the
    specific practitioner identified by target_doctor_id.
    """
    role = current_user.get("role")
    if role in {"super_admin", "hospital_admin"}:
        return

    caller_id = current_user.get("user_id")
    if not caller_id or str(caller_id) != str(target_doctor_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: clinical actions must be authored by the designated practitioner",
        )


def get_user_department_ids(user: dict[str, Any], db: Any) -> list[UUID]:
    """
    Return list of assigned department UUIDs for staff user, or empty list for Admin (unscoped).
    """
    if user.get("role") in {"super_admin", "hospital_admin"}:
        return []

    dept_ids = user.get("department_ids")
    if dept_ids:
        return [UUID(str(d)) for d in dept_ids]

    user_id = user.get("user_id")
    if not user_id:
        return []

    from modules.doctors.entities.doctor import HospitalUserDepartment, HospitalUser
    user_uuid = UUID(str(user_id))
    h_uuid = UUID(str(user.get("hospital_uuid")))

    assigned = db.query(HospitalUserDepartment.department_id).filter(
        HospitalUserDepartment.hospital_id == h_uuid,
        HospitalUserDepartment.user_id == user_uuid
    ).all()
    if assigned:
        return [r[0] for r in assigned]

    h_user = db.query(HospitalUser.department_id).filter(HospitalUser.id == user_uuid).first()
    if h_user and h_user[0]:
        return [h_user[0]]

    return []


def get_user_ward_ids(user: dict[str, Any], db: Any) -> list[UUID]:
    """
    Return list of assigned ward UUIDs for staff user, or empty list for Admin (unscoped).
    """
    if user.get("role") in {"super_admin", "hospital_admin"}:
        return []

    user_id = user.get("user_id")
    if not user_id:
        return []

    from modules.doctors.entities.doctor import HospitalUserLocation
    user_uuid = UUID(str(user_id))
    h_uuid = UUID(str(user.get("hospital_uuid")))

    locations = db.query(HospitalUserLocation.ward_id).filter(
        HospitalUserLocation.hospital_id == h_uuid,
        HospitalUserLocation.user_id == user_uuid,
        HospitalUserLocation.ward_id.isnot(None)
    ).all()
    return [r[0] for r in locations]
