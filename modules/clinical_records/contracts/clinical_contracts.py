"""
Pydantic contracts for Clinical Records and Prescriptions.

Conforms to UltrionTech-Backend-Template modules/clinical_records/contracts/ specification.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modules.appointments.entities.enums import AppointmentStatus


class PrescriptionCreate(BaseModel):
    patient_id: UUID
    appointment_id: UUID | None = None
    symptoms: str = Field(default="")
    diagnosis: str = Field(default="")
    medicines: str = Field(default="")
    dosage: str = Field(default="")
    advice: str | None = None
    follow_up_date: date | None = None
    signature_data: str | None = None
    status: str = "issued"
    test_ids: list[UUID] = Field(default_factory=list)
    panel_ids: list[UUID] = Field(default_factory=list)
    scan_ids: list[UUID] = Field(default_factory=list)
    override_confirmed: bool = False
    override_reason: str | None = None


class PrescriptionUpdate(BaseModel):
    appointment_id: UUID | None = None
    symptoms: str | None = None
    diagnosis: str | None = None
    medicines: str | None = None
    dosage: str | None = None
    advice: str | None = None
    follow_up_date: date | None = None
    signature_data: str | None = None
    status: str | None = None
    test_ids: list[UUID] | None = None
    panel_ids: list[UUID] | None = None
    scan_ids: list[UUID] | None = None
    override_confirmed: bool = False
    override_reason: str | None = None


class PrescriptionCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_must_be_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason is required")
        return stripped


class PrescriptionResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    doctor_id: UUID
    patient_id: UUID
    appointment_id: UUID | None = None
    symptoms: str
    diagnosis: str
    medicines: str
    dosage: str
    advice: str | None = None
    follow_up_date: date | None = None
    signature_data: str | None = None
    has_signature: bool = False
    status: str = "issued"
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    cancel_reason: str | None = None
    created_at: datetime
    patient_name: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None
    appointment_status: AppointmentStatus | None = None
    test_ids: list[UUID] = []
    panel_ids: list[UUID] = []
    scan_ids: list[UUID] = []

    model_config = ConfigDict(from_attributes=True)


class MedicalRecordCreate(BaseModel):
    patient_id: UUID
    appointment_id: UUID | None = None
    report_type: str = Field(min_length=1, max_length=64)
    provenance: str = Field(default="external", max_length=32)
    title: str = Field(min_length=1, max_length=255)
    notes: str | None = None
    file_name: str | None = None
    file_data: str | None = None


class MedicalRecordResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    doctor_id: UUID
    patient_id: UUID
    appointment_id: UUID | None = None
    lab_order_id: UUID | None = None
    radiology_order_id: UUID | None = None
    report_type: str
    provenance: str = "internal"
    title: str
    notes: str | None = None
    file_name: str | None = None
    has_file: bool = False
    created_at: datetime
    patient_name: str | None = None
    doctor_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class FileDownloadPayload(BaseModel):
    file_name: str | None
    file_data: str
