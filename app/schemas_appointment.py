from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import AppointmentStatus


class BookAppointmentRequest(BaseModel):
    patient_id: UUID
    doctor_id: UUID
    appointment_date: date
    appointment_time: time
    visit_type: str = Field(default="OPD", min_length=1, max_length=64)
    purpose: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class RescheduleRequest(BaseModel):
    appointment_date: date
    appointment_time: time


class AppointmentListItem(BaseModel):
    id: UUID
    hospital_id: UUID
    doctor_id: UUID
    patient_id: UUID
    appointment_date: date
    appointment_time: time
    purpose: str
    visit_type: str
    status: AppointmentStatus
    notes: str | None
    queue_token: int | None
    checked_in_at: datetime | None
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None


class LeaveBlock(BaseModel):
    start: str
    end: str
    reason: str | None = None


class DoctorAvailability(BaseModel):
    doctor_id: UUID
    doctor_name: str
    date: date
    available: bool
    reason: str | None = None
    booked_slots: list[str] = []
    leave_blocks: list[LeaveBlock] = []
    shift_start: str | None = None
    shift_end: str | None = None


class QueueGroup(BaseModel):
    doctor_id: UUID
    doctor_name: str
    patients: list[AppointmentListItem]
