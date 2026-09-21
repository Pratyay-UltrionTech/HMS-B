"""
FastAPI authentication and authorization dependencies.

Conforms to UltrionTech-Backend-Template shared/auth/ specification.
Preserves 100% behavioral equivalence with OG HMS-B endpoint security.
"""

import time
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from config.settings import Settings, get_settings

security = HTTPBearer()

# FLAW-008: In-memory TTL cache of role permissions keyed by (hospital_uuid, role_id).
# Each entry maps module_key -> {"can_view": bool, "can_edit": bool} and carries the
# timestamp at which it was loaded. The cache is refreshed after PERMISSION_CACHE_TTL
# seconds so that admin permission changes propagate without a restart.
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


# ── FLAW-008: Granular role-permission enforcement ────────────────────────────

def invalidate_role_permission_cache(hospital_id: str | UUID, role_id: str | UUID) -> None:
    """Drop the cached permissions for a single (hospital, role) pair.

    Called by admin mutation flows (role permission / role-active / user role
    changes) so that in-memory permission caches are not served stale. The TTL
    cache is self-healing anyway, but explicit invalidation avoids a window
    where a freshly-revoked permission is still honored.
    """
    _role_permission_cache.pop((str(hospital_id), str(role_id)), None)


def _load_role_permissions(db, hospital_uuid: str, role_id: str) -> dict[str, dict[str, bool]]:
    """Query RolePermission rows for a (hospital, role) and return module-key map."""
    from modules.doctors.entities.doctor import RolePermission

    rows = (
        db.query(RolePermission)
        .filter(
            RolePermission.hospital_id == UUID(hospital_uuid),
            RolePermission.role_id == UUID(role_id),
        )
        .all()
    )
    return {
        r.module_key: {"can_view": bool(r.can_view), "can_edit": bool(r.can_edit)}
        for r in rows
    }


def _get_cached_role_permissions(db, hospital_uuid: str, role_id: str) -> dict[str, dict[str, bool]]:
    """Return role permissions, populating the TTL cache on miss or expiry."""
    key = (hospital_uuid, role_id)
    now = time.monotonic()
    cached = _role_permission_cache.get(key)
    if cached and (now - cached[0]) < _PERMISSION_CACHE_TTL:
        return cached[1]
    perms = _load_role_permissions(db, hospital_uuid, role_id)
    _role_permission_cache[key] = (now, perms)
    return perms


def require_permission(module_key: str, action: str = "view"):
    """
    FastAPI dependency enforcing granular role-permission on a route.

    Semantics:
      - super_admin / hospital_admin bypass permission checks (full access).
      - hospital_staff must hold a RolePermission for `module_key` in the
        current hospital with the requested capability:
            action == "edit" -> requires can_edit
            action == "view" -> requires can_view
    Permissions are read from `hospital_role_permissions` (role_permissions)
    keyed by the caller's staff_role_id (from the JWT) and cached in-memory by
    (hospital_uuid, role_id) for PERMISSION_CACHE_TTL seconds.

    Usage: `@router.post("/x", dependencies=[Depends(require_permission("billing", "edit"))])`
    """
    from infrastructure.postgres.session import get_transitional_sync_session

    def _dependency(
        user: dict[str, Any] = Depends(require_hospital_user),
        db: Any = Depends(get_transitional_sync_session),
    ) -> dict[str, Any]:
        role = user.get("role")
        if role in {"super_admin", "hospital_admin"}:
            return user

        role_id = user.get("staff_role_id")
        hospital_uuid = user.get("hospital_uuid")
        if not role_id or not hospital_uuid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Role permission context missing from token",
            )

        perms = _get_cached_role_permissions(db, str(hospital_uuid), str(role_id))
        granted = perms.get(module_key)
        required = "can_edit" if action == "edit" else "can_view"
        if not granted or not granted.get(required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: missing {module_key}:{action} permission",
            )
        return user

    return _dependency
