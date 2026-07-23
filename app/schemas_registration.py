from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.utils.phone import OptionalPhoneNumber, PhoneNumber

from app.models import AdmissionStatus, PatientStatus


BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "Unknown"]

EMERGENCY_RELATIONS = ("Father", "Mother", "Spouse", "Sibling", "Child", "Friend", "Other")
EmergencyRelation = Literal["Father", "Mother", "Spouse", "Sibling", "Child", "Friend", "Other"]


def _normalize_optional_text(value: str | None, *, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if max_len is not None and len(cleaned) > max_len:
        raise ValueError(f"must be at most {max_len} characters")
    return cleaned


def validate_emergency_contact_bundle(
    *,
    name: str | None,
    relation: str | None,
    phone: str | None,
) -> None:
    """If any emergency field is present, phone is required."""
    name_n = _normalize_optional_text(name)
    relation_n = _normalize_optional_text(relation)
    phone_n = phone.strip() if isinstance(phone, str) and phone.strip() else phone
    if relation_n and relation_n not in EMERGENCY_RELATIONS:
        raise ValueError(f"emergency_contact_relation must be one of: {', '.join(EMERGENCY_RELATIONS)}")
    if (name_n or relation_n or phone_n) and not phone_n:
        raise ValueError("emergency_contact phone is required when any emergency contact field is entered")


class PatientRegister(BaseModel):
    first_name: str = Field(min_length=1, max_length=128)
    last_name: str = Field(min_length=1, max_length=128)
    gender: str = Field(min_length=1, max_length=32)
    date_of_birth: date | None = None
    age: int | None = Field(default=None, ge=0, le=150)
    mobile: PhoneNumber
    email: EmailStr | None = None
    address: str | None = None
    emergency_contact: OptionalPhoneNumber = None
    emergency_contact_name: str | None = Field(default=None, max_length=128)
    emergency_contact_relation: EmergencyRelation | None = None
    blood_group: str | None = Field(default=None, max_length=16)
    has_insurance: bool = False
    insurance_provider: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_emergency_and_insurance(self):
        validate_emergency_contact_bundle(
            name=self.emergency_contact_name,
            relation=self.emergency_contact_relation,
            phone=self.emergency_contact,
        )
        if not self.has_insurance:
            object.__setattr__(self, "insurance_provider", None)
        else:
            object.__setattr__(
                self,
                "insurance_provider",
                _normalize_optional_text(self.insurance_provider, max_len=255),
            )
        object.__setattr__(
            self,
            "emergency_contact_name",
            _normalize_optional_text(self.emergency_contact_name, max_len=128),
        )
        return self


class PatientRegisterUpdate(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=128)
    last_name: str | None = Field(default=None, min_length=1, max_length=128)
    gender: str | None = Field(default=None, min_length=1, max_length=32)
    date_of_birth: date | None = None
    age: int | None = Field(default=None, ge=0, le=150)
    mobile: PhoneNumber | None = None
    email: EmailStr | None = None
    address: str | None = None
    emergency_contact: OptionalPhoneNumber = None
    emergency_contact_name: str | None = Field(default=None, max_length=128)
    emergency_contact_relation: EmergencyRelation | None = None
    blood_group: str | None = Field(default=None, max_length=16)
    has_insurance: bool | None = None
    insurance_provider: str | None = Field(default=None, max_length=255)
    status: PatientStatus | None = None

    @model_validator(mode="after")
    def normalize_optional_fields(self):
        # Emergency phone requirement is validated after merge with existing patient in the update router.
        data = self.model_dump(exclude_unset=True)
        if "emergency_contact_name" in data:
            object.__setattr__(
                self,
                "emergency_contact_name",
                _normalize_optional_text(self.emergency_contact_name, max_len=128),
            )
        if "emergency_contact_relation" in data and self.emergency_contact_relation is not None:
            if self.emergency_contact_relation not in EMERGENCY_RELATIONS:
                raise ValueError(
                    f"emergency_contact_relation must be one of: {', '.join(EMERGENCY_RELATIONS)}"
                )
        if "insurance_provider" in data:
            object.__setattr__(
                self,
                "insurance_provider",
                _normalize_optional_text(self.insurance_provider, max_len=255),
            )
        if data.get("has_insurance") is False:
            object.__setattr__(self, "insurance_provider", None)
        return self


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
    emergency_contact_name: str | None = None
    emergency_contact_relation: str | None = None
    blood_group: str | None
    has_insurance: bool = False
    insurance_provider: str | None = None
    insurance_details: dict | None = None
    status: PatientStatus
    created_at: datetime
    visits: list[VisitSummary] = []
    prescriptions: list[PrescriptionSummary] = []
    medical_reports: list[ReportSummary] = []
    admissions: list[AdmissionSummary] = []
    bills: list[dict] = []
    financial_summary: dict | None = None


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
