"""Authentication contracts."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr

from hms_migration.modules.tenancy.entities.hospital import PlanType


class LoginRequest(BaseModel):
    """Login credentials payload."""

    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    """Authenticated user response with JWT access token and context claims."""

    access_token: str
    token_type: str = "bearer"
    role: str
    hospital_id: str | None = None
    name: str | None = None
    plan: PlanType | None = None
    staff_role_name: str | None = None
    permissions: list[dict] | None = None
    user_id: str | None = None
