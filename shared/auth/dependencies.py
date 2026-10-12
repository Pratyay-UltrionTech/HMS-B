"""
FastAPI authentication and authorization dependencies.

Conforms to UltrionTech-Backend-Template shared/auth/ specification.
Preserves 100% behavioral equivalence with OG HMS-B endpoint security.
Admin functionality (super_admin, hospital_admin) remains frozen and fully bypassed.
"""

import time
from typing import Any, Sequence
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from config.settings import Settings, get_settings
from infrastructure.postgres.session import get_transitional_sync_session

security = HTTPBearer()


# Zero-cache compliance: HMS enforces direct, transactional database authorization.
def invalidate_tenant_status_cache(hospital_uuid: str | UUID | None = None) -> None:
    """Deprecated no-op: HMS is strictly cache-free; authorization queries authoritative DB state."""
    pass


def check_hospital_tenant_active(hospital_uuid: str | UUID, db: Any) -> None:
    """Authoritative direct database check for operational status (NO CACHE)."""
    from modules.tenancy.entities.hospital import Hospital

    try:
        h_uuid = UUID(str(hospital_uuid))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid hospital context in token",
        )

    row = db.query(Hospital.is_active, Hospital.status).filter(Hospital.id == h_uuid).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hospital tenant no longer exists",
        )
    is_active = bool(row[0])
    status_val = str(row[1] or ("active" if is_active else "suspended"))
    if not is_active or status_val in {"suspended", "archived"}:
        detail_msg = (
            "Hospital account is suspended."
            if status_val == "suspended"
            else "Hospital tenant is archived and decommissioned. Operational access is closed; records are retained for historical audit."
            if status_val == "archived"
            else "Hospital account is suspended or inactive."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail_msg,
        )


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Any = Depends(get_transitional_sync_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Validate Bearer JWT and return server-authoritative principal payload (strictly cache-free)."""
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

    if role == "super_admin":
        return payload

    # For hospital principals, validate tenant operational status in authoritative DB
    h_uuid_raw = payload.get("hospital_uuid")
    if not h_uuid_raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hospital context missing from token",
        )
    try:
        h_uuid = UUID(str(h_uuid_raw))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid hospital context in token",
        ) from exc

    from modules.tenancy.entities.hospital import Hospital
    from modules.doctors.entities.doctor import HospitalUser

    hosp = db.query(Hospital).filter(Hospital.id == h_uuid).first()
    if not hosp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hospital tenant no longer exists",
        )

    status_val = str(getattr(hosp, "status", None) or ("active" if hosp.is_active else "suspended"))
    if not hosp.is_active or status_val in {"suspended", "archived"}:
        detail_msg = (
            "Hospital account is suspended."
            if status_val == "suspended"
            else "Hospital tenant is archived and decommissioned. Operational access is closed; records are retained for historical audit."
            if status_val == "archived"
            else "Hospital account is suspended or inactive."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail_msg,
        )

    user_id_raw = payload.get("user_id")
    if user_id_raw:
        try:
            u_uuid = UUID(str(user_id_raw))
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user context in token",
            ) from exc

        staff = db.query(HospitalUser).filter(HospitalUser.id == u_uuid, HospitalUser.hospital_id == h_uuid).first()
        if not staff:
            if getattr(hosp, "auth_model", None) == "legacy_organization" or payload.get("role") == "hospital_admin":
                payload["hospital_uuid"] = str(hosp.id)
                payload["hospital_id"] = hosp.hospital_id
                return payload
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account no longer exists.",
            )
        if not staff.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account is inactive.",
            )

        token_v = payload.get("token_version")
        if token_v is not None and staff.token_version > token_v:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or revoked. Please log in again.",
            )

        # Authoritative role resolution: determine if user is hospital_admin
        is_admin = False
        if staff.role and (getattr(staff.role, "is_admin_role", False) or getattr(staff.role, "code", None) == "HOSPITAL_ADMIN"):
            is_admin = True
        elif staff.assigned_roles:
            for ur in staff.assigned_roles:
                if ur.role and (getattr(ur.role, "is_admin_role", False) or getattr(ur.role, "code", None) == "HOSPITAL_ADMIN") and ur.role.is_active:
                    is_admin = True
                    break

        if is_admin or payload.get("role") == "hospital_admin":
            payload["role"] = "hospital_admin"
        else:
            payload["role"] = "hospital_staff"

        payload["hospital_uuid"] = str(hosp.id)
        payload["hospital_id"] = hosp.hospital_id
        return payload

    # If no user_id in token: only legacy hospital row accounts allowed
    auth_model = getattr(hosp, "auth_model", "user_accounts")
    if auth_model == "user_accounts":
        has_users = db.query(HospitalUser.id).filter(HospitalUser.hospital_id == h_uuid).first()
        if has_users:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: human administrator account required.",
            )

    payload["hospital_uuid"] = str(hosp.id)
    payload["hospital_id"] = hosp.hospital_id
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


def get_active_hospital_tenant(
    user: dict[str, Any] = Depends(get_current_user),
) -> UUID:
    """Request-scoped dependency: extracts validated active hospital UUID from principal."""
    role = user.get("role")
    if role not in {"hospital_admin", "hospital_staff"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hospital access required",
        )
    return UUID(str(user["hospital_uuid"]))


def require_hospital_user(
    user: dict[str, Any] = Depends(get_current_user),
    _: UUID = Depends(get_active_hospital_tenant),
) -> dict[str, Any]:
    """Require hospital_admin or hospital_staff role with active tenant in database."""
    return user


def require_hospital_admin(
    user: dict[str, Any] = Depends(get_current_user),
    _: UUID = Depends(get_active_hospital_tenant),
) -> dict[str, Any]:
    """Require hospital_admin role with active tenant in database."""
    if user.get("role") != "hospital_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hospital admin access required",
        )
    return user


def get_hospital_context(
    hospital_uuid: UUID = Depends(get_active_hospital_tenant),
) -> UUID:
    """Extract and return validated active hospital UUID for clinical routes."""
    return hospital_uuid


def get_hospital_uuid(
    user: dict[str, Any] = Depends(require_hospital_admin),
    hospital_uuid: UUID = Depends(get_active_hospital_tenant),
) -> UUID:
    """Extract hospital_uuid for hospital_admin only, ensuring active tenant in database."""
    return hospital_uuid



from shared.auth.registry import ACTION_FLAG_MAP, MODULE_REGISTRY, parse_action
from shared.auth.service import AuthDecision, AuthReasonCode, authorization


def invalidate_role_permission_cache(hospital_id: str | UUID, role_id: str | UUID) -> None:
    """Deprecated no-op: HMS is strictly cache-free; permissions query authoritative DB state."""
    pass


def _load_role_permissions(db, hospital_uuid: str, role_id: str) -> dict[str, dict[str, bool]]:
    """Query RolePermission rows for a (hospital, role) and return module-key map."""
    return authorization._load_role_permissions(db, hospital_uuid, role_id)


def _get_cached_role_permissions(db, hospital_uuid: str, role_id: str) -> dict[str, dict[str, bool]]:
    """Return fresh role permissions from DB (NO CACHE)."""
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


def require_any_permission(allowed_specs: Sequence[tuple[str, str]]):
    """
    FastAPI dependency enforcing that the user has at least one of the specified
    (module_key, action) permissions (e.g. view permissions across doctors, nurses, all_ipd, bed).
    Admin roles (super_admin, hospital_admin) bypass checks.
    """
    from infrastructure.postgres.session import get_transitional_sync_session

    def _dependency(
        user: dict[str, Any] = Depends(require_hospital_user),
        db: Any = Depends(get_transitional_sync_session),
    ) -> dict[str, Any]:
        role = user.get("role")
        if role in {"super_admin", "hospital_admin"}:
            return user

        for module_key, action in allowed_specs:
            decision = authorization.evaluate(user, (module_key, action), resource=None, db=db)
            if decision.allowed:
                return user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: required permission not found in user roles",
        )

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
