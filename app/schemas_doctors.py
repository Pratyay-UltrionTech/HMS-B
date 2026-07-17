from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, Field

from app.utils.phone import PhoneNumber

from app.models import AppointmentStatus
from app.schemas_laboratory import LabOrderResponse
from app.schemas_radiology import RadOrderResponse
from app.schemas_ot import OtSurgeryResponse


class DoctorSummary(BaseModel):
    id: UUID
    name: str
    email: str
    phone: str
    role_name: str | None = None
    custom_values: dict = {}
    is_active: bool
    patient_count: int = 0
    today_appointment_count: int = 0


class PatientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    mobile: PhoneNumber
    age: int | None = Field(default=None, ge=0, le=150)
    gender: str | None = Field(default=None, max_length=32)
    address: str | None = None


class PatientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    mobile: PhoneNumber | None = None
    age: int | None = Field(default=None, ge=0, le=150)
    gender: str | None = Field(default=None, max_length=32)
    address: str | None = None


class PatientResponse(BaseModel):
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

    model_config = {"from_attributes": True}


class AppointmentCreate(BaseModel):
    patient_id: UUID
    appointment_date: date
    appointment_time: time
    purpose: str = Field(min_length=1, max_length=255)
    notes: str | None = None
    status: AppointmentStatus = AppointmentStatus.scheduled


class AppointmentUpdate(BaseModel):
    appointment_date: date | None = None
    appointment_time: time | None = None
    purpose: str | None = Field(default=None, min_length=1, max_length=255)
    notes: str | None = None
    status: AppointmentStatus | None = None


class AppointmentResponse(BaseModel):
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

    model_config = {"from_attributes": True}


class PrescriptionCreate(BaseModel):
    patient_id: UUID
    appointment_id: UUID | None = None
    symptoms: str = Field(min_length=1)
    diagnosis: str = Field(min_length=1)
    medicines: str = Field(min_length=1)
    dosage: str = Field(min_length=1)
    advice: str | None = None
    follow_up_date: date | None = None


class PrescriptionResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    doctor_id: UUID
    patient_id: UUID
    appointment_id: UUID | None
    symptoms: str
    diagnosis: str
    medicines: str
    dosage: str
    advice: str | None
    follow_up_date: date | None
    created_at: datetime
    patient_name: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None

    model_config = {"from_attributes": True}


class MedicalRecordCreate(BaseModel):
    patient_id: UUID
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
    report_type: str
    title: str
    notes: str | None
    file_name: str | None
    has_file: bool = False
    created_at: datetime
    patient_name: str | None = None
    doctor_name: str | None = None

    model_config = {"from_attributes": True}


class PatientHistoryResponse(BaseModel):
    patient: PatientResponse
    appointments: list[AppointmentResponse] = []
    prescriptions: list[PrescriptionResponse] = []
    medical_records: list[MedicalRecordResponse] = []
    lab_orders: list[LabOrderResponse] = []
    radiology_orders: list[RadOrderResponse] = []
    ot_surgeries: list[OtSurgeryResponse] = []


class HospitalClinicProfile(BaseModel):
    id: UUID
    hospital_id: str
    name: str
    address: str
    phone: str
    email: str
    slogan: str | None = None
    website: str | None = None
