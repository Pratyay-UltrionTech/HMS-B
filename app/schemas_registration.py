from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.models import AdmissionStatus, PatientStatus


BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "Unknown"]


class PatientRegister(BaseModel):
    first_name: str = Field(min_length=1, max_length=128)
    last_name: str = Field(min_length=1, max_length=128)
    gender: str = Field(min_length=1, max_length=32)
    date_of_birth: date | None = None
    age: int | None = Field(default=None, ge=0, le=150)
    mobile: str = Field(min_length=1, max_length=32)
    email: EmailStr | None = None
    address: str | None = None
    emergency_contact: str | None = Field(default=None, max_length=64)
    blood_group: str | None = Field(default=None, max_length=16)


class PatientRegisterUpdate(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=128)
    last_name: str | None = Field(default=None, min_length=1, max_length=128)
    gender: str | None = Field(default=None, min_length=1, max_length=32)
    date_of_birth: date | None = None
    age: int | None = Field(default=None, ge=0, le=150)
    mobile: str | None = Field(default=None, min_length=1, max_length=32)
    email: EmailStr | None = None
    address: str | None = None
    emergency_contact: str | None = Field(default=None, max_length=64)
    blood_group: str | None = Field(default=None, max_length=16)
    status: PatientStatus | None = None


class PatientDirectoryItem(BaseModel):
    id: UUID
    uhid: str
    name: str
    first_name: str
    last_name: str
    mobile: str
    email: str | None
    gender: str | None
    age: int | None
    date_of_birth: date | None
    blood_group: str | None
    status: PatientStatus
    last_visit: date | None = None
    created_at: datetime


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


class VisitSummary(BaseModel):
    id: UUID
    appointment_date: date
    appointment_time: str
    doctor_name: str | None = None
    purpose: str
    visit_type: str
    status: str


class PrescriptionSummary(BaseModel):
    id: UUID
    diagnosis: str
    medicines: str
    doctor_name: str | None = None
    created_at: datetime


class ReportSummary(BaseModel):
    id: UUID
    report_type: str
    title: str
    notes: str | None
    created_at: datetime
    doctor_name: str | None = None


class PatientProfile(BaseModel):
    id: UUID
    uhid: str
    first_name: str
    last_name: str
    name: str
    mobile: str
    email: str | None
    gender: str | None
    age: int | None
    date_of_birth: date | None
    address: str | None
    emergency_contact: str | None
    blood_group: str | None
    status: PatientStatus
    created_at: datetime
    visits: list[VisitSummary] = []
    prescriptions: list[PrescriptionSummary] = []
    medical_reports: list[ReportSummary] = []
    admissions: list[AdmissionSummary] = []
    bills: list[dict] = []  # placeholder until billing module


class AdmitPatientRequest(BaseModel):
    ward_id: UUID
    room_id: UUID
    bed_id: UUID
    doctor_id: UUID | None = None
    notes: str | None = None


class BedOption(BaseModel):
    id: UUID
    bed_code: str
    room_id: UUID
    room_code: str | None = None
    ward_id: UUID
    ward_name: str | None = None
    is_occupied: bool


class DischargeResponse(BaseModel):
    id: UUID
    status: AdmissionStatus
    discharged_at: datetime | None
