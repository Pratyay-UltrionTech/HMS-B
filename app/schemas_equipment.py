from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.utils.phone import OptionalPhoneNumber

from app.models import (
    EquipmentAssignTarget,
    EquipmentRequestStatus,
    EquipmentStatus,
    MaintenanceStatus,
)


class EquipCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    is_active: bool = True


class EquipCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    is_active: bool | None = None


class EquipCategoryResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    equipment_count: int = 0

    model_config = {"from_attributes": True}


class EquipmentCreate(BaseModel):
    asset_id: str | None = Field(default=None, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    category_id: UUID | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    purchase_date: date | None = None
    purchase_cost: float = Field(ge=0, default=0)
    department: str | None = None
    current_location: str | None = None
    status: EquipmentStatus = EquipmentStatus.available
    vendor: str | None = None
    warranty_start: date | None = None
    warranty_end: date | None = None
    amc_start: date | None = None
    amc_end: date | None = None
    vendor_contact: OptionalPhoneNumber = None
    notes: str | None = None


class EquipmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    category_id: UUID | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    purchase_date: date | None = None
    purchase_cost: float | None = Field(default=None, ge=0)
    department: str | None = None
    current_location: str | None = None
    status: EquipmentStatus | None = None
    vendor: str | None = None
    warranty_start: date | None = None
    warranty_end: date | None = None
    amc_start: date | None = None
    amc_end: date | None = None
    vendor_contact: OptionalPhoneNumber = None
    notes: str | None = None


class EquipmentResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    asset_id: str
    name: str
    category_id: UUID | None
    category_name: str | None = None
    manufacturer: str | None
    model: str | None
    serial_number: str | None
    purchase_date: date | None
    purchase_cost: float
    department: str | None
    current_location: str | None
    status: EquipmentStatus
    vendor: str | None
    warranty_start: date | None
    warranty_end: date | None
    amc_start: date | None
    amc_end: date | None
    vendor_contact: str | None
    notes: str | None
    created_at: datetime
    active_assignment: str | None = None

    model_config = {"from_attributes": True}


class AssignmentCreate(BaseModel):
    equipment_id: UUID
    target_type: EquipmentAssignTarget = EquipmentAssignTarget.department
    target_name: str = Field(min_length=1, max_length=255)
    remarks: str | None = None


class AssignmentResponse(BaseModel):
    id: UUID
    equipment_id: UUID
    equipment_name: str | None = None
    asset_id: str | None = None
    target_type: EquipmentAssignTarget
    target_name: str
    assigned_by_name: str
    assigned_at: datetime
    returned_at: datetime | None
    is_active: bool
    remarks: str | None

    model_config = {"from_attributes": True}


class MaintenanceCreate(BaseModel):
    equipment_id: UUID
    last_service_date: date | None = None
    next_service_date: date
    remarks: str | None = None


class MaintenanceComplete(BaseModel):
    work_done: str = Field(min_length=1)
    engineer: str | None = None
    cost: float = Field(ge=0, default=0)
    remarks: str | None = None
    next_service_date: date | None = None


class MaintenanceResponse(BaseModel):
    id: UUID
    equipment_id: UUID
    equipment_name: str | None = None
    asset_id: str | None = None
    last_service_date: date | None
    next_service_date: date
    status: MaintenanceStatus
    remarks: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class ServiceLogCreate(BaseModel):
    equipment_id: UUID
    service_date: date
    work_done: str = Field(min_length=1)
    engineer: str | None = None
    cost: float = Field(ge=0, default=0)
    remarks: str | None = None


class ServiceLogResponse(BaseModel):
    id: UUID
    equipment_id: UUID
    equipment_name: str | None = None
    asset_id: str | None = None
    service_date: date
    work_done: str
    engineer: str | None
    cost: float
    remarks: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AmcUpdate(BaseModel):
    vendor: str | None = None
    warranty_start: date | None = None
    warranty_end: date | None = None
    amc_start: date | None = None
    amc_end: date | None = None
    vendor_contact: OptionalPhoneNumber = None


class RequestCreate(BaseModel):
    department: str = Field(min_length=1, max_length=128)
    equipment_name: str = Field(min_length=1, max_length=255)
    quantity: int = Field(ge=1, le=100, default=1)
    remarks: str | None = None


class RequestAction(BaseModel):
    admin_remarks: str | None = None
    equipment_id: UUID | None = None


class RequestResponse(BaseModel):
    id: UUID
    request_no: str
    department: str
    equipment_name: str
    quantity: int
    status: EquipmentRequestStatus
    requested_by_name: str
    remarks: str | None
    admin_remarks: str | None
    assigned_equipment_id: UUID | None
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class EquipDashboardResponse(BaseModel):
    total: int
    available: int
    in_use: int
    under_maintenance: int
    out_of_service: int
