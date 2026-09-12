"""
Target-native Pydantic contracts for Masters Insurance & TPA domain.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class InsuranceProviderCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    category: str = Field(default="Private TPA", max_length=64)
    irdai_reg_no: str | None = Field(default=None, max_length=64)
    pre_auth_sla_hours: int = Field(default=2, ge=1)
    tariff_discount_percent: float = Field(default=10.0, ge=0.0, le=100.0)
    tariff_notes: str | None = Field(default=None, max_length=255)
    contact_person: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    status: str = Field(default="active", max_length=32)
    mou_valid_till: date | None = None
    is_active: bool = True


class InsuranceProviderUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = None
    irdai_reg_no: str | None = None
    pre_auth_sla_hours: int | None = None
    tariff_discount_percent: float | None = None
    tariff_notes: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    status: str | None = None
    mou_valid_till: date | None = None
    is_active: bool | None = None


class InsuranceProviderResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    code: str
    name: str
    category: str
    irdai_reg_no: str | None = None
    pre_auth_sla_hours: int
    tariff_discount_percent: float
    tariff_notes: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    status: str
    mou_valid_till: date | None = None
    active_claims_count: int = 0
    active_receivables_amount: float = 0.0
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
