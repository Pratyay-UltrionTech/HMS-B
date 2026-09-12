"""
Tenancy / Hospitals API Router.

Conforms to UltrionTech-Backend-Template modules/tenancy/api/ specification.
Provides complete target-native endpoints for hospital tenancy management and dashboards.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.tenancy.actions.tenancy_actions import TenancyActions
from hms_migration.modules.tenancy.contracts.tenancy_contracts import (
    HospitalCreate,
    HospitalCreateResponse,
    HospitalDashboardResponse,
    HospitalResponse,
    RoleDashboardResponse,
)
from hms_migration.shared.auth import (
    get_hospital_context,
    require_hospital_user,
    require_super_admin,
)

router = APIRouter(prefix="/hospitals", tags=["hospitals"])


@router.post("", response_model=HospitalCreateResponse, status_code=status.HTTP_201_CREATED)
def create_hospital(
    payload: HospitalCreate,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
) -> HospitalCreateResponse:
    return TenancyActions(db).create_hospital(payload)


@router.get("", response_model=list[HospitalResponse])
def list_hospitals(
    search: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
) -> list[HospitalResponse]:
    return TenancyActions(db).list_hospitals(search=search)


@router.get("/me/dashboard", response_model=HospitalDashboardResponse)
def hospital_dashboard(
    date_from: date | None = Query(default=None, description="Range start (defaults to today)"),
    date_to: date | None = Query(default=None, description="Range end (defaults to date_from)"),
    on_date: date | None = Query(default=None, description="Legacy single-day filter; sets both ends"),
    doctor_id: UUID | None = Query(default=None, description="Filter by doctor"),
    wing_id: UUID | None = Query(default=None, description="Filter by block/wing"),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> HospitalDashboardResponse:
    return TenancyActions(db).get_hospital_dashboard(
        hospital_id=hospital_id,
        date_from=date_from,
        date_to=date_to,
        on_date=on_date,
        doctor_id=doctor_id,
        wing_id=wing_id,
    )


@router.get("/me/role-dashboard", response_model=RoleDashboardResponse)
def role_dashboard(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    user: dict[str, Any] = Depends(require_hospital_user),
) -> RoleDashboardResponse:
    return TenancyActions(db).get_role_dashboard(hospital_id=hospital_id, user=user)


@router.get("/{hospital_uuid}", response_model=HospitalResponse)
def get_hospital(
    hospital_uuid: str,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
) -> HospitalResponse:
    return TenancyActions(db).get_hospital(hospital_uuid)


@router.delete("/{hospital_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_hospital(
    hospital_uuid: str,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
):
    TenancyActions(db).delete_hospital(hospital_uuid)
