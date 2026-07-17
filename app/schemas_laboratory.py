from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import LabItemStatus, LabOrderStatus, LabSampleType


# ── Catalogue ──────────────────────────────────────────────────────────────────
class LabTestCreate(BaseModel):
    test_code: str = Field(min_length=1, max_length=32)
    test_name: str = Field(min_length=1, max_length=255)
    department: str = Field(default="Laboratory", min_length=1, max_length=128)
    price: float = Field(ge=0, default=0)
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


# ── Orders ─────────────────────────────────────────────────────────────────────
class LabOrderCreate(BaseModel):
    patient_id: UUID
    doctor_id: UUID | None = None
    test_ids: list[UUID] = Field(min_length=1)
    clinical_notes: str | None = None


class LabOrderItemResponse(BaseModel):
    id: UUID
    test_id: UUID | None
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
