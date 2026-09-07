"""
Pydantic contracts and response schemas for the Analytics module.

Conforms to UltrionTech-Backend-Template modules/analytics/contracts/ specification.
Preserves 100% field, typing, and serialization fidelity with legacy schemas_analytics.
"""

from datetime import datetime

from pydantic import BaseModel


class MonthCount(BaseModel):
    """Historical monthly metric count."""

    month: str  # YYYY-MM
    label: str  # Mon YYYY
    count: int


class PlanCount(BaseModel):
    """Distribution count for a hospital subscription tier."""

    plan: str
    label: str
    count: int


class RecentHospitalRow(BaseModel):
    """Summary record of a recently created hospital."""

    id: str
    hospital_id: str
    name: str
    plan: str
    is_active: bool
    created_at: datetime


class PlatformAnalyticsResponse(BaseModel):
    """Aggregated platform performance and adoption analytics."""

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
