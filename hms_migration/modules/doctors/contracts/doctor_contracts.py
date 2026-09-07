"""
Pydantic contracts for the Doctors domain.

Conforms to UltrionTech-Backend-Template modules/doctors/contracts/ specification.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.inpatient.entities.admission import IpdFormSubmissionStatus
from hms_migration.modules.patients.contracts.patients_contracts import PhoneNumber


class DoctorSummary(BaseModel):
    id: UUID
    name: str
    email: str
    phone: str
    role_name: str | None = None
    specialization: str | None = None
    medical_registration_number: str | None = None
    qualification: str | None = None
    years_of_experience: int | None = None
    consultation_room: str | None = None
    show_financial_details: bool = True
    department_id: UUID | None = None
    department_name: str | None = None
    custom_values: dict[str, Any] = Field(default_factory=dict)
    is_active: bool
    patient_count: int = 0
    today_appointment_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class StaffDoctorOption(BaseModel):
    id: str
    name: str
    phone: str
    email: str
    specialization: str | None = None
    qualification: str | None = None
    medical_registration_number: str | None = None
    years_of_experience: int | None = None
    consultation_room: str | None = None


class DoctorPatientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    mobile: PhoneNumber
    age: int | None = Field(default=None, ge=0, le=150)
    gender: str | None = Field(default=None, max_length=32)
    address: str | None = None


class DoctorPatientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    mobile: PhoneNumber | None = None
    age: int | None = Field(default=None, ge=0, le=150)
    gender: str | None = Field(default=None, max_length=32)
    address: str | None = None


class DoctorPatientResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    name: str
    mobile: str
    age: int | None
    gender: str | None
    address: str | None
    created_at: datetime
    last_visit: date | None = None
    last_diagnosis: str | None = None
    uhid: str | None = None
    emergency_contact: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_relation: str | None = None
    has_insurance: bool = False
    insurance_provider: str | None = None

    model_config = ConfigDict(from_attributes=True)


class DoctorAppointmentCreate(BaseModel):
    patient_id: UUID
    appointment_date: date
    appointment_time: time
    purpose: str = Field(min_length=1, max_length=255)
    notes: str | None = None
    status: AppointmentStatus = AppointmentStatus.scheduled
    nurse_id: UUID | None = None


class DoctorAppointmentUpdate(BaseModel):
    appointment_date: date | None = None
    appointment_time: time | None = None
    purpose: str | None = Field(default=None, min_length=1, max_length=255)
    notes: str | None = None
    status: AppointmentStatus | None = None
    nurse_id: UUID | None = None


class DoctorAppointmentResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    doctor_id: UUID
    patient_id: UUID
    appointment_date: date
    appointment_time: time
    purpose: str
    status: AppointmentStatus
    notes: str | None
    created_at: datetime
    patient_name: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None
    patient_uhid: str | None = None
    op_id: str | None = None
    admission_id: UUID | None = None
    ip_id: str | None = None
    admission_ward: str | None = None
    admission_bed: str | None = None
    admission_status: str | None = None
    nurse_id: UUID | None = None
    nurse_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class TransferToInpatientRequest(BaseModel):
    notes: str | None = None


class HospitalClinicProfile(BaseModel):
    id: UUID
    hospital_id: str
    name: str
    address: str
    phone: str
    email: str
    slogan: str | None = None
    website: str | None = None


class DoctorScheduleContext(BaseModel):
    doctor_id: UUID
    shift_name: str | None = None
    shift_start: str
    shift_end: str
    slot_duration_minutes: int = 15
    uses_default_shift: bool = False


LEAVE_TYPES = (
    "Personal",
    "Conference",
    "Training",
    "Vacation",
    "Emergency",
    "Medical",
    "Other",
)


class DoctorLeaveCreate(BaseModel):
    leave_date: date
    start_time: time | None = None
    end_time: time | None = None
    leave_type: str | None = Field(default="Personal", max_length=32)
    reason: str | None = Field(default=None, max_length=500)
    full_day: bool = False


class DoctorLeaveRangeCreate(BaseModel):
    start_date: date
    end_date: date
    leave_type: str = Field(default="Personal", max_length=32)
    full_day: bool = True
    start_time: time | None = None
    end_time: time | None = None
    reason: str | None = Field(default=None, max_length=500)


class LeaveConflictDay(BaseModel):
    date: date
    times: list[str]


class LeaveConflictDetail(BaseModel):
    code: str = "appointment_conflict"
    message: str
    conflicts: list[LeaveConflictDay]


class DoctorLeaveResponse(BaseModel):
    id: UUID
    doctor_id: UUID
    leave_date: date
    start_time: time
    end_time: time
    leave_type: str | None = None
    reason: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class IpdFormHistoryItem(BaseModel):
    id: UUID
    admission_id: UUID | None = None
    form_id: str
    form_title: str
    status: IpdFormSubmissionStatus
    has_html_snapshot: bool = False
    filled_by_name: str = ""
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PatientHistoryResponse(BaseModel):
    patient: DoctorPatientResponse
    appointments: list[DoctorAppointmentResponse] = Field(default_factory=list)
    prescriptions: list[Any] = Field(default_factory=list)
    medical_records: list[Any] = Field(default_factory=list)
    lab_orders: list[Any] = Field(default_factory=list)
    radiology_orders: list[Any] = Field(default_factory=list)
    ot_surgeries: list[Any] = Field(default_factory=list)
    vitals: list[Any] = Field(default_factory=list)
    ipd_forms: list[IpdFormHistoryItem] = Field(default_factory=list)
    financial_summary: dict[str, Any] | None = None
