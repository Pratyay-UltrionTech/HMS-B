"""Pydantic schemas for Financial Clearance Exceptions and Emergency Overrides."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FinancialExceptionRequest(BaseModel):
    patient_id: UUID
    admission_id: UUID | None = None
    service_type: str = Field(..., description="admission_bed, laboratory, radiology, pharmacy, ot, discharge")
    action_type: str = Field(..., description="bed_allocation, sample_collection, scan_execution, dispense, surgery_start, discharge")
    source_id: UUID | None = None
    required_amount: float = 0.0
    reason_category: str = Field(..., description="e.g. administrative_approval, charity_care, government_scheme, family_guarantee, other")
    reason: str = Field(..., min_length=5, description="Mandatory written justification for financial exception")


class FinancialExceptionDecision(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected)$")
    approval_remarks: str | None = None


class EmergencyOverrideRequest(BaseModel):
    patient_id: UUID
    admission_id: UUID | None = None
    service_type: str
    action_type: str
    source_id: UUID | None = None
    required_amount: float = 0.0
    reason: str = Field(..., min_length=5, description="Mandatory clinical emergency justification")


class FinancialExceptionResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    admission_id: UUID | None = None
    financial_account_id: UUID | None = None
    service_type: str
    action_type: str
    source_id: UUID | None = None
    is_emergency: bool
    required_amount: float
    amount_covered: float
    shortfall_amount: float
    reason_category: str
    reason: str
    status: str
    requested_by_id: UUID
    requested_by_name: str
    requested_at: datetime
    approved_by_id: UUID | None = None
    approved_by_name: str | None = None
    approved_at: datetime | None = None
    approval_remarks: str | None = None
    is_consumed: bool
    consumed_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
