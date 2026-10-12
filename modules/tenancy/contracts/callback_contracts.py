"""Callback request schemas and contracts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from shared.validators.phone import PhoneNumber


class CallbackRequestCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    phone: PhoneNumber
    email: EmailStr | None = None
    location: str | None = Field(default=None, max_length=255)
    comments: str | None = Field(default=None, max_length=2000)


class CallbackRequestResponse(BaseModel):
    id: UUID
    name: str
    phone: str
    email: EmailStr | None = None
    location: str | None = None
    comments: str | None = None
    status: str = "pending"
    notes: str | None = None
    contacted_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CallbackRequestUpdate(BaseModel):
    status: str = Field(pattern="^(pending|contacted|closed)$")
    notes: str | None = Field(default=None, max_length=2000)

