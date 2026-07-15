from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import WardType


# ── Wings ──────────────────────────────────────────────────────────────────────
class WingCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str | None = Field(default=None, max_length=32)
    description: str | None = None
    is_active: bool = True


class WingUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    code: str | None = None
    description: str | None = None
    is_active: bool | None = None


class WingResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    name: str
    code: str | None
    description: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Departments ────────────────────────────────────────────────────────────────
class DepartmentCreate(BaseModel):
    wing_id: UUID
    name: str = Field(min_length=1, max_length=255)
    code: str | None = Field(default=None, max_length=32)
    description: str | None = None
    is_active: bool = True


class DepartmentUpdate(BaseModel):
    wing_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    code: str | None = None
    description: str | None = None
    is_active: bool | None = None


class DepartmentResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    wing_id: UUID
    name: str
    code: str | None
    description: str | None
    is_active: bool
    created_at: datetime
    wing_name: str | None = None

    model_config = {"from_attributes": True}


# ── Shift types ────────────────────────────────────────────────────────────────
class ShiftTypeCreate(BaseModel):
    department_id: UUID
    name: str = Field(min_length=1, max_length=255)
    start_time: time
    end_time: time
    description: str | None = None
    is_active: bool = True


class ShiftTypeUpdate(BaseModel):
    department_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    start_time: time | None = None
    end_time: time | None = None
    description: str | None = None
    is_active: bool | None = None


class ShiftTypeResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    department_id: UUID
    name: str
    start_time: time
    end_time: time
    description: str | None
    is_active: bool
    created_at: datetime
    department_name: str | None = None

    model_config = {"from_attributes": True}


# ── Holidays ───────────────────────────────────────────────────────────────────
class HolidayCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    holiday_date: date
    description: str | None = None
    is_recurring: bool = False


class HolidayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    holiday_date: date | None = None
    description: str | None = None
    is_recurring: bool | None = None


class HolidayResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    name: str
    holiday_date: date
    description: str | None
    is_recurring: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Appointment types ──────────────────────────────────────────────────────────
class AppointmentTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slot_duration_minutes: int = Field(default=15, ge=5, le=480)
    description: str | None = None
    is_active: bool = True


class AppointmentTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slot_duration_minutes: int | None = Field(default=None, ge=5, le=480)
    description: str | None = None
    is_active: bool | None = None


class AppointmentTypeResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    name: str
    slot_duration_minutes: int
    description: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Wards ──────────────────────────────────────────────────────────────────────
class WardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    ward_type: WardType
    wing_id: UUID | None = None
    department_id: UUID | None = None
    description: str | None = None
    is_active: bool = True


class WardUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    ward_type: WardType | None = None
    wing_id: UUID | None = None
    department_id: UUID | None = None
    description: str | None = None
    is_active: bool | None = None


class WardResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    wing_id: UUID | None
    department_id: UUID | None
    name: str
    ward_type: WardType
    description: str | None
    is_active: bool
    created_at: datetime
    wing_name: str | None = None
    department_name: str | None = None

    model_config = {"from_attributes": True}


# ── Rooms ──────────────────────────────────────────────────────────────────────
class RoomCreate(BaseModel):
    ward_id: UUID
    room_code: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    bed_count: int = Field(default=1, ge=1, le=100)
    is_active: bool = True


class RoomUpdate(BaseModel):
    ward_id: UUID | None = None
    room_code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = None
    bed_count: int | None = Field(default=None, ge=1, le=100)
    is_active: bool | None = None


class RoomResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    ward_id: UUID
    room_code: str
    name: str | None
    bed_count: int
    is_active: bool
    created_at: datetime
    ward_name: str | None = None

    model_config = {"from_attributes": True}


# ── Suppliers ──────────────────────────────────────────────────────────────────
class SupplierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact_person: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = None
    is_active: bool = True


class SupplierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    is_active: bool | None = None


class SupplierResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    name: str
    contact_person: str | None
    phone: str | None
    email: str | None
    address: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
