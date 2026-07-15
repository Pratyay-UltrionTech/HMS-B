from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db

security = HTTPBearer()
settings = get_settings()


def create_access_token(data: dict[str, Any]) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = decode_access_token(credentials.credentials)
    role = payload.get("role")
    if role not in {"super_admin", "hospital_admin", "hospital_staff"}:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token role")
    return payload


def require_super_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin access required")
    return user


def require_hospital_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") != "hospital_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hospital admin access required")
    if not user.get("hospital_uuid"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Hospital context missing from token")
    return user


def get_hospital_uuid(user: dict[str, Any] = Depends(get_current_user)) -> UUID:
    """Hospital admin only — used for masters/admin configuration APIs."""
    if user.get("role") != "hospital_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hospital admin access required")
    try:
        return UUID(str(user["hospital_uuid"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid hospital context in token",
        ) from exc


def get_hospital_context(user: dict[str, Any] = Depends(get_current_user)) -> UUID:
    """Hospital admin or hospital staff — for clinical modules scoped by hospital."""
    if user.get("role") not in {"hospital_admin", "hospital_staff"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hospital access required")
    try:
        return UUID(str(user["hospital_uuid"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid hospital context in token",
        ) from exc


def require_hospital_user(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") not in {"hospital_admin", "hospital_staff"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hospital access required")
    if not user.get("hospital_uuid"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Hospital context missing from token")
    return user
