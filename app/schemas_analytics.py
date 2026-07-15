from datetime import datetime

from pydantic import BaseModel


class MonthCount(BaseModel):
    month: str  # YYYY-MM
    label: str
    count: int


class MonthAmount(BaseModel):
    month: str
    label: str
    amount: float


class PlanCount(BaseModel):
    plan: str
    label: str
    count: int
    monthly_revenue: float


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
    monthly_revenue: float
    platform_users: int
    hospital_growth: list[MonthCount]
    revenue_trend: list[MonthAmount]
    plan_distribution: list[PlanCount]
    recent_hospitals: list[RecentHospitalRow]
    generated_at: datetime
