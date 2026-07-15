from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import RadiologyOrderStatus


class RadScanCreate(BaseModel):
    scan_code: str = Field(min_length=1, max_length=32)
    scan_name: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=128, default="General")
    department: str = Field(min_length=1, max_length=128, default="Radiology")
    price: float = Field(ge=0, default=0)
    duration_minutes: int = Field(ge=1, le=1440, default=30)
    description: str | None = None
    is_active: bool = True


class RadScanUpdate(BaseModel):
    scan_code: str | None = Field(default=None, min_length=1, max_length=32)
    scan_name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=128)
    department: str | None = Field(default=None, min_length=1, max_length=128)
    price: float | None = Field(default=None, ge=0)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    description: str | None = None
    is_active: bool | None = None


class RadScanResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    scan_code: str
    scan_name: str
    category: str
    department: str
    price: float
    duration_minutes: int
    description: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class RadOrderCreate(BaseModel):
    patient_id: UUID
    doctor_id: UUID | None = None
    scan_ids: list[UUID] = Field(min_length=1)
    clinical_notes: str | None = None


class RadScheduleRequest(BaseModel):
    scheduled_at: datetime
    machine: str = Field(min_length=1, max_length=128)
    technician_name: str = Field(min_length=1, max_length=255)


class RadReportRequest(BaseModel):
    findings: str = Field(min_length=1)
    impression: str = Field(min_length=1)
    remarks: str | None = None
    report_date: date | None = None
    report_file_name: str | None = None
    report_file_data: str | None = None
    image_file_name: str | None = None
    image_file_data: str | None = None


class RadOrderResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    order_no: str
    patient_id: UUID
    doctor_id: UUID | None
    scan_id: UUID | None
    scan_code: str
    scan_name: str
    category: str
    price: float
    ordered_by_name: str
    ordered_by_role: str
    status: RadiologyOrderStatus
    clinical_notes: str | None
    scheduled_at: datetime | None
    machine: str | None
    technician_name: str | None
    started_at: datetime | None
    completed_at: datetime | None
    findings: str | None
    impression: str | None
    remarks: str | None
    report_file_name: str | None
    has_report_file: bool = False
    image_file_name: str | None
    has_image_file: bool = False
    report_uploaded_by: str | None
    report_date: date | None
    ordered_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None

    model_config = {"from_attributes": True}


class RadDashboardResponse(BaseModel):
    todays_orders: int
    pending_scans: int
    completed_scans: int
    reports_pending: int
    cancelled_orders: int
