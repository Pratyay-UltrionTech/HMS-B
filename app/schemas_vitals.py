from datetime import date, datetime, time
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
