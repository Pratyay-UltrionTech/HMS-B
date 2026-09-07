"""
JWT token encoding and decoding utilities.

Conforms to UltrionTech-Backend-Template shared/auth/ specification.
Preserves exact OG HMS-B JWT signing, claims, expiration, and error handling.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from hms_migration.config.settings import Settings, get_settings
from hms_migration.shared.exceptions.base import UnauthorizedError


def create_access_token(
    data: dict[str, Any],
    settings: Settings | None = None,
) -> str:
    """Generate a signed JWT token with expiry."""
    cfg = settings or get_settings()
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=cfg.jwt_expire_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


def decode_access_token(
    token: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Decode and validate a JWT access token."""
    cfg = settings or get_settings()
    try:
        return jwt.decode(token, cfg.jwt_secret, algorithms=[cfg.jwt_algorithm])
    except JWTError as exc:
        raise UnauthorizedError(message="Invalid or expired token") from exc
