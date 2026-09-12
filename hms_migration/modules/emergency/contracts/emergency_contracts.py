"""
Pydantic contracts for Emergency Department Management.

Separates request/response validation schemas from SQLAlchemy entities.
Conforms to UltrionTech-Backend-Template modules/emergency/contracts/ specification.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.emergency.entities.emergency_entities import (
    EmergencyArrivalSource,
    EmergencyDispositionType,
    EmergencyOrderStatus,
    EmergencyOrderType,
    EmergencyStatus,
)


class EmergencyEncounterCreate(BaseModel):
    patient_id: UUID
    arrival_source: EmergencyArrivalSource = EmergencyArrivalSource.walk_in
    presenting_complaints: str = Field(min_length=2)
    attending_doctor_id: UUID | None = None


class EmergencyEncounterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    patient_id: UUID
    er_id: str
    arrival_time: datetime
    arrival_source: EmergencyArrivalSource
    presenting_complaints: str
    triage_level: int | None
    attending_doctor_id: UUID | None
    status: EmergencyStatus
    is_escalated: bool
    escalation_reason: str | None
    created_at: datetime
    updated_at: datetime


class EmergencyTriageCreate(BaseModel):
    acuity_level: int = Field(ge=1, le=5, description="ESI/MTS Acuity: 1 (Resuscitation) to 5 (Non-urgent)")
    avpu: str = Field(default="alert", pattern="^(alert|verbal|pain|unresponsive)$")
    pain_score: int | None = Field(default=None, ge=0, le=10)
    respiratory_distress: bool = False
    systolic_bp: float | None = None
    diastolic_bp: float | None = None
    heart_rate: float | None = None
    respiratory_rate: float | None = None
    temperature: float | None = None
    spo2: float | None = None
    red_flags: dict | None = None
    triage_notes: str | None = None


class EmergencyTriageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    encounter_id: UUID
    acuity_level: int
    avpu: str
    pain_score: int | None
    respiratory_distress: bool
    systolic_bp: float | None
    diastolic_bp: float | None
    heart_rate: float | None
    respiratory_rate: float | None
    temperature: float | None
    spo2: float | None
    red_flags: dict | None
    triage_notes: str | None
    triaged_by_id: UUID | None
    triaged_by_name: str
    triaged_at: datetime


class EmergencyQueueItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    encounter_id: UUID
    er_id: str
    patient_id: UUID
    patient_name: str
    patient_uhid: str
    patient_age: int | None
    patient_gender: str | None
    arrival_time: datetime
    arrival_source: EmergencyArrivalSource
    triage_level: int | None
    status: EmergencyStatus
    wait_time_minutes: int
    is_escalated: bool
    escalation_reason: str | None
    attending_doctor_name: str | None


class EmergencyEscalateRequest(BaseModel):
    escalation_reason: str = Field(min_length=3)


class EmergencyOrderCreate(BaseModel):
    order_type: EmergencyOrderType = EmergencyOrderType.medication
    description: str = Field(min_length=2)
    dosage: str | None = None
    route: str | None = None
    is_stat: bool = True
    is_verbal: bool = False
    clinical_notes: str | None = None


class EmergencyOrderExecute(BaseModel):
    clinical_notes: str | None = None


class EmergencyOrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    encounter_id: UUID
    order_type: EmergencyOrderType
    description: str
    dosage: str | None
    route: str | None
    is_stat: bool
    is_verbal: bool
    ordered_by_doctor_id: UUID | None
    ordered_by_name: str
    executed_by_nurse_id: UUID | None
    executed_by_name: str | None
    execution_status: EmergencyOrderStatus
    executed_at: datetime | None
    clinical_notes: str | None
    created_at: datetime


class EmergencyDispositionCreate(BaseModel):
    disposition_type: EmergencyDispositionType
    destination_ward_id: UUID | None = None
    destination_bed_id: UUID | None = None
    transfer_facility: str | None = None
    disposition_notes: str | None = None


class EmergencyDispositionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    encounter_id: UUID
    disposition_type: EmergencyDispositionType
    destination_ward_id: UUID | None
    destination_bed_id: UUID | None
    admission_id: UUID | None
    transfer_facility: str | None
    disposition_notes: str | None
    decided_by_id: UUID | None
    decided_by_name: str
    decided_at: datetime
