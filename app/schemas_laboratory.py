from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.models import (
    LabItemStatus,
    LabOrderSource,
    LabOrderStatus,
    LabPrescriptionRequestStatus,
    LabRequestItemStatus,
    LabSampleType,
)


# ── Catalogue ──────────────────────────────────────────────────────────────────
class LabTestCreate(BaseModel):
    test_code: str = Field(min_length=1, max_length=32)
    test_name: str = Field(min_length=1, max_length=255)
    department: str = Field(default="Laboratory", min_length=1, max_length=128)
    price: float = Field(ge=0)
    sample_type: LabSampleType = LabSampleType.blood
    tat_hours: int = Field(ge=1, le=720, default=24)
    description: str | None = None
    is_active: bool = True


class LabTestUpdate(BaseModel):
    test_code: str | None = Field(default=None, min_length=1, max_length=32)
    test_name: str | None = Field(default=None, min_length=1, max_length=255)
    department: str | None = Field(default=None, min_length=1, max_length=128)
    price: float | None = Field(default=None, ge=0)
    sample_type: LabSampleType | None = None
    tat_hours: int | None = Field(default=None, ge=1, le=720)
    description: str | None = None
    is_active: bool | None = None


class LabTestResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    test_code: str
    test_name: str
    department: str
    price: float
    sample_type: LabSampleType
    tat_hours: int
    description: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Panels ─────────────────────────────────────────────────────────────────────
class LabPanelCreate(BaseModel):
    panel_code: str = Field(min_length=1, max_length=32)
    panel_name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    test_ids: list[UUID] = Field(default_factory=list)


class LabPanelUpdate(BaseModel):
    panel_code: str | None = Field(default=None, min_length=1, max_length=32)
    panel_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    test_ids: list[UUID] | None = None


class LabPanelTestMember(BaseModel):
    test_id: UUID
    test_code: str
    test_name: str
    sample_type: LabSampleType | None = None
    is_active: bool = True
    sort_order: int = 0


class LabPanelResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    panel_code: str
    panel_name: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime | None = None
    test_count: int = 0
    tests: list[LabPanelTestMember] = []

    model_config = {"from_attributes": True}


# ── Prescription requests ──────────────────────────────────────────────────────
class LabPrescriptionRequestItemResponse(BaseModel):
    id: UUID
    test_id: UUID | None
    panel_id: UUID | None = None
    panel_name: str | None = None
    test_code: str
    test_name: str
    department: str
    price: float
    sort_order: int = 0
    status: LabRequestItemStatus

    model_config = {"from_attributes": True}


class LabPrescriptionRequestResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    prescription_id: UUID
    patient_id: UUID
    doctor_id: UUID
    appointment_id: UUID | None = None
    status: LabPrescriptionRequestStatus
    prescribed_test_ids: list[UUID] = []
    prescribed_panel_ids: list[UUID] = []
    clinical_notes: str | None = None
    cancel_reason: str | None = None
    lab_order_id: UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None
    patient_name: str | None = None
    patient_uhid: str | None = None
    doctor_name: str | None = None
    panel_names: str | None = None
    test_names: str | None = None
    test_count: int = 0
    pending_test_count: int = 0
    appointment_label: str | None = None
    items: list[LabPrescriptionRequestItemResponse] = []

    model_config = {"from_attributes": True}


class LabRequestCancelBody(BaseModel):
    reason: str | None = None


# ── Orders ─────────────────────────────────────────────────────────────────────
class LabOrderCreate(BaseModel):
    patient_id: UUID
    doctor_id: UUID | None = None
    appointment_id: UUID | None = None
    prescription_request_id: UUID | None = None
    test_ids: list[UUID] = Field(default_factory=list)
    panel_ids: list[UUID] = Field(default_factory=list)
    clinical_notes: str | None = None

    @model_validator(mode="after")
    def require_tests_panels_or_request(self):
        if self.prescription_request_id:
            return self
        if not self.test_ids and not self.panel_ids:
            raise ValueError("Provide at least one test or panel, or a prescription request")
        return self


class LabOrderItemResponse(BaseModel):
    id: UUID
    test_id: UUID | None
    panel_id: UUID | None = None
    panel_name: str | None = None
    test_code: str
    test_name: str
    department: str
    price: float
    status: LabItemStatus

    model_config = {"from_attributes": True}


class LabResultResponse(BaseModel):
    id: UUID
    order_item_id: UUID | None
    parameter_name: str
    result_value: str
    unit: str | None
    reference_range: str | None
    remarks: str | None
    sort_order: int

    model_config = {"from_attributes": True}


class LabOrderResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    order_no: str
    patient_id: UUID
    doctor_id: UUID | None
    appointment_id: UUID | None = None
    prescription_id: UUID | None = None
    prescription_request_id: UUID | None = None
    order_source: LabOrderSource = LabOrderSource.self_requested
    ordered_by_name: str
    ordered_by_role: str
    status: LabOrderStatus
    clinical_notes: str | None
    sample_type: LabSampleType | None
    collected_at: datetime | None
    collected_by: str | None
    collection_remarks: str | None
    ordered_at: datetime
    completed_at: datetime | None
    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_mobile: str | None = None
    doctor_name: str | None = None
    test_names: str | None = None
    panel_names: str | None = None
    items: list[LabOrderItemResponse] = []
    results: list[LabResultResponse] = []

    model_config = {"from_attributes": True}


class SampleCollectRequest(BaseModel):
    collected_at: datetime | None = None
    collected_by: str = Field(min_length=1, max_length=255)
    sample_type: LabSampleType | None = None
    collection_remarks: str | None = None


class ItemStatusUpdate(BaseModel):
    status: LabItemStatus


class LabResultInput(BaseModel):
    order_item_id: UUID | None = None
    parameter_name: str = Field(min_length=1, max_length=255)
    result_value: str = Field(min_length=1, max_length=128)
    unit: str | None = Field(default=None, max_length=64)
    reference_range: str | None = Field(default=None, max_length=128)
    remarks: str | None = None
    sort_order: int = 0


class LabReportSaveRequest(BaseModel):
    results: list[LabResultInput] = Field(min_length=1)
    mark_completed: bool = True


class LabDashboardResponse(BaseModel):
    todays_orders: int
    pending: int
    completed: int
    cancelled: int
    sample_collected: int
    in_progress: int
    panels_count: int = 0
    tests_count: int = 0
    seeded_tests_estimate: int = 0
    top_panels: list[dict] = []
    pending_doctor_requests: int = 0
    doctor_prescribed_orders: int = 0
    self_requested_orders: int = 0
    pending_requests: list[LabPrescriptionRequestResponse] = []


class LabCatalogueSeedResult(BaseModel):
    template_pack: str = "standard"
    tests_added: int = 0
    tests_already_existed: int = 0
    panels_added: int = 0
    panels_already_existed: int = 0
    created_test_codes: list[str] = []
    created_panel_codes: list[str] = []
