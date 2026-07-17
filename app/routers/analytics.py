from calendar import month_abbr
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Hospital, HospitalUser, Patient, PlanType, RolePermission
from app.schemas_admin import BASIC_MODULE_KEYS
from app.schemas_analytics import (
    MonthCount,
    PlanCount,
    PlatformAnalyticsResponse,
    RecentHospitalRow,
)
from app.utils.auth import require_super_admin

router = APIRouter(prefix="/analytics", tags=["analytics"])

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

    total_patients = int(db.query(func.count(Patient.id)).scalar() or 0)
    patients = db.query(Patient.id, Patient.created_at).all()

    growth: list[MonthCount] = []
    patient_growth: list[MonthCount] = []
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

        registered = sum(
            1
            for _pid, created_at in patients
            if created_at
            and created_at.astimezone(timezone.utc) >= cursor
            and created_at.astimezone(timezone.utc) < next_month
        )
        patient_growth.append(MonthCount(month=key, label=label, count=registered))

    plan_distribution = [
        PlanCount(
            plan=plan.value,
            label=PLAN_LABELS[plan],
            count=plan_counts[plan],
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
        total_patients=total_patients,
        platform_users=platform_users,
        hospital_growth=growth,
        patient_growth=patient_growth,
        plan_distribution=plan_distribution,
        recent_hospitals=recent_hospitals,
        generated_at=now,
    )
