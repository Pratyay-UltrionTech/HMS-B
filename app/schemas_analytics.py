from datetime import datetime

from pydantic import BaseModel


class MonthCount(BaseModel):
    month: str  # YYYY-MM
    label: str
    count: int


class PlanCount(BaseModel):
    plan: str
    label: str
    count: int


class RecentHospitalRow(BaseModel):
    id: str
    hospital_id: str
    name: str
    plan: str
    is_active: bool
    created_at: datetime


class PlatformAnalyticsResponse(BaseModel):
    total_hospitals: int
    active_hospitals: int
    active_modules: int
    total_patients: int
    platform_users: int
    hospital_growth: list[MonthCount]
    patient_growth: list[MonthCount]
    plan_distribution: list[PlanCount]
    recent_hospitals: list[RecentHospitalRow]
    generated_at: datetime
