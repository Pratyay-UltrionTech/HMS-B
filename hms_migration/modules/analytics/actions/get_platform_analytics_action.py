"""
Business action to aggregate and construct platform analytics.

Conforms to UltrionTech-Backend-Template modules/analytics/actions/ specification.
Orchestrates metrics aggregation, plan distribution, 6-month historical growth,
and active modules calculations with 100% behavioral equivalence to legacy analytics.
"""

from calendar import month_abbr
from datetime import datetime, timezone
from typing import Any

from hms_migration.modules.analytics.contracts.analytics_contracts import (
    MonthCount,
    PlanCount,
    PlatformAnalyticsResponse,
    RecentHospitalRow,
)
from hms_migration.modules.analytics.db.analytics_repository import AnalyticsRepository

PLAN_SPECS = [
    ("basic", "Basic"),
    ("premium", "Premium"),
    ("platinum", "Platinum"),
]

# Standard suite of core hospital management modules (17 domain keys)
MODULES_PER_PLAN_COUNT = 17


def _month_start(year: int, month: int) -> datetime:
    """Return the UTC start datetime for a given calendar month."""
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Add or subtract integer months and return (year, month)."""
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _month_label(year: int, month: int) -> str:
    """Format short month and full year (e.g. 'Sep 2026')."""
    return f"{month_abbr[month]} {year}"


class GetPlatformAnalyticsAction:
    """Action that compiles platform-wide KPI metrics and trend series."""

    def __init__(self, repo: AnalyticsRepository) -> None:
        self.repo = repo

    def execute(self) -> PlatformAnalyticsResponse:
        """Execute calculations and return a validated PlatformAnalyticsResponse."""
        now = datetime.now(timezone.utc)
        hospitals = self.repo.get_all_hospitals()

        total_hospitals = len(hospitals)
        active_hospitals = sum(1 for h in hospitals if h.get("is_active"))

        # 1. Plan Distribution for active hospitals
        plan_counts: dict[str, int] = {p[0]: 0 for p in PLAN_SPECS}
        for h in hospitals:
            if h.get("is_active"):
                plan_val = str(h.get("plan") or "basic").lower()
                if hasattr(h.get("plan"), "value"):
                    plan_val = str(h["plan"].value).lower()
                if plan_val in plan_counts:
                    plan_counts[plan_val] += 1
                else:
                    plan_counts["basic"] += 1

        # 2. Active Modules Calculation
        active_modules = sum(plan_counts[plan] * MODULES_PER_PLAN_COUNT for plan, _ in PLAN_SPECS)
        distinct_perm_modules = self.repo.get_distinct_permission_modules_count()
        if distinct_perm_modules > active_modules:
            active_modules = distinct_perm_modules

        # 3. Platform Users Count
        staff_users = self.repo.get_active_staff_users_count()
        platform_users = staff_users + active_hospitals

        # 4. Total Patients
        total_patients = self.repo.get_total_patients_count()
        patient_records = self.repo.get_patient_creation_timestamps()

        # 5. Six-Month Rolling Growth
        growth: list[MonthCount] = []
        patient_growth: list[MonthCount] = []
        for i in range(5, -1, -1):
            year, month = _add_months(now.year, now.month, -i)
            cursor = _month_start(year, month)
            ny, nm = _add_months(year, month, 1)
            next_month = _month_start(ny, nm)
            label = _month_label(year, month)
            key = f"{year:04d}-{month:02d}"

            # Filter hospitals created in this calendar month window
            created = 0
            for h in hospitals:
                created_at = h.get("created_at")
                if created_at:
                    dt = created_at.astimezone(timezone.utc) if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
                    if cursor <= dt < next_month:
                        created += 1
            growth.append(MonthCount(month=key, label=label, count=created))

            # Filter patients created in this calendar month window
            registered = 0
            for _pid, created_at in patient_records:
                if created_at:
                    dt = created_at.astimezone(timezone.utc) if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
                    if cursor <= dt < next_month:
                        registered += 1
            patient_growth.append(MonthCount(month=key, label=label, count=registered))

        # 6. Format Plan Distribution List
        plan_distribution = [
            PlanCount(
                plan=plan,
                label=label,
                count=plan_counts[plan],
            )
            for plan, label in PLAN_SPECS
        ]

        # 7. Recent Hospitals
        recent_raw = self.repo.get_recent_hospitals(limit=8)
        recent_hospitals = [
            RecentHospitalRow(
                id=str(h["id"]),
                hospital_id=str(h.get("hospital_id") or ""),
                name=str(h.get("name") or ""),
                plan=str(getattr(h.get("plan"), "value", h.get("plan")) or "basic"),
                is_active=bool(h.get("is_active")),
                created_at=h["created_at"],
            )
            for h in recent_raw
        ]

        return PlatformAnalyticsResponse(
            total_hospitals=total_hospitals,
            active_hospitals=active_hospitals,
            active_modules=active_modules,
            total_patients=total_patients,
            platform_users=platform_users,
            hospital_growth=growth,
            patient_growth=patient_growth,
            plan_distribution=plan_distribution,
            recent_hospitals=recent_hospitals,
            generated_at=now,
        )
