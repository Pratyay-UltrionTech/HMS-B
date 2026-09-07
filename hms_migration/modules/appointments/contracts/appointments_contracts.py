"""
Pydantic contracts for the Appointments module.

Conforms to UltrionTech-Backend-Template modules/appointments/contracts/ specification.
Preserves exact request/response schemas for HMS-frontend compatibility.
"""

from __future__ import annotations

from datetime import date, datetime, time
import re
from typing import Annotated
import uuid

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, model_validator

from hms_migration.modules.appointments.entities.enums import AppointmentStatus

# Phone number normalization matching Indian 10-digit mobile standards
_PHONE_RE = re.compile(r"^\d{10}$")
_NON_DIGIT = re.compile(r"\D+")


def normalize_phone(value: str) -> str:
    digits = _NON_DIGIT.sub("", (value or "").strip())
    if len(digits) > 10:
        digits = digits[-10:]
    if not _PHONE_RE.fullmatch(digits):
        raise ValueError("must be exactly 10 digits")
    return digits


def normalize_optional_phone(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return normalize_phone(stripped)


PhoneNumber = Annotated[str, AfterValidator(normalize_phone)]
OptionalPhoneNumber = Annotated[str | None, AfterValidator(normalize_optional_phone)]


class BookAppointmentRequest(BaseModel):
    """Book with an existing patient_id, or pass new-patient fields to auto-register."""

    patient_id: uuid.UUID | None = None
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

    doctor_id: uuid.UUID
    appointment_date: date
    appointment_time: time
    visit_type: str = Field(default="OPD", min_length=1, max_length=64)
    appointment_type_id: uuid.UUID | None = None
    # Optional — when omitted, resolved from doctor's consultation pricing / shift
    wing_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    purpose: str | None = Field(default=None, max_length=255)
    notes: str | None = None
    booking_kind: str = Field(default="future", pattern="^(walk_in|future)$")
    nurse_id: uuid.UUID | None = None

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
                ("date_of_birth", self.date_of_birth),
                ("age", self.age),
                ("emergency_contact", self.emergency_contact),
            )
            if val is None or (isinstance(val, str) and not val.strip())
        ]
        if missing:
            raise ValueError(
                "Provide patient_id for an existing patient, or first_name, last_name, mobile, "
                "gender, date_of_birth, age, and emergency_contact to auto-register"
            )
        if self.mobile and self.emergency_contact and self.mobile == self.emergency_contact:
            raise ValueError("Emergency contact number must be different from patient mobile number")
        return self


class FeePreviewRequest(BaseModel):
    doctor_id: uuid.UUID
    appointment_type_id: uuid.UUID
    wing_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    patient_id: uuid.UUID | None = None
    appointment_date: date | None = None


class RescheduleRequest(BaseModel):
    appointment_date: date
    appointment_time: time


class AppointmentListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hospital_id: uuid.UUID
    doctor_id: uuid.UUID
    patient_id: uuid.UUID
    appointment_date: date
    appointment_time: time
    purpose: str
    visit_type: str
    appointment_type_id: uuid.UUID | None = None
    wing_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    consultation_fee: float = 0.0
    followup_eligibility: str | None = None
    status: AppointmentStatus
    booking_kind: str = "future"
    notes: str | None = None
    queue_token: int | None = None
    checked_in_at: datetime | None = None
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None
    op_id: str | None = None
    admission_id: uuid.UUID | None = None
    ip_id: str | None = None
    nurse_id: uuid.UUID | None = None
    nurse_name: str | None = None


class AssignNurseRequest(BaseModel):
    nurse_id: uuid.UUID | None = None


class AdmitIpdRequest(BaseModel):
    ward_id: uuid.UUID
    room_id: uuid.UUID
    bed_id: uuid.UUID
    notes: str | None = None


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
    wing_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


class LeaveBlock(BaseModel):
    start: str
    end: str
    reason: str | None = None


class DoctorAvailability(BaseModel):
    doctor_id: uuid.UUID
    doctor_name: str
    date: date
    available: bool
    reason: str | None = None
    booked_slots: list[str] = []
    leave_blocks: list[LeaveBlock] = []
    shift_start: str | None = None
    shift_end: str | None = None


class QueueGroup(BaseModel):
    doctor_id: uuid.UUID
    doctor_name: str
    patients: list[AppointmentListItem]
