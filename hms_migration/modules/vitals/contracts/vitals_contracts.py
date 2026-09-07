"""
Vitals API Request and Response Contracts.

Preserves exact API field names, types, constraints, and serialization
behavior required by the HMS frontend.
"""

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class VitalItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    result: str = Field(min_length=1, max_length=128)
    # Optional for backward compatibility with older clients; no longer collected in UI
    suitable_range: str = Field(default="", max_length=128)


class VitalBatchCreate(BaseModel):
    appointment_id: UUID
    items: list[VitalItemCreate] = Field(min_length=1)


class VitalItemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    result: str | None = Field(default=None, min_length=1, max_length=128)
    suitable_range: str | None = Field(default=None, max_length=128)


class VitalReadingResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    appointment_id: UUID
    patient_id: UUID
    name: str
    suitable_range: str
    result: str
    recorded_by_name: str
    recorded_at: datetime
    created_at: datetime
    patient_name: str | None = None
    doctor_name: str | None = None

    model_config = {"from_attributes": True}


def serialize_vital_reading(row: Any) -> VitalReadingResponse:
    """Map a VitalReading ORM row to its API response contract."""
    patient = getattr(row, "patient", None)
    appt = getattr(row, "appointment", None)
    doctor_name = None
    if appt is not None and getattr(appt, "doctor", None) is not None:
        doctor_name = appt.doctor.name
    return VitalReadingResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        appointment_id=row.appointment_id,
        patient_id=row.patient_id,
        name=row.name,
        suitable_range=row.suitable_range,
        result=row.result,
        recorded_by_name=row.recorded_by_name,
        recorded_at=row.recorded_at,
        created_at=row.created_at,
        patient_name=patient.name if patient else None,
        doctor_name=doctor_name,
    )


class VitalsTodayItem(BaseModel):
    appointment_id: UUID
    patient_id: UUID
    patient_name: str
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    doctor_id: UUID
    doctor_name: str | None = None
    appointment_date: date
    appointment_time: time
    purpose: str
    status: str
    vitals_count: int = 0
    vitals: list[VitalReadingResponse] = []
