"""
Pydantic contracts for Critical Care domain.

Conforms to UltrionTech-Backend-Template modules/critical_care/contracts/ specification.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.critical_care.entities.critical_care_entities import (
    AlertStatus,
    CodeBlueOutcome,
    CodeBlueStatus,
    DeteriorationRiskLevel,
)


class IcuBoardPatientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    admission_id: UUID
    patient_id: UUID
    patient_name: str
    patient_uhid: str
    patient_age: int | None
    patient_gender: str | None
    ward_name: str
    bed_code: str
    attending_doctor_name: str | None
    length_of_stay_days: int
    ventilator_mode: str | None
    peep: float | None
    fio2_percent: float | None
    invasive_line_days: dict | None
    inotrope_support: bool
    inotrope_details: str | None
    gcs_score: int | None
    latest_heart_rate: float | None
    latest_spo2: float | None
    latest_map: float | None
    latest_hourly_balance_ml: float | None


class IcuProfileUpdate(BaseModel):
    ventilator_mode: str | None = None
    peep: float | None = None
    fio2_percent: float | None = None
    invasive_line_days: dict | None = None
    inotrope_support: bool = False
    inotrope_details: str | None = None
    gcs_score: int | None = Field(default=None, ge=3, le=15)
    care_indicators: dict | None = None


class IcuFlowsheetCreate(BaseModel):
    recorded_at: datetime
    heart_rate: float | None = None
    systolic_bp: float | None = None
    diastolic_bp: float | None = None
    spo2: float | None = None
    respiratory_rate: float | None = None
    temperature: float | None = None
    abg_ph: float | None = None
    abg_pco2: float | None = None
    abg_po2: float | None = None
    abg_hco3: float | None = None
    abg_lactate: float | None = None
    hourly_iv_intake_ml: float = 0.0
    hourly_enteral_intake_ml: float = 0.0
    hourly_urine_output_ml: float = 0.0
    hourly_drain_output_ml: float = 0.0
    remarks: str | None = None


class IcuFlowsheetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    admission_id: UUID
    recorded_at: datetime
    recorded_by_name: str
    heart_rate: float | None
    systolic_bp: float | None
    diastolic_bp: float | None
    mean_arterial_pressure: float | None
    spo2: float | None
    respiratory_rate: float | None
    temperature: float | None
    abg_ph: float | None
    abg_pco2: float | None
    abg_po2: float | None
    abg_hco3: float | None
    abg_lactate: float | None
    hourly_iv_intake_ml: float
    hourly_enteral_intake_ml: float
    hourly_urine_output_ml: float
    hourly_drain_output_ml: float
    hourly_balance_ml: float
    remarks: str | None
    created_at: datetime


class DeteriorationAlertCalculateRequest(BaseModel):
    patient_id: UUID
    admission_id: UUID | None = None
    respiratory_rate: float
    spo2: float
    on_supplemental_oxygen: bool = False
    systolic_bp: float
    heart_rate: float
    avpu: str = "alert"
    temperature: float


class DeteriorationAlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    patient_id: UUID
    admission_id: UUID | None
    calculator_type: str
    total_score: int
    risk_level: DeteriorationRiskLevel
    parameter_breakdown: dict
    alert_status: AlertStatus
    acknowledged_by_name: str | None
    acknowledged_at: datetime | None
    escalation_level: int
    created_at: datetime


class DeteriorationAlertAcknowledgeRequest(BaseModel):
    notes: str | None = None


class CodeBlueIncidentCreate(BaseModel):
    patient_id: UUID | None = None
    location_description: str = Field(min_length=3)
    ward_id: UUID | None = None
    bed_id: UUID | None = None
    team_members: dict | None = None


class CodeBlueEventCreate(BaseModel):
    event_type: str = Field(min_length=2, description="cpr_cycle, shock, drug_administered, rhythm, intubation")
    details: str = Field(min_length=2)


class CodeBlueConcludeRequest(BaseModel):
    outcome: CodeBlueOutcome
    summary_notes: str = Field(min_length=3)


class CodeBlueEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    incident_id: UUID
    event_time: datetime
    event_type: str
    details: str
    recorded_by_name: str
    created_at: datetime


class CodeBlueIncidentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    incident_code: str
    patient_id: UUID | None
    location_description: str
    ward_id: UUID | None
    bed_id: UUID | None
    status: CodeBlueStatus
    activated_at: datetime
    activated_by_name: str
    team_arrived_at: datetime | None
    concluded_at: datetime | None
    outcome: CodeBlueOutcome | None
    team_members: dict | None
    summary_notes: str | None
    events: list[CodeBlueEventResponse] = []
