from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models import AppointmentStatus
from app.utils.phone import OptionalPhoneNumber, PhoneNumber


class BookAppointmentRequest(BaseModel):
    """Book with an existing patient_id, or pass new-patient fields to auto-register."""

    patient_id: UUID | None = None
    # Auto-register fields (used when patient_id is omitted)
    first_name: str | None = Field(default=None, min_length=1, max_length=128)
    last_name: str | None = Field(default=None, min_length=1, max_length=128)
    mobile: PhoneNumber | None = None
    gender: str | None = Field(default=None, min_length=1, max_length=32)
    date_of_birth: date | None = None
    age: int | None = Field(default=None, ge=0, le=150)
    email: EmailStr | None = None
    address: str | None = None
    emergency_contact: OptionalPhoneNumber = None
    blood_group: str | None = Field(default=None, max_length=16)

    doctor_id: UUID
    appointment_date: date
    appointment_time: time
    visit_type: str = Field(default="OPD", min_length=1, max_length=64)
    appointment_type_id: UUID | None = None
    wing_id: UUID | None = None
    department_id: UUID | None = None
    purpose: str | None = Field(default=None, max_length=255)
    notes: str | None = None

    @model_validator(mode="after")
    def require_patient_or_new_details(self):
        if self.patient_id is not None:
            return self
        missing = [
            name
            for name, val in (
                ("first_name", self.first_name),
                ("last_name", self.last_name),
                ("mobile", self.mobile),
                ("gender", self.gender),
            )
            if not (val and str(val).strip())
        ]
        if missing:
            raise ValueError(
                "Provide patient_id for an existing patient, or first_name, last_name, mobile, and gender to auto-register"
            )
        return self


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
    appointment_type_id: UUID | None = None
    wing_id: UUID | None = None
    department_id: UUID | None = None
    consultation_fee: float = 0.0
    followup_eligibility: str | None = None
    status: AppointmentStatus
    notes: str | None
    queue_token: int | None
    checked_in_at: datetime | None
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None


class FeePreviewRequest(BaseModel):
    doctor_id: UUID
    appointment_type_id: UUID
    wing_id: UUID
    department_id: UUID
    patient_id: UUID | None = None
    appointment_date: date | None = None


class FeePreviewResponse(BaseModel):
    consultation_fee: float
    base_fee: float
    pricing_found: bool
    followup_free_days: int | None = None
    is_follow_up: bool = False
    followup_eligibility: str | None = None
    followup_message: str | None = None
    last_completed_visit_date: date | None = None
    appointment_type_name: str | None = None
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
