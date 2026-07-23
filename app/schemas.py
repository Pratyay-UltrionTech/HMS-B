from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.utils.phone import PhoneNumber

from app.models import PlanType


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    hospital_id: str | None = None
    name: str | None = None
    plan: PlanType | None = None
    staff_role_name: str | None = None
    permissions: list[dict] | None = None
    user_id: str | None = None


class HospitalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    address: str = Field(min_length=1)
    phone: PhoneNumber
    email: EmailStr
    plan: PlanType = PlanType.basic
    icon_url: str | None = None


class HospitalResponse(BaseModel):
    id: UUID
    hospital_id: str
    name: str
    address: str
    phone: str
    email: EmailStr
    plan: PlanType
    icon_url: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class HospitalCreateResponse(HospitalResponse):
    generated_password: str


class HospitalDashboardListItem(BaseModel):
    id: str
    title: str
    subtitle: str | None = None
    meta: str | None = None
    status: str | None = None
    time: str | None = None


class HospitalDashboardResponse(BaseModel):
    id: UUID
    hospital_id: str
    name: str
    address: str
    phone: str
    email: EmailStr
    plan: PlanType
    icon_url: str | None
    is_active: bool
    created_at: datetime
    staff_count: int
    doctor_count: int
    patient_count: int
    appointments_today: int
    active_admissions: int
    beds_total: int
    beds_occupied: int
    modules_available: int
    # Operations
    patients_registered_today: int = 0
    occupied_beds_pct: int = 0
    # Clinical
    lab_orders_today: int = 0
    radiology_orders_today: int = 0
    ot_surgeries_today: int = 0
    # Financial (whole rupees for KPI cards)
    charges_today: int = 0
    collections_today: int = 0
    outstanding_total: int = 0
    # Lists
    recent_registrations: list[HospitalDashboardListItem] = []
    upcoming_appointments: list[HospitalDashboardListItem] = []
    pending_lab_orders: list[HospitalDashboardListItem] = []
    pending_radiology_reports: list[HospitalDashboardListItem] = []


class RoleDashboardMetric(BaseModel):
    key: str
    label: str
    value: int
    sub: str | None = None


class RoleDashboardListItem(BaseModel):
    id: str
    title: str
    subtitle: str | None = None
    meta: str | None = None
    status: str | None = None
    time: str | None = None


class DoctorRecentRevenueItem(BaseModel):
    patient_name: str
    appointment_date: str | None = None
    amount: float
    status: str


class RoleDashboardResponse(BaseModel):
    persona: str  # doctor | nurse | reception | lab | radiology | ot | billing | admin | staff
    display_name: str
    staff_role_name: str | None = None
    metrics: list[RoleDashboardMetric] = []
    today_items: list[RoleDashboardListItem] = []
    upcoming_items: list[RoleDashboardListItem] = []
    recent_items: list[RoleDashboardListItem] = []
    activity_items: list[RoleDashboardListItem] = []
    quick_actions: list[dict] = []
    # Doctor Practice Performance (doctor persona only; others omit / default)
    today_revenue: float | None = None
    month_revenue: float | None = None
    patients_this_month: int | None = None
    average_revenue_per_patient: float | None = None
    recent_revenue: list[DoctorRecentRevenueItem] | None = None
