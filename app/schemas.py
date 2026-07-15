from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.models import PlanType


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    hospital_id: str | None = None
    name: str | None = None
    plan: PlanType | None = None
    staff_role_name: str | None = None
    permissions: list[dict] | None = None
    user_id: str | None = None


class HospitalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    address: str = Field(min_length=1)
    phone: str = Field(min_length=1, max_length=32)
    email: EmailStr
    plan: PlanType = PlanType.basic
    icon_url: str | None = None


class HospitalResponse(BaseModel):
    id: UUID
    hospital_id: str
    name: str
    address: str
    phone: str
    email: EmailStr
    plan: PlanType
    icon_url: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class HospitalCreateResponse(HospitalResponse):
    generated_password: str
