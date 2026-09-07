"""Analytics contracts package."""

from hms_migration.modules.analytics.contracts.analytics_contracts import (
    MonthCount,
    PlanCount,
    PlatformAnalyticsResponse,
    RecentHospitalRow,
)

__all__ = [
    "MonthCount",
    "PlanCount",
    "RecentHospitalRow",
    "PlatformAnalyticsResponse",
]
