"""
Target Pydantic contracts for Radiology domain.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.radiology.entities.radiology_entities import (
    RadiologyOrderStatus,
    RadPrescriptionRequestStatus,
    RadRequestItemStatus,
)


class RadScanCreate(BaseModel):
    scan_code: str = Field(min_length=1, max_length=32)
    scan_name: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=128, default="General")
    department: str = Field(min_length=1, max_length=128, default="Radiology")
    price: float = Field(ge=0)
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

    model_config = ConfigDict(from_attributes=True)


class RadOrderCreate(BaseModel):
    patient_id: UUID
    doctor_id: UUID | None = None
    appointment_id: UUID | None = None
    admission_id: UUID | None = None
    prescription_request_id: UUID | None = None
    scan_ids: list[UUID] = Field(default_factory=list)
    clinical_notes: str | None = None
    is_emergency: bool = False
    emergency_reason: str | None = None
    is_emergency_override: bool = False
    emergency_override_reason: str | None = None

    @model_validator(mode="after")
    def require_scans_or_request(self):
        if self.prescription_request_id:
            return self
        if not self.scan_ids:
            raise ValueError("Provide at least one scan, or a prescription request")
        return self


# ── Prescription requests ────────────────────────────────────────────────────
class RadPrescriptionRequestItemResponse(BaseModel):
    id: UUID
    scan_id: UUID | None
    scan_code: str
    scan_name: str
    category: str
    price: float
    sort_order: int = 0
    status: RadRequestItemStatus

    model_config = ConfigDict(from_attributes=True)


class RadPrescriptionRequestResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    prescription_id: UUID
    patient_id: UUID
    doctor_id: UUID
    appointment_id: UUID | None = None
    admission_id: UUID | None = None
    status: RadPrescriptionRequestStatus
    prescribed_scan_ids: list[UUID] = []
    clinical_notes: str | None = None
    cancel_reason: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    patient_name: str | None = None
    patient_uhid: str | None = None
    doctor_name: str | None = None
    scan_names: str | None = None
    scan_count: int = 0
    pending_scan_count: int = 0
    appointment_label: str | None = None
    is_financially_cleared: bool = False
    payment_status: str = "pending"
    outstanding_amount: float = 0.0
    items: list[RadPrescriptionRequestItemResponse] = []

    model_config = ConfigDict(from_attributes=True)


class RadRequestCancelBody(BaseModel):
    reason: str | None = None


class RadScheduleRequest(BaseModel):
    scheduled_at: datetime
    machine: str = Field(min_length=1, max_length=128)
    technician_name: str = Field(min_length=1, max_length=255)
    is_emergency_override: bool = False
    emergency_override_reason: str | None = None


class RadStartScanRequest(BaseModel):
    is_emergency_override: bool = False
    emergency_override_reason: str | None = None


class RadReportRequest(BaseModel):
    findings: str = Field(min_length=1)
    impression: str = Field(min_length=1)
    remarks: str | None = None
    report_date: date | None = None
    report_file_name: str | None = None
    report_file_data: str | None = None
    image_file_name: str | None = None
    image_file_data: str | None = None
    amendment_reason: str | None = None


class RadAttachmentResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    order_id: UUID
    file_name: str
    mime_type: str
    file_size: int
    storage_path: str
    attachment_type: str
    uploaded_by: str
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RadAttachmentUploadRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    file_data: str | None = Field(default=None, min_length=1)  # base64 encoded binary data
    file_data_base64: str | None = Field(default=None, min_length=1)  # legacy frontend alias
    mime_type: str = Field(default="application/octet-stream", max_length=128)
    attachment_type: str = Field(default="scan_plate", max_length=64)

    @model_validator(mode="after")
    def coalesce_file_data(self):
        data = (self.file_data or "").strip() or (self.file_data_base64 or "").strip()
        if not data:
            raise ValueError("file_data (or legacy file_data_base64) is required")
        self.file_data = data
        return self



class RadOrderResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    order_no: str
    patient_id: UUID
    doctor_id: UUID | None
    appointment_id: UUID | None = None
    admission_id: UUID | None = None
    prescription_id: UUID | None = None
    prescription_request_id: UUID | None = None
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
    attachments: list[RadAttachmentResponse] = []
    report_uploaded_by: str | None
    report_date: date | None
    is_amended: bool = False
    amendment_reason: str | None = None
    ordered_at: datetime
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None
    payment_status: str = "pending"
    is_financially_cleared: bool = False
    net_amount: float = 0.0
    amount_paid: float = 0.0
    outstanding_amount: float = 0.0

    model_config = ConfigDict(from_attributes=True)


class RadDashboardResponse(BaseModel):
    todays_orders: int
    pending_scans: int
    completed_scans: int
    reports_pending: int
    cancelled_orders: int
    scans_count: int = 0
    seeded_scans_estimate: int = 0


class RadCatalogueSeedResult(BaseModel):
    template_pack: str = "standard"
    added: int = 0
    already_existed: int = 0
    created_codes: list[str] = []
