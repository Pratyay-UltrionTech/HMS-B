from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import OtPriority, OtSurgeryStatus


class OtSurgeryCreate(BaseModel):
    patient_id: UUID
    surgeon_id: UUID | None = None
    assistant_surgeon: str | None = None
    surgery_type: str = Field(min_length=1, max_length=255)
    surgery_category: str = Field(min_length=1, max_length=128, default="General")
    priority: OtPriority = OtPriority.elective
    ot_room: str = Field(min_length=1, max_length=64, default="OT-1")
    scheduled_at: datetime
    duration_minutes: int = Field(ge=15, le=1440, default=60)
    anaesthetist: str | None = Field(default=None, max_length=255)
    remarks: str | None = None


class OtSurgeryUpdate(BaseModel):
    surgeon_id: UUID | None = None
    assistant_surgeon: str | None = None
    surgery_type: str | None = Field(default=None, min_length=1, max_length=255)
    surgery_category: str | None = Field(default=None, min_length=1, max_length=128)
    priority: OtPriority | None = None
    ot_room: str | None = Field(default=None, min_length=1, max_length=64)
    scheduled_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=15, le=1440)
    anaesthetist: str | None = None
    remarks: str | None = None


class OtRescheduleRequest(BaseModel):
    scheduled_at: datetime
    ot_room: str | None = Field(default=None, min_length=1, max_length=64)
    duration_minutes: int | None = Field(default=None, ge=15, le=1440)
    remarks: str | None = None


class OtCompleteRequest(BaseModel):
    shifted_to: str | None = Field(default=None, max_length=64)
    actual_duration_minutes: int | None = Field(default=None, ge=1, le=1440)


class OtNotesRequest(BaseModel):
    pre_op_diagnosis: str = Field(min_length=1)
    procedure_performed: str = Field(min_length=1)
    findings: str | None = None
    implants_used: str | None = None
    complications: str | None = None
    post_op_instructions: str | None = None
    follow_up_notes: str | None = None
    shifted_to: str | None = Field(default=None, max_length=64)
    ot_report_file_name: str | None = None
    ot_report_file_data: str | None = None
    consent_file_name: str | None = None
    consent_file_data: str | None = None
    image_file_name: str | None = None
    image_file_data: str | None = None


class OtSurgeryResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    surgery_no: str
    patient_id: UUID
    surgeon_id: UUID | None
    assistant_surgeon: str | None
    surgery_type: str
    surgery_category: str
    priority: OtPriority
    ot_room: str
    scheduled_at: datetime
    duration_minutes: int
    anaesthetist: str | None
    remarks: str | None
    booked_by_name: str
    booked_by_role: str
    status: OtSurgeryStatus
    started_at: datetime | None
    completed_at: datetime | None
    actual_duration_minutes: int | None
    shifted_to: str | None
    pre_op_diagnosis: str | None
    procedure_performed: str | None
    findings: str | None
    implants_used: str | None
    complications: str | None
    post_op_instructions: str | None
    follow_up_notes: str | None
    notes_recorded_by: str | None
    notes_recorded_at: datetime | None
    ot_report_file_name: str | None
    has_ot_report: bool = False
    consent_file_name: str | None
    has_consent: bool = False
    image_file_name: str | None
    has_image: bool = False
    has_notes: bool = False
    created_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    surgeon_name: str | None = None

    model_config = {"from_attributes": True}


class OtDashboardResponse(BaseModel):
    todays_surgeries: int
    completed: int
    ongoing: int
    scheduled: int
    cancelled: int
