"""
Pydantic contracts for Inpatient Admissions and IPD Form Submissions.

Conforms to UltrionTech-Backend-Template modules/inpatient/contracts/ specification.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from modules.inpatient.entities.admission import (
    AdmissionStatus,
    IpdFormSubmissionStatus,
)
from modules.inpatient.entities.care_team import AdmissionCareTeamRole
from modules.inpatient.entities.discharge_exception import ExceptionStatus
from modules.inpatient.entities.medication_order import MedicationOrderStatus


class AdmissionCareTeamCreate(BaseModel):
    doctor_id: UUID
    role: AdmissionCareTeamRole = AdmissionCareTeamRole.consulting_doctor
    notes: str | None = None


class AdmissionCareTeamResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    admission_id: UUID
    doctor_id: UUID
    doctor_name: str | None = None
    doctor_department: str | None = None
    role: AdmissionCareTeamRole
    is_active: bool
    assigned_at: datetime
    assigned_by_id: UUID | None = None
    assigned_by_name: str | None = None
    ended_at: datetime | None = None
    notes: str | None = None

    model_config = ConfigDict(from_attributes=True)


class IpdMedicationOrderCreate(BaseModel):
    medicine_id: UUID | None = None
    medicine_name: str = Field(min_length=1)
    dose: str = Field(min_length=1)
    dosage_unit: str = "mg"
    route: str = "oral"
    frequency: str = "od"
    schedule_timing: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_days: int | None = None
    instructions: str | None = None
    is_prn: bool = False
    prn_indication: str | None = None


class IpdMedicationOrderResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    admission_id: UUID
    patient_id: UUID
    doctor_id: UUID
    doctor_name: str
    medicine_id: UUID | None = None
    medicine_name: str
    dose: str
    dosage_unit: str
    route: str
    frequency: str
    schedule_timing: str | None = None
    start_time: datetime
    end_time: datetime | None = None
    duration_days: int | None = None
    instructions: str | None = None
    is_prn: bool
    prn_indication: str | None = None
    status: MedicationOrderStatus
    discontinued_at: datetime | None = None
    discontinued_by_id: UUID | None = None
    discontinued_by_name: str | None = None
    discontinued_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DiscontinueMedicationOrderRequest(BaseModel):
    reason: str = Field(min_length=1)


class AdministerPrnMedicationRequest(BaseModel):
    indication: str = Field(min_length=1, description="Clinical reason/indication for PRN administration")
    dose: str | None = None
    route: str | None = None
    notes: str | None = None
    administered_at: datetime | None = None


class DischargeFinancialExceptionCreate(BaseModel):
    reason: str = Field(min_length=1)
    recommendation: str | None = None


class DischargeFinancialExceptionDecision(BaseModel):
    status: ExceptionStatus  # approved | rejected
    approval_remarks: str | None = None


class DischargeFinancialExceptionResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    admission_id: UUID
    financial_account_id: UUID
    patient_id: UUID
    patient_name: str | None = None
    patient_uhid: str | None = None
    outstanding_amount_at_request: float
    reason: str
    recommendation: str | None = None
    requested_by_id: UUID
    requested_by_name: str
    requested_at: datetime
    status: ExceptionStatus
    approved_by_id: UUID | None = None
    approved_by_name: str | None = None
    approved_at: datetime | None = None
    approval_remarks: str | None = None
    approved_amount: float | None = None
    remaining_receivable: float | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdmitRequest(BaseModel):
    patient_id: UUID
    ward_id: UUID
    room_id: UUID
    bed_id: UUID
    doctor_id: UUID | None = None
    admission_date: date | None = None
    notes: str | None = None
    estimated_amount: float | None = None
    requested_advance: float | None = None
    department_id: UUID | None = None
    department_name: str | None = None


class AllocateRequest(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    ward_id: UUID
    room_id: UUID
    bed_id: UUID


class TransferRequest(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    to_ward_id: UUID
    to_room_id: UUID
    to_bed_id: UUID


class DischargeRequestCreate(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    discharge_notes: str | None = Field(default=None, max_length=2000)


class DischargeRequest(BaseModel):
    admission_id: UUID | None = None
    patient_id: UUID | None = None
    discharge_date: date | None = None
    discharge_time: time | None = None
    discharge_notes: str | None = Field(default=None, max_length=2000)
    require_discharge_summary: bool | None = None
    no_discharge_meds: bool = False
    no_discharge_meds_reason: str | None = None


class NoDischargeMedsRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class DischargeMedicationStatusResponse(BaseModel):
    admission_id: UUID
    has_discharge_prescription: bool
    prescription_id: UUID | None = None
    no_discharge_meds: bool = False
    no_discharge_meds_reason: str | None = None
    no_discharge_meds_doctor_id: UUID | None = None
    no_discharge_meds_doctor_name: str | None = None
    can_discharge_medically: bool


class SuggestedDischargeMedication(BaseModel):
    id: UUID
    medicine_id: UUID | None = None
    medicine_name: str
    dose: str
    dosage_unit: str = "mg"
    route: str = "oral"
    frequency: str = "od"
    instructions: str | None = None
    is_prn: bool = False
    prn_indication: str | None = None
    status: str
    last_administered_at: datetime | None = None


class AdmissionDetail(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    patient_name: str | None = None
    patient_current_name: str | None = None
    patient_date_of_birth: date | None = None
    patient_current_age: int | None = None
    patient_current_gender: str | None = None
    gender_at_admission: str | None = None
    age_at_admission: int | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    # Nullable: requested / awaiting-bed admissions have no physical bed yet.
    ward_id: UUID | None = None
    room_id: UUID | None = None
    bed_id: UUID | None = None
    ward_name: str | None = None
    room_code: str | None = None
    bed_code: str | None = None
    doctor_id: UUID | None = None
    doctor_name: str | None = None
    primary_doctor_name: str | None = None
    department_id: UUID | None = None
    department_name: str | None = None
    status: AdmissionStatus
    # Derived flag: admitted without a bed. Frontends must render
    # "Admitted — Awaiting Bed", never plain "Admitted".
    is_awaiting_bed: bool = False
    notes: str | None = None
    discharge_notes: str | None = None
    admitted_at: datetime
    discharged_at: datetime | None = None
    admission_fee: float = 0.0
    bed_charge_per_day: float = 0.0
    ip_id: str | None = None
    op_id: str | None = None
    source_appointment_id: UUID | None = None
    no_discharge_meds: bool = False
    no_discharge_meds_reason: str | None = None
    no_discharge_meds_doctor_id: UUID | None = None
    care_team: list[AdmissionCareTeamResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class DischargeQueueItem(AdmissionDetail):
    total_charges: float = 0.0
    total_paid: float = 0.0
    total_deposits_available: float = 0.0
    outstanding: float = 0.0
    net_patient_balance: float = 0.0
    can_discharge: bool = False
    financial_exception_approved: bool = False
    financial_exception_id: UUID | None = None


class AdmitPatientRequest(BaseModel):
    ward_id: UUID
    room_id: UUID
    bed_id: UUID
    doctor_id: UUID | None = None
    notes: str | None = None
    estimated_amount: float | None = None
    requested_advance: float | None = None
    department_id: UUID | None = None
    department_name: str | None = None


class AdmissionRequestCreate(BaseModel):
    """Payload for creating an admission request (no bed required)."""

    patient_id: UUID
    doctor_id: UUID | None = None
    notes: str | None = None
    source_appointment_id: UUID | None = None


class AdmissionAcceptRequest(BaseModel):
    """Accept a requested admission, optionally assigning a bed.

    Bed fields all-None → "Admitted — Awaiting Bed".
    All bed fields set → "Admitted — Bed Assigned".
    """

    ward_id: UUID | None = None
    room_id: UUID | None = None
    bed_id: UUID | None = None
    doctor_id: UUID | None = None
    notes: str | None = None


class AdmissionSummary(BaseModel):
    id: UUID
    ward_id: UUID | None = None
    room_id: UUID | None = None
    bed_id: UUID | None = None
    ward_name: str | None = None
    room_code: str | None = None
    bed_code: str | None = None
    doctor_name: str | None = None
    status: AdmissionStatus
    admitted_at: datetime
    discharged_at: datetime | None = None
    notes: str | None = None
    ip_id: str | None = None
    op_id: str | None = None

    model_config = ConfigDict(from_attributes=True)


class DischargeResponse(BaseModel):
    id: UUID
    status: AdmissionStatus
    discharged_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class IpdFormSubmissionCreate(BaseModel):
    patient_id: UUID
    admission_id: UUID | None = None
    form_id: str = Field(min_length=1, max_length=128)
    form_title: str = Field(min_length=1, max_length=255)
    form_data: dict[str, Any] = Field(default_factory=dict)
    html_snapshot: str | None = None
    status: IpdFormSubmissionStatus = IpdFormSubmissionStatus.draft


class IpdFormSubmissionUpdate(BaseModel):
    admission_id: UUID | None = None
    form_data: dict[str, Any] | None = None
    html_snapshot: str | None = None
    status: IpdFormSubmissionStatus | None = None
    form_title: str | None = Field(default=None, min_length=1, max_length=255)


class IpdFormSubmissionResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    admission_id: UUID | None = None
    form_id: str
    form_title: str
    form_data: dict[str, Any]
    status: IpdFormSubmissionStatus
    filled_by_id: UUID | None = None
    filled_by_name: str = ""
    filled_by_role: str = ""
    medical_record_id: UUID | None = None
    patient_document_id: UUID | None = None
    has_html_snapshot: bool = False
    addenda_count: int = 0
    created_at: datetime
    updated_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None

    model_config = ConfigDict(from_attributes=True)


class IpdFormAddendumCreate(BaseModel):
    reason: str = Field(min_length=1, description="Reason for addendum")
    content: str = Field(min_length=1, description="Addendum content")


class IpdFormAddendumResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    submission_id: UUID
    admission_id: UUID | None = None
    patient_id: UUID
    author_id: UUID
    author_name: str
    author_role: str
    reason: str
    content: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

