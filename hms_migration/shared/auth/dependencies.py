"""
FastAPI authentication and authorization dependencies.

Conforms to UltrionTech-Backend-Template shared/auth/ specification.
Preserves 100% behavioral equivalence with OG HMS-B endpoint security.
"""

from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from hms_migration.config.settings import Settings, get_settings

security = HTTPBearer()


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
