"""Registration request contracts and schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from modules.tenancy.entities.hospital import PlanType
from modules.tenancy.entities.hospital_registration_request import RegistrationStatus
from shared.validators.phone import PhoneNumber


class HospitalRegistrationCreate(BaseModel):
    hospital_name: str = Field(min_length=1, max_length=255)
    contact_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: PhoneNumber
    address: str = Field(min_length=1)
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    estimated_beds: int | None = None
    comments: str | None = None


class HospitalRegistrationResponse(BaseModel):
    id: UUID
    request_number: str
    hospital_name: str
    status: RegistrationStatus
    submitted_at: datetime
    message: str = "Your hospital registration request has been submitted. Our administration team will review it."

    model_config = {"from_attributes": True}


class HospitalRegistrationDetail(BaseModel):
    id: UUID
    request_number: str
    hospital_name: str
    contact_name: str
    email: EmailStr
    phone: str
    address: str
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    estimated_beds: int | None = None
    comments: str | None = None
    status: RegistrationStatus
    submitted_at: datetime
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    rejection_reason: str | None = None
    approved_hospital_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RegistrationApprovePayload(BaseModel):
    plan: PlanType = PlanType.basic


class RegistrationApproveResponse(BaseModel):
    hospital_id: str
    hospital_uuid: UUID
    hospital_name: str
    admin_email: EmailStr
    setup_token: str
    setup_url: str
    message: str


class RegistrationRejectPayload(BaseModel):
    rejection_reason: str = Field(min_length=3, max_length=1000)


class RegistrationPaginatedResponse(BaseModel):
    items: list[HospitalRegistrationDetail]
    total: int
    page: int
    page_size: int
    total_pages: int
