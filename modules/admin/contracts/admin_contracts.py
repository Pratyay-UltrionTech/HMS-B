"""
Target-native Pydantic contracts for Admin domain.

100% route contract parity with legacy schemas_admin.py.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from modules.doctors.entities.doctor import CustomFieldType
from shared.validators.phone import PhoneNumber

BASIC_MODULE_KEYS = [
    "masters",
    "admin",
    "doctors",
    "nurses",
    "registration",
    "appointment",
    "vitals",
    "bed",
    "all_ipd",
    "laboratory",
    "radiology",
    "ot",
    "dms",
    "equipment",
    "mis",
    "billing",
    "pharmacy",
    "emergency",
    "critical_care",
    "ambulance",
    "blood_bank",
    "cssd",
    "inventory",
    "procurement",
    "clinical_decision",
    "insurance",
]

BASIC_MODULE_LABELS = {
    "masters": "Masters Management",
    "admin": "Admin Management",
    "doctors": "Doctors Management",
    "nurses": "Nurses",
    "registration": "Registration",
    "appointment": "Appointment",
    "vitals": "Vitals",
    "bed": "Bed Management",
    "all_ipd": "All IPD",
    "laboratory": "Laboratory",
    "radiology": "Radiology",
    "ot": "Operation Theatre",
    "dms": "Document Management",
    "equipment": "Equipment Management",
    "mis": "MIS Reports",
    "billing": "Billing",
    "pharmacy": "Pharmacy",
    "emergency": "Emergency Department",
    "critical_care": "Critical Care / ICU",
    "ambulance": "Ambulance Services",
    "blood_bank": "Blood Bank",
    "cssd": "CSSD (Sterilization)",
    "inventory": "Hospital Inventory",
    "procurement": "Procurement & Purchase",
    "clinical_decision": "Clinical Decision Support",
    "insurance": "Insurance & TPA",
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
    can_create: bool = False
    can_delete: bool = False
    can_approve: bool = False
    can_validate: bool = False
    can_release: bool = False
    can_dispense: bool = False
    can_refund: bool = False
    can_cancel: bool = False
    can_administer: bool = False


class RolePermissionResponse(BaseModel):
    id: UUID
    module_key: str
    can_view: bool
    can_edit: bool
    can_create: bool = False
    can_delete: bool = False
    can_approve: bool = False
    can_validate: bool = False
    can_release: bool = False
    can_dispense: bool = False
    can_refund: bool = False
    can_cancel: bool = False
    can_administer: bool = False

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


# ── Hospital user assignment sub-models ────────────────────────────────────────
class UserRoleAssignmentResponse(BaseModel):
    role_id: UUID
    role_name: str
    is_primary: bool = False
    assigned_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserDepartmentAssignmentResponse(BaseModel):
    department_id: UUID
    department_name: str
    is_primary: bool = False
    can_supervise: bool = False

    model_config = {"from_attributes": True}


class UserLocationAssignmentResponse(BaseModel):
    ward_id: UUID | None = None
    ward_name: str | None = None
    wing_id: UUID | None = None
    wing_name: str | None = None

    model_config = {"from_attributes": True}


# ── Hospital users ─────────────────────────────────────────────────────────────
class HospitalUserCreate(BaseModel):
    role_id: UUID
    name: str = Field(min_length=1, max_length=255)
    phone: PhoneNumber
    email: EmailStr
    password: str = Field(min_length=4, max_length=128)
    role_ids: list[UUID] | None = None
    department_id: UUID | None = None
    department_ids: list[UUID] | None = None
    ward_ids: list[UUID] | None = None
    shift_id: UUID | None = None
    specialization: str | None = Field(default=None, max_length=255)
    medical_registration_number: str | None = Field(default=None, max_length=64)
    qualification: str | None = Field(default=None, max_length=255)
    years_of_experience: int | None = Field(default=None, ge=0, le=80)
    consultation_room: str | None = Field(default=None, max_length=128)
    show_financial_details: bool = True
    custom_values: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class HospitalUserUpdate(BaseModel):
    role_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: PhoneNumber | None = None
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=4, max_length=128)
    role_ids: list[UUID] | None = None
    department_id: UUID | None = None
    department_ids: list[UUID] | None = None
    ward_ids: list[UUID] | None = None
    shift_id: UUID | None = None
    specialization: str | None = Field(default=None, max_length=255)
    medical_registration_number: str | None = Field(default=None, max_length=64)
    qualification: str | None = Field(default=None, max_length=255)
    years_of_experience: int | None = Field(default=None, ge=0, le=80)
    consultation_room: str | None = Field(default=None, max_length=128)
    show_financial_details: bool | None = None
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
    specialization: str | None = None
    medical_registration_number: str | None = None
    qualification: str | None = None
    years_of_experience: int | None = None
    consultation_room: str | None = None
    show_financial_details: bool = True
    custom_values: dict[str, Any]
    is_active: bool
    created_at: datetime
    role_name: str | None = None
    shift_name: str | None = None
    shift_department_id: UUID | None = None
    shift_department_name: str | None = None
    shift_start_time: str | None = None
    shift_end_time: str | None = None
    department_id: UUID | None = None
    department_name: str | None = None
    roles: list[UserRoleAssignmentResponse] = Field(default_factory=list)
    departments: list[UserDepartmentAssignmentResponse] = Field(default_factory=list)
    locations: list[UserLocationAssignmentResponse] = Field(default_factory=list)
    effective_permissions: list[dict] = Field(default_factory=list)

    model_config = {"from_attributes": True}

    model_config = {"from_attributes": True}


class HospitalUserListItem(BaseModel):
    id: UUID
    hospital_id: UUID
    role_id: UUID
    name: str
    phone: str
    email: str
    role_name: str | None = None
    department_id: UUID | None = None
    department_name: str | None = None
    shift_id: UUID | None = None
    shift_name: str | None = None
    is_active: bool
    created_at: datetime
    roles: list[UserRoleAssignmentResponse] = Field(default_factory=list)
    departments: list[UserDepartmentAssignmentResponse] = Field(default_factory=list)


class HospitalUserPage(BaseModel):
    items: list[HospitalUserListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class StaffRosterSegmentItem(BaseModel):
    id: UUID
    roster_day_id: UUID | None = None
    shift_type_id: UUID | None = None
    shift_name: str | None = None
    start_at: datetime
    end_at: datetime
    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"
    duty_kind: str = "OTHER"
    presence_type: str = "ONSITE"
    is_bookable: bool = False
    department_id: UUID | None = None
    department_name: str | None = None
    notes: str | None = None
    is_cross_midnight: bool = False


# ── Daily shift roster ──────────────────────────────────────────────────────────
class ShiftRosterEntry(BaseModel):
    user_id: UUID
    name: str
    phone: str
    role_id: UUID
    role_name: str | None = None
    shift_id: UUID | None = None
    shift_name: str | None = None
    department_id: UUID | None = None
    department_name: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    status: str  # on_duty | off | leave
    is_override: bool = False
    notes: str | None = None
    default_shift_id: UUID | None = None
    eligible_department_ids: list[UUID] = Field(default_factory=list)
    primary_department_id: UUID | None = None
    primary_department_name: str | None = None
    segments: list[StaffRosterSegmentItem] = Field(default_factory=list)


class ShiftRosterResponse(BaseModel):
    roster_date: date
    hospital_name: str
    holiday_name: str | None = None
    entries: list[ShiftRosterEntry]
    unique_staff_working: int = 0
    total_duty_segments: int = 0
    total_staff: int = 0
    off_count: int = 0
    leave_count: int = 0


class ShiftRosterAssign(BaseModel):
    roster_date: date
    user_id: UUID
    shift_id: UUID | None = None
    status: str = Field(default="on_duty", pattern="^(on_duty|off|leave|working)$")
    notes: str | None = Field(default=None, max_length=500)


class RosterDutySegmentCreate(BaseModel):
    roster_date: date
    user_id: UUID
    shift_type_id: UUID | None = None
    start_time: str = Field(pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    end_time: str = Field(pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    duty_kind: str = Field(default="OTHER", pattern="^(OPD|WARD|ICU|EMERGENCY|OT|LAB|RADIOLOGY|ON_CALL|ADMIN|TRAINING|OTHER)$")
    presence_type: str = Field(default="ONSITE", pattern="^(ONSITE|ON_CALL|REMOTE)$")
    department_id: UUID | None = None
    is_bookable: bool = False
    notes: str | None = None


# ── Staff Recurring Work Pattern ──────────────────────────────────────────────
class WorkPatternSegmentItem(BaseModel):
    id: UUID | None = None
    day_of_week: int = Field(ge=0, le=6)  # 0=Monday .. 6=Sunday
    start_time: str = Field(pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    end_time: str = Field(pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    shift_type_id: UUID | None = None
    shift_name: str | None = None
    department_id: UUID | None = None
    department_name: str | None = None
    duty_kind: str = Field(default="OTHER", pattern="^(OPD|WARD|ICU|EMERGENCY|OT|LAB|RADIOLOGY|ON_CALL|ADMIN|TRAINING|OTHER)$")
    presence_type: str = Field(default="ONSITE", pattern="^(ONSITE|ON_CALL|REMOTE)$")
    is_bookable: bool = False
    notes: str | None = None


class StaffWorkPatternResponse(BaseModel):
    user_id: UUID
    pattern_id: UUID | None = None
    name: str = "Default Recurring Schedule"
    is_active: bool = True
    notes: str | None = None
    segments: list[WorkPatternSegmentItem] = Field(default_factory=list)


class StaffWorkPatternUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    notes: str | None = None
    segments: list[WorkPatternSegmentItem] = Field(default_factory=list)


class WorkPatternSegmentCreate(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time: str = Field(pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    end_time: str = Field(pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    shift_type_id: UUID | None = None
    department_id: UUID | None = None
    duty_kind: str = Field(default="OTHER", pattern="^(OPD|WARD|ICU|EMERGENCY|OT|LAB|RADIOLOGY|ON_CALL|ADMIN|TRAINING|OTHER)$")
    presence_type: str = Field(default="ONSITE", pattern="^(ONSITE|ON_CALL|REMOTE)$")
    is_bookable: bool = False
    notes: str | None = None


class WorkPatternSegmentUpdate(BaseModel):
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    start_time: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    end_time: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    shift_type_id: UUID | None = None
    department_id: UUID | None = None
    duty_kind: str | None = Field(default=None, pattern="^(OPD|WARD|ICU|EMERGENCY|OT|LAB|RADIOLOGY|ON_CALL|ADMIN|TRAINING|OTHER)$")
    presence_type: str | None = Field(default=None, pattern="^(ONSITE|ON_CALL|REMOTE)$")
    is_bookable: bool | None = None
    notes: str | None = None


class RosterDutySegmentUpdate(BaseModel):
    shift_type_id: UUID | None = None
    start_time: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    end_time: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")
    duty_kind: str | None = Field(default=None, pattern="^(OPD|WARD|ICU|EMERGENCY|OT|LAB|RADIOLOGY|ON_CALL|ADMIN|TRAINING|OTHER)$")
    presence_type: str | None = Field(default=None, pattern="^(ONSITE|ON_CALL|REMOTE)$")
    department_id: UUID | None = None
    is_bookable: bool | None = None
    notes: str | None = None


class StaffRosterDayUpdate(BaseModel):
    roster_date: date
    user_id: UUID
    status: str = Field(default="working", pattern="^(working|on_duty|off|leave)$")
    notes: str | None = None
    segments: list[RosterDutySegmentCreate] | None = None


class ShiftRosterSeed(BaseModel):
    roster_date: date
    overwrite: bool = False



class ShiftRosterSnapshot(BaseModel):
    roster_date: date
    hospital_name: str
    holiday_name: str | None = None
    text: str
    entries_count: int


# ── Audit logs ─────────────────────────────────────────────────────────────────
class AuditLogResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    actor_email: str
    actor_name: str
    actor_role: str
    actor_role_label: str | None
    action: str
    entity_type: str
    entity_id: str | None
    summary: str
    details: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Hospital Facility Settings (SCR-022) ───────────────────────────────────────
class HospitalFacilitySettings(BaseModel):
    hospital_id: str
    legal_name: str
    display_name: str
    institutional_code: str
    address: str
    phone: str
    email: str
    cea_registration: str = "CEA/DL/2024/0981"
    nabh_accreditation_ref: str = "NABH-2024-0418"
    nabh_status: str = "Full Accreditation Valid"
    tax_gstin: str = "07AAACH1234F1Z5"
    rohini_id: str = "890000124578"
    aerb_license_no: str = "AERB/MED/DL/0412"
    pharmacy_license_no: str = "DL-20B/21B-45892"
    prescription_header_text: str = "CITY GENERAL HOSPITAL & MEDICAL RESEARCH CENTRE"
    prescription_footer_text: str = "Emergency Hotline: 1066 / 011-26598700 • Email: emergency@hospital.org"
    abdm_facility_id: str = "IN0710000412"
    abdm_m1_active: bool = True
    abdm_m2_active: bool = True
    abdm_m3_active: bool = True
    sms_gateway_active: bool = True
    whatsapp_gateway_active: bool = True
    dlt_sender_id: str = "CGHHSP"


class HospitalFacilitySettingsUpdate(BaseModel):
    legal_name: str | None = None
    display_name: str | None = None
    institutional_code: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    cea_registration: str | None = None
    nabh_accreditation_ref: str | None = None
    nabh_status: str | None = None
    tax_gstin: str | None = None
    rohini_id: str | None = None
    aerb_license_no: str | None = None
    pharmacy_license_no: str | None = None
    prescription_header_text: str | None = None
    prescription_footer_text: str | None = None
    abdm_facility_id: str | None = None
    abdm_m1_active: bool | None = None
    abdm_m2_active: bool | None = None
    abdm_m3_active: bool | None = None
    sms_gateway_active: bool | None = None
    whatsapp_gateway_active: bool | None = None
    dlt_sender_id: str | None = None


class AuthSimulateRequest(BaseModel):
    user_id: str
    action: str
    department_id: str | None = None
    ward_id: str | None = None
    resource_hospital_id: str | None = None
    resource_creator_id: str | None = None


class AuthSimulateResponse(BaseModel):
    decision: str
    details: dict[str, Any]


class WorkPatternSegmentItem(BaseModel):
    id: UUID | None = None
    day_of_week: int  # 0=Monday .. 6=Sunday
    shift_type_id: UUID | None = None
    shift_name: str | None = None
    department_id: UUID | None = None
    department_name: str | None = None
    start_time: str
    end_time: str
    duty_kind: str = "OTHER"
    presence_type: str = "ONSITE"
    is_bookable: bool = False
    notes: str | None = None


class StaffWorkPatternResponse(BaseModel):
    id: UUID | None = None
    pattern_id: UUID | None = None
    hospital_id: UUID | None = None
    user_id: UUID
    name: str = "Default Recurring Schedule"
    is_active: bool = True
    notes: str | None = None
    segments: list[WorkPatternSegmentItem] = Field(default_factory=list)


class StaffWorkPatternUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = True
    notes: str | None = None
    segments: list[WorkPatternSegmentItem] = Field(default_factory=list)


class WorkPatternSegmentCreate(BaseModel):
    user_id: UUID
    day_of_week: int
    start_time: str
    end_time: str
    shift_type_id: UUID | None = None
    department_id: UUID | None = None
    duty_kind: str = "OTHER"
    presence_type: str = "ONSITE"
    is_bookable: bool = False
    notes: str | None = None

