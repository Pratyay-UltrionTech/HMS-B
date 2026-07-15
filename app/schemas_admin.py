from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.models import CustomFieldType

BASIC_MODULE_KEYS = [
    "masters",
    "admin",
    "doctors",
    "registration",
    "appointment",
    "bed",
    "laboratory",
    "radiology",
    "ot",
    "dms",
    "equipment",
    "mis",
]

BASIC_MODULE_LABELS = {
    "masters": "Masters Management",
    "admin": "Admin Management",
    "doctors": "Doctors Management",
    "registration": "Registration",
    "appointment": "Appointment",
    "bed": "Bed Management",
    "laboratory": "Laboratory",
    "radiology": "Radiology",
    "ot": "Operation Theatre",
    "dms": "Document Management",
    "equipment": "Equipment Management",
    "mis": "MIS Reports",
}


# ── Role fields ────────────────────────────────────────────────────────────────
class RoleFieldInput(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    field_type: CustomFieldType = CustomFieldType.text
    options: str | None = None
    is_required: bool = False
    sort_order: int = 0


class RoleFieldResponse(BaseModel):
    id: UUID
    label: str
    field_key: str
    field_type: CustomFieldType
    options: str | None
    is_required: bool
    sort_order: int

    model_config = {"from_attributes": True}


# ── Role permissions ───────────────────────────────────────────────────────────
class RolePermissionInput(BaseModel):
    module_key: str
    can_view: bool = False
    can_edit: bool = False


class RolePermissionResponse(BaseModel):
    id: UUID
    module_key: str
    can_view: bool
    can_edit: bool

    model_config = {"from_attributes": True}


# ── Roles ──────────────────────────────────────────────────────────────────────
class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    fields: list[RoleFieldInput] = []
    permissions: list[RolePermissionInput] = []


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    fields: list[RoleFieldInput] | None = None
    permissions: list[RolePermissionInput] | None = None


class RoleResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    fields: list[RoleFieldResponse] = []
    permissions: list[RolePermissionResponse] = []

    model_config = {"from_attributes": True}


class ModuleInfo(BaseModel):
    key: str
    label: str


# ── Hospital users ─────────────────────────────────────────────────────────────
class HospitalUserCreate(BaseModel):
    role_id: UUID
    name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=1, max_length=32)
    email: EmailStr
    password: str = Field(min_length=4, max_length=128)
    shift_id: UUID | None = None
    custom_values: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class HospitalUserUpdate(BaseModel):
    role_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, min_length=1, max_length=32)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=4, max_length=128)
    shift_id: UUID | None = None
    custom_values: dict[str, Any] | None = None
    is_active: bool | None = None


class HospitalUserResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    role_id: UUID
    shift_id: UUID | None = None
    name: str
    phone: str
    email: EmailStr
    custom_values: dict[str, Any]
    is_active: bool
    created_at: datetime
    role_name: str | None = None
    shift_name: str | None = None
    shift_department_name: str | None = None
    shift_start_time: str | None = None
    shift_end_time: str | None = None

    model_config = {"from_attributes": True}


class PermissionOut(BaseModel):
    module_key: str
    can_view: bool
    can_edit: bool
