"""
Pydantic contracts for Nursing Care Plans, Merged Clinical Notes, and Shift Handovers.

Conforms to UltrionTech-Backend-Template modules/inpatient/contracts/ specification.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.inpatient.entities.nursing_entities import (
    CarePlanStatus,
    ClinicalNoteType,
    HandoverShiftType,
    MedicationAdminStatus,
    ShiftType,
)


class NursingCarePlanCreate(BaseModel):
    nursing_diagnosis: str = Field(min_length=3)
    diagnosis_code: str | None = None
    goals: str = Field(min_length=3)
    interventions: dict | None = None
    evaluation_frequency: str = "every_shift"


class NursingCarePlanReassess(BaseModel):
    status: CarePlanStatus
    reassessment_notes: str = Field(min_length=3)


class NursingCarePlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    admission_id: UUID
    patient_id: UUID
    diagnosis_code: str | None
    nursing_diagnosis: str
    goals: str
    interventions: dict | None
    evaluation_frequency: str
    status: CarePlanStatus
    created_by_nurse_name: str
    reassessment_notes: str | None
    reassessed_at: datetime | None
    created_at: datetime


class IpdClinicalNoteCreate(BaseModel):
    note_type: ClinicalNoteType = ClinicalNoteType.nursing_progress
    shift_type: ShiftType | None = None
    subjective: str | None = None
    objective_vitals: dict | None = None
    assessment: str | None = None
    plan: str | None = None
    note_content: str = Field(min_length=3)
    signature_data: str | None = None


class IpdClinicalNoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    admission_id: UUID
    patient_id: UUID
    author_id: UUID | None
    author_name: str
    author_role: str
    note_type: ClinicalNoteType
    shift_type: ShiftType | None
    subjective: str | None
    objective_vitals: dict | None
    assessment: str | None
    plan: str | None
    note_content: str
    signature_data: str | None
    created_at: datetime
    updated_at: datetime


class NursingShiftHandoverCreate(BaseModel):
    shift_date: date = Field(default_factory=date.today)
    shift_type: HandoverShiftType
    situation: str = Field(min_length=3)
    background: str = Field(min_length=3)
    assessment_acuity: str = Field(min_length=2)
    pending_stat_orders: dict | None = None
    critical_lab_alerts: dict | None = None
    high_risk_precautions: dict | None = None
    pending_tasks: dict | None = None


class NursingShiftHandoverAcknowledge(BaseModel):
    remarks: str | None = None


class NursingShiftHandoverResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    admission_id: UUID
    patient_id: UUID
    shift_date: date
    shift_type: HandoverShiftType
    outgoing_nurse_id: UUID | None
    outgoing_nurse_name: str
    incoming_nurse_id: UUID | None
    incoming_nurse_name: str | None
    situation: str
    background: str
    assessment_acuity: str
    pending_stat_orders: dict | None
    critical_lab_alerts: dict | None
    high_risk_precautions: dict | None
    pending_tasks: dict | None
    is_acknowledged: bool
    acknowledged_at: datetime | None
    created_at: datetime


class MedicationAdminScheduleCreate(BaseModel):
    medicine_id: UUID | None = None
    medicine_name: str = Field(min_length=2)
    dose: str = Field(min_length=1)
    route: str = Field(min_length=1)
    scheduled_time: datetime
    is_high_alert: bool = False


class MedicationAdminRecordExecution(BaseModel):
    status: MedicationAdminStatus
    vitals_before_admin: dict | None = None
    witness_nurse_id: UUID | None = None
    witness_nurse_name: str | None = None
    notes_or_reason: str | None = None


class MedicationAdminResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    admission_id: UUID
    patient_id: UUID
    medicine_id: UUID | None
    medicine_name: str
    dose: str
    route: str
    scheduled_time: datetime
    status: MedicationAdminStatus
    administered_at: datetime | None
    administering_nurse_id: UUID | None
    administering_nurse_name: str | None
    is_high_alert: bool
    witness_nurse_id: UUID | None
    witness_nurse_name: str | None
    vitals_before_admin: dict | None
    notes_or_reason: str | None
    created_at: datetime
