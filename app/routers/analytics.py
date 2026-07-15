from calendar import month_abbr
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Hospital, HospitalUser, PlanType, RolePermission
from app.schemas_admin import BASIC_MODULE_KEYS
from app.schemas_analytics import (
    MonthAmount,
    MonthCount,
    PlanCount,
    PlatformAnalyticsResponse,
    RecentHospitalRow,
)
from app.utils.auth import require_super_admin

router = APIRouter(prefix="/analytics", tags=["analytics"])

# Estimated subscription prices (INR / month) until billing is wired.
PLAN_MONTHLY_INR: dict[PlanType, float] = {
    PlanType.basic: 9_999,
    PlanType.premium: 24_999,
    PlanType.platinum: 49_999,
}

PLAN_LABELS = {
    PlanType.basic: "Basic",
    PlanType.premium: "Premium",
    PlanType.platinum: "Platinum",
}

MODULES_PER_PLAN: dict[PlanType, int] = {
    PlanType.basic: len(BASIC_MODULE_KEYS),
    PlanType.premium: len(BASIC_MODULE_KEYS),
    PlanType.platinum: len(BASIC_MODULE_KEYS),
}


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _month_label(year: int, month: int) -> str:
    return f"{month_abbr[month]} {year}"


@router.get("/platform", response_model=PlatformAnalyticsResponse)
def platform_analytics(
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin),
):
    now = datetime.now(timezone.utc)
    hospitals = db.query(Hospital).all()

    total_hospitals = len(hospitals)
    active_hospitals = sum(1 for h in hospitals if h.is_active)

    plan_counts: dict[PlanType, int] = {p: 0 for p in PlanType}
    for h in hospitals:
        if h.is_active:
            plan_counts[h.plan] = plan_counts.get(h.plan, 0) + 1

    monthly_revenue = sum(plan_counts[plan] * PLAN_MONTHLY_INR[plan] for plan in PlanType)
    active_modules = sum(plan_counts[plan] * MODULES_PER_PLAN[plan] for plan in PlanType)

    distinct_perm_modules = (
        db.query(func.count(func.distinct(RolePermission.module_key)))
        .filter(RolePermission.can_view.is_(True))
        .scalar()
        or 0
    )
    if int(distinct_perm_modules) > active_modules:
        active_modules = int(distinct_perm_modules)

    staff_users = (
        db.query(func.count(HospitalUser.id))
        .filter(HospitalUser.is_active.is_(True))
        .scalar()
        or 0
    )
    platform_users = int(staff_users) + active_hospitals

    growth: list[MonthCount] = []
    revenue_trend: list[MonthAmount] = []
    for i in range(5, -1, -1):
        year, month = _add_months(now.year, now.month, -i)
        cursor = _month_start(year, month)
        ny, nm = _add_months(year, month, 1)
        next_month = _month_start(ny, nm)
        label = _month_label(year, month)
        key = f"{year:04d}-{month:02d}"

        created = sum(
            1
            for h in hospitals
            if h.created_at
            and h.created_at.astimezone(timezone.utc) >= cursor
            and h.created_at.astimezone(timezone.utc) < next_month
        )
        growth.append(MonthCount(month=key, label=label, count=created))

        month_rev = 0.0
        for h in hospitals:
            if not h.is_active or not h.created_at:
                continue
            if h.created_at.astimezone(timezone.utc) < next_month:
                month_rev += PLAN_MONTHLY_INR.get(h.plan, 0.0)
        revenue_trend.append(MonthAmount(month=key, label=label, amount=month_rev))

    plan_distribution = [
        PlanCount(
            plan=plan.value,
            label=PLAN_LABELS[plan],
            count=plan_counts[plan],
            monthly_revenue=plan_counts[plan] * PLAN_MONTHLY_INR[plan],
        )
        for plan in PlanType
    ]

    recent = db.query(Hospital).order_by(Hospital.created_at.desc()).limit(8).all()
    recent_hospitals = [
        RecentHospitalRow(
            id=str(h.id),
            hospital_id=h.hospital_id,
            name=h.name,
            plan=h.plan.value if isinstance(h.plan, PlanType) else str(h.plan),
            is_active=h.is_active,
            created_at=h.created_at,
        )
        for h in recent
    ]

    return PlatformAnalyticsResponse(
        total_hospitals=total_hospitals,
        active_hospitals=active_hospitals,
        active_modules=active_modules,
        monthly_revenue=monthly_revenue,
        platform_users=platform_users,
        hospital_growth=growth,
        revenue_trend=revenue_trend,
        plan_distribution=plan_distribution,
        recent_hospitals=recent_hospitals,
        generated_at=now,
    )
