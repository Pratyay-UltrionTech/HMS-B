"""
Pydantic contracts for Clinical Records and Prescriptions.

Conforms to UltrionTech-Backend-Template modules/clinical_records/contracts/ specification.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.appointments.entities.enums import AppointmentStatus


class PrescriptionCreate(BaseModel):
    patient_id: UUID
    appointment_id: UUID | None = None
    symptoms: str = Field(min_length=1)
    diagnosis: str = Field(min_length=1)
    medicines: str = Field(min_length=1)
    dosage: str = Field(min_length=1)
    advice: str | None = None
    follow_up_date: date | None = None
    signature_data: str | None = None
    test_ids: list[UUID] = Field(default_factory=list)
    panel_ids: list[UUID] = Field(default_factory=list)
    scan_ids: list[UUID] = Field(default_factory=list)


class PrescriptionUpdate(BaseModel):
    appointment_id: UUID | None = None
    symptoms: str | None = Field(default=None, min_length=1)
    diagnosis: str | None = Field(default=None, min_length=1)
    medicines: str | None = Field(default=None, min_length=1)
    dosage: str | None = Field(default=None, min_length=1)
    advice: str | None = None
    follow_up_date: date | None = None
    signature_data: str | None = None


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
    created_at: datetime
    patient_name: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None
    appointment_status: AppointmentStatus | None = None

    model_config = ConfigDict(from_attributes=True)


class MedicalRecordCreate(BaseModel):
    patient_id: UUID
    appointment_id: UUID | None = None
    report_type: str = Field(min_length=1, max_length=64)
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
