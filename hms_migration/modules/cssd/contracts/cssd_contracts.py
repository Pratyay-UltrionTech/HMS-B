"""
Pydantic contracts / DTOs for the CSSD (Central Sterile Services Department) domain.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from hms_migration.modules.cssd.entities.cssd_entities import (
    BiologicalIndicatorResult,
    BowieDickResult,
    DiscrepancyInvestigationStatus,
    DiscrepancyType,
    InstrumentSetIssueStatus,
    QcResult,
    ReturnCondition,
    SterilizationBatchStatus,
    SterilizationMethod,
)

# ── Feature 39: Instrument Set Catalogue ────────────────────────────────────


class InstrumentSetItemCreate(BaseModel):
    instrument_name: str = Field(min_length=1, max_length=255)
    quantity: int = Field(ge=1, default=1)


class InstrumentSetItemResponse(BaseModel):
    id: UUID
    instrument_name: str
    quantity: int

    model_config = {"from_attributes": True}


class InstrumentSetCreate(BaseModel):
    set_code: str | None = Field(default=None, max_length=32)
    set_name: str = Field(min_length=1, max_length=255)
    owning_department: str | None = None
    description: str | None = None
    is_active: bool = True
    items: list[InstrumentSetItemCreate] = Field(default_factory=list)


class InstrumentSetUpdate(BaseModel):
    set_name: str | None = Field(default=None, min_length=1, max_length=255)
    owning_department: str | None = None
    description: str | None = None
    is_active: bool | None = None


class InstrumentSetResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    set_code: str
    set_name: str
    owning_department: str | None
    instrument_count: int
    description: str | None
    is_active: bool
    created_at: datetime
    items: list[InstrumentSetItemResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


# ── Feature 40: Sterilization Batch ──────────────────────────────────────────


class SterilizationBatchCreate(BaseModel):
    batch_number: str | None = Field(default=None, max_length=32)
    sterilization_method: SterilizationMethod
    machine_id: str | None = None
    operator_staff_id: str | None = None
    temperature_c: float | None = None
    pressure_kpa: float | None = None
    instrument_set_ids: list[UUID] = Field(default_factory=list)


class SterilizationBatchComplete(BaseModel):
    temperature_c: float | None = None
    pressure_kpa: float | None = None


class SterilizationBatchFail(BaseModel):
    remarks: str | None = None


class SterilizationBatchItemResponse(BaseModel):
    id: UUID
    instrument_set_id: UUID
    instrument_set_name: str | None = None

    model_config = {"from_attributes": True}


class SterilizationBatchResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    batch_number: str
    sterilization_method: SterilizationMethod
    machine_id: str | None
    operator_staff_id: str | None
    cycle_start_time: datetime
    cycle_end_time: datetime | None
    temperature_c: float | None
    pressure_kpa: float | None
    status: SterilizationBatchStatus
    created_at: datetime
    items: list[SterilizationBatchItemResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


# ── Feature 41: Sterilization QC ─────────────────────────────────────────────


class SterilizationQCCreate(BaseModel):
    chemical_indicator_result: QcResult
    biological_indicator_result: BiologicalIndicatorResult = BiologicalIndicatorResult.pending
    bowie_dick_test_result: BowieDickResult | None = None
    checked_by_staff_id: str | None = None
    remarks: str | None = None


class SterilizationQCResponse(BaseModel):
    id: UUID
    batch_id: UUID
    chemical_indicator_result: QcResult
    biological_indicator_result: BiologicalIndicatorResult
    bowie_dick_test_result: BowieDickResult | None
    overall_result: QcResult
    checked_by_staff_id: str | None
    remarks: str | None
    checked_at: datetime

    model_config = {"from_attributes": True}


# ── Feature 42: Issue & Return Tracking ──────────────────────────────────────


class InstrumentSetIssueCreate(BaseModel):
    instrument_set_id: UUID
    batch_id: UUID
    issued_to_department: str = Field(min_length=1, max_length=128)
    issued_to_staff_id: str | None = None
    expected_return_time: datetime | None = None


class InstrumentSetReturn(BaseModel):
    returned_by_staff_id: str | None = None
    return_condition: ReturnCondition = ReturnCondition.intact
    discrepancy_instrument_name: str | None = None
    discrepancy_quantity_affected: int = Field(ge=1, default=1)
    discrepancy_remarks: str | None = None


class InstrumentSetIssueResponse(BaseModel):
    id: UUID
    instrument_set_id: UUID
    instrument_set_name: str | None = None
    batch_id: UUID
    batch_number: str | None = None
    issued_to_department: str
    issued_to_staff_id: str | None
    issue_time: datetime
    expected_return_time: datetime | None
    status: InstrumentSetIssueStatus
    return_time: datetime | None
    returned_by_staff_id: str | None
    return_condition: ReturnCondition | None

    model_config = {"from_attributes": True}


# ── Feature 43: Missing/Damaged Instrument Tracking ──────────────────────────


class DiscrepancyReportCreate(BaseModel):
    issue_id: UUID
    discrepancy_type: DiscrepancyType
    instrument_name: str = Field(min_length=1, max_length=255)
    quantity_affected: int = Field(ge=1, default=1)
    reported_by_staff_id: str | None = None


class DiscrepancyReportTransition(BaseModel):
    investigation_status: DiscrepancyInvestigationStatus
    resolution_notes: str | None = None


class DiscrepancyReportResponse(BaseModel):
    id: UUID
    issue_id: UUID
    discrepancy_type: DiscrepancyType
    instrument_name: str
    quantity_affected: int
    reported_by_staff_id: str | None
    reported_at: datetime
    investigation_status: DiscrepancyInvestigationStatus
    resolution_notes: str | None
    resolved_at: datetime | None

    model_config = {"from_attributes": True}
