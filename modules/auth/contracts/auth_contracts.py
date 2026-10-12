"""Authentication contracts."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr

from modules.tenancy.entities.hospital import PlanType


class LoginRequest(BaseModel):
    """Login credentials payload with optional tenant qualification."""

    email: EmailStr
    password: str
    hospital_id: str | None = None


class MatchingHospitalSummary(BaseModel):
    """Summary of a hospital for disambiguation during login."""

    hospital_id: str
    name: str


class SetupPasswordRequest(BaseModel):
    """Payload to set password using a setup or invite token."""

    token: str
    password: str


class ForgotPasswordRequest(BaseModel):
    """Payload to initiate password reset."""

    email: EmailStr
    hospital_id: str | None = None


class ResetPasswordRequest(BaseModel):
    """Payload to reset password using a reset token."""

    token: str
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
    roles: list[str] | None = None
    primary_role: str | None = None
    department_id: str | None = None
    department_name: str | None = None
    department_ids: list[str] | None = None
    ward_ids: list[str] | None = None
    permissions: list[dict] | None = None
    user_id: str | None = None
    token_version: int = 1


class MeResponse(BaseModel):
    """Server-authoritative session state returned by GET /api/auth/me."""

    sub: str
    role: str
    name: str | None = None
    hospital_id: str | None = None
    hospital_uuid: str | None = None
    staff_role_name: str | None = None
    roles: list[str] | None = None
    primary_role: str | None = None
    department_id: str | None = None
    department_name: str | None = None
    department_ids: list[str] | None = None
    ward_ids: list[str] | None = None
    user_id: str | None = None
    is_active: bool = True
    token_version: int = 1
    permissions: list[dict] | None = None
