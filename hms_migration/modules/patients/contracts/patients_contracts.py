"""
Request and response contracts (Pydantic schemas) for the Patient domain.

Conforms to UltrionTech-Backend-Template modules/patients/contracts/ specification.
Preserves exact wire formats, validation constraints, and serialization rules
of OG HMS-B registration contracts.
"""

from datetime import date, datetime
import enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from hms_migration.shared.validators.phone import OptionalPhoneNumber, PhoneNumber
from hms_migration.modules.patients.entities.patient import PatientStatus
from hms_migration.modules.patients.validators.patients_validator import (
    EMERGENCY_RELATIONS,
    EmergencyRelation,
    normalize_optional_text,
    validate_emergency_contact_bundle,
)


class AdmissionStatus(str, enum.Enum):
    """Status of an inpatient admission."""

    admitted = "admitted"
    discharge_requested = "discharge_requested"
    discharged = "discharged"


class PatientRegister(BaseModel):
    """Payload for registering a new patient."""

    first_name: str = Field(min_length=1, max_length=128)
    last_name: str = Field(min_length=1, max_length=128)
    gender: str = Field(min_length=1, max_length=32)
    date_of_birth: date | None = None
    age: int | None = Field(default=None, ge=0, le=150)
    mobile: PhoneNumber
    email: EmailStr | None = None
    address: str | None = None
    emergency_contact: PhoneNumber
    emergency_contact_name: str = Field(min_length=1, max_length=128)
    emergency_contact_relation: EmergencyRelation
    blood_group: str | None = Field(default=None, max_length=16)
    has_insurance: bool = False
    insurance_provider: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_emergency_and_insurance(self) -> "PatientRegister":
        validate_emergency_contact_bundle(
            name=self.emergency_contact_name,
            relation=self.emergency_contact_relation,
            phone=self.emergency_contact,
            required=True,
        )
        if self.mobile and self.emergency_contact and self.mobile == self.emergency_contact:
            raise ValueError("Emergency contact number must be different from patient mobile number")
        if not self.has_insurance:
            object.__setattr__(self, "insurance_provider", None)
        else:
            object.__setattr__(
                self,
                "insurance_provider",
                normalize_optional_text(self.insurance_provider, max_len=255),
            )
        object.__setattr__(
            self,
            "emergency_contact_name",
            self.emergency_contact_name.strip(),
        )
        return self


class PatientRegisterUpdate(BaseModel):
    """Payload for updating an existing patient record."""

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
    def normalize_optional_fields(self) -> "PatientRegisterUpdate":
        data = self.model_dump(exclude_unset=True)
        if "emergency_contact_name" in data:
            object.__setattr__(
                self,
                "emergency_contact_name",
                normalize_optional_text(self.emergency_contact_name, max_len=128),
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
                normalize_optional_text(self.insurance_provider, max_len=255),
            )
        if data.get("has_insurance") is False:
            object.__setattr__(self, "insurance_provider", None)
        return self


class PatientDirectoryItem(BaseModel):
    """Summary item for patient list and directory view."""

    model_config = ConfigDict(from_attributes=True)

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


class VisitSummary(BaseModel):
    """Summary item for an outpatient visit / appointment in patient profile."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appointment_date: date
    appointment_time: str
    doctor_name: str | None = None
    purpose: str
    visit_type: str
    status: str
    op_id: str | None = None


class PrescriptionSummary(BaseModel):
    """Summary item for a prescription in patient profile."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    diagnosis: str
    medicines: str
    doctor_name: str | None = None
    created_at: datetime


class ReportSummary(BaseModel):
    """Summary item for a medical report in patient profile."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    report_type: str
    title: str
    notes: str | None
    created_at: datetime
    doctor_name: str | None = None


class AdmissionSummary(BaseModel):
    """Summary item for an inpatient admission in patient profile."""

    model_config = ConfigDict(from_attributes=True)

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
    ip_id: str | None = None


class PatientProfile(BaseModel):
    """Comprehensive 360-degree patient profile response."""

    model_config = ConfigDict(from_attributes=True)

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
    bills: list[dict[str, Any]] = []
    financial_summary: dict[str, Any] | None = None
