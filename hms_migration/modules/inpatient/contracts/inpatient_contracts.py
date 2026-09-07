"""
Pydantic contracts for Inpatient Admissions and IPD Form Submissions.

Conforms to UltrionTech-Backend-Template modules/inpatient/contracts/ specification.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.inpatient.entities.admission import (
    AdmissionStatus,
    IpdFormSubmissionStatus,
)


class AdmitRequest(BaseModel):
    patient_id: UUID
    ward_id: UUID
    room_id: UUID
    bed_id: UUID
    doctor_id: UUID | None = None
    admission_date: date | None = None
    notes: str | None = None


class AllocateRequest(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    ward_id: UUID
    room_id: UUID
    bed_id: UUID


class TransferRequest(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    to_ward_id: UUID
    to_room_id: UUID
    to_bed_id: UUID


class DischargeRequestCreate(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    discharge_notes: str | None = Field(default=None, max_length=2000)


class DischargeRequest(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    discharge_date: date | None = None
    discharge_time: time | None = None
    discharge_notes: str | None = Field(default=None, max_length=2000)


class AdmissionDetail(BaseModel):
    id: UUID
    patient_id: UUID
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    ward_id: UUID
    room_id: UUID
    bed_id: UUID
    ward_name: str | None = None
    room_code: str | None = None
    bed_code: str | None = None
    doctor_id: UUID | None = None
    doctor_name: str | None = None
    status: AdmissionStatus
    notes: str | None = None
    discharge_notes: str | None = None
    admitted_at: datetime
    discharged_at: datetime | None = None
    admission_fee: float = 0.0
    bed_charge_per_day: float = 0.0
    ip_id: str | None = None
    source_appointment_id: UUID | None = None

    model_config = ConfigDict(from_attributes=True)


class DischargeQueueItem(AdmissionDetail):
    total_charges: float = 0.0
    total_paid: float = 0.0
    outstanding: float = 0.0
    can_discharge: bool = False


class AdmitPatientRequest(BaseModel):
    ward_id: UUID
    room_id: UUID
    bed_id: UUID
    doctor_id: UUID | None = None
    notes: str | None = None


class AdmissionSummary(BaseModel):
    id: UUID
    ward_id: UUID
    room_id: UUID
    bed_id: UUID
    ward_name: str | None = None
    room_code: str | None = None
    bed_code: str | None = None
    doctor_name: str | None = None
    status: AdmissionStatus
    admitted_at: datetime
    discharged_at: datetime | None = None
    notes: str | None = None
    ip_id: str | None = None

    model_config = ConfigDict(from_attributes=True)


class DischargeResponse(BaseModel):
    id: UUID
    status: AdmissionStatus
    discharged_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class IpdFormSubmissionCreate(BaseModel):
    patient_id: UUID
    admission_id: UUID | None = None
    form_id: str = Field(min_length=1, max_length=128)
    form_title: str = Field(min_length=1, max_length=255)
    form_data: dict[str, Any] = Field(default_factory=dict)
    html_snapshot: str | None = None
    status: IpdFormSubmissionStatus = IpdFormSubmissionStatus.draft


class IpdFormSubmissionUpdate(BaseModel):
    admission_id: UUID | None = None
    form_data: dict[str, Any] | None = None
    html_snapshot: str | None = None
    status: IpdFormSubmissionStatus | None = None
    form_title: str | None = Field(default=None, min_length=1, max_length=255)


class IpdFormSubmissionResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    admission_id: UUID | None = None
    form_id: str
    form_title: str
    form_data: dict[str, Any]
    status: IpdFormSubmissionStatus
    filled_by_id: UUID | None = None
    filled_by_name: str = ""
    filled_by_role: str = ""
    medical_record_id: UUID | None = None
    patient_document_id: UUID | None = None
    has_html_snapshot: bool = False
    created_at: datetime
    updated_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None

    model_config = ConfigDict(from_attributes=True)
