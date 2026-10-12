"""
Tenancy / Hospitals API Router.

Conforms to UltrionTech-Backend-Template modules/tenancy/api/ specification.
Provides complete target-native endpoints for hospital tenancy management and dashboards.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings

from infrastructure.postgres.session import get_transitional_sync_session
from modules.tenancy.actions.tenancy_actions import TenancyActions
from modules.tenancy.contracts.callback_contracts import (
    CallbackRequestCreate,
    CallbackRequestResponse,
    CallbackRequestUpdate,
)
from modules.tenancy.contracts.registration_contracts import (
    HospitalRegistrationCreate,
    HospitalRegistrationDetail,
    HospitalRegistrationResponse,
    RegistrationApprovePayload,
    RegistrationApproveResponse,
    RegistrationPaginatedResponse,
    RegistrationRejectPayload,
)
from modules.tenancy.contracts.tenancy_contracts import (
    HospitalArchivePayload,
    HospitalCreate,
    HospitalCreateResponse,
    HospitalDashboardResponse,
    HospitalDashboardSummaryResponse,
    HospitalResponse,
    HospitalSuspendPayload,
    HospitalUpdate,
    RoleDashboardResponse,
)


from modules.tenancy.entities.hospital_registration_request import RegistrationStatus
from shared.auth import (
    get_hospital_context,
    require_hospital_user,
    require_super_admin,
)

router = APIRouter(prefix="/hospitals", tags=["hospitals"])
callback_router = APIRouter(prefix="/callback-requests", tags=["callback"])


@router.post("/callback-requests", response_model=CallbackRequestResponse, status_code=status.HTTP_201_CREATED)
@callback_router.post("", response_model=CallbackRequestResponse, status_code=status.HTTP_201_CREATED)
def submit_callback_request(
    payload: CallbackRequestCreate,
    db: Session = Depends(get_transitional_sync_session),
) -> CallbackRequestResponse:
    """Public unauthenticated endpoint to submit a callback request."""
    return TenancyActions(db).submit_callback_request(payload)


@callback_router.get("", response_model=list[CallbackRequestResponse])
def list_callback_requests(
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
) -> list[CallbackRequestResponse]:
    """Super Admin queue for reviewing callback inquiries."""
    return TenancyActions(db).list_callback_requests(status_filter=status, page=page, page_size=page_size)


@callback_router.patch("/{request_id}", response_model=CallbackRequestResponse)
def update_callback_request(
    request_id: str,
    payload: CallbackRequestUpdate,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
) -> CallbackRequestResponse:
    """Super Admin action to update callback inquiry status and notes."""
    return TenancyActions(db).update_callback_request(request_id, payload)



@router.post("/registration-requests", response_model=HospitalRegistrationResponse, status_code=status.HTTP_201_CREATED)
def submit_registration_request(
    payload: HospitalRegistrationCreate,
    db: Session = Depends(get_transitional_sync_session),
) -> HospitalRegistrationResponse:
    """Public unauthenticated endpoint to submit a hospital registration request."""
    return TenancyActions(db).submit_registration_request(payload)



@router.get("/registration-requests", response_model=RegistrationPaginatedResponse)
def list_registration_requests(
    status: RegistrationStatus | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
) -> RegistrationPaginatedResponse:
    """Super Admin queue for reviewing submitted hospital registration requests."""
    return TenancyActions(db).list_registration_requests(
        status_filter=status, search=search, page=page, page_size=page_size
    )


@router.get("/registration-requests/{request_id}", response_model=HospitalRegistrationDetail)
def get_registration_request(
    request_id: str,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
) -> HospitalRegistrationDetail:
    """Super Admin detail view of a single registration application."""
    return TenancyActions(db).get_registration_request(request_id)


@router.post("/registration-requests/{request_id}/approve", response_model=RegistrationApproveResponse)
def approve_registration_request(
    request_id: str,
    payload: RegistrationApprovePayload,
    db: Session = Depends(get_transitional_sync_session),
    admin: dict[str, Any] = Depends(require_super_admin),
) -> RegistrationApproveResponse:
    """Super Admin action to transactionally approve and provision a hospital tenant."""
    reviewer_email = str(admin.get("sub") or "superadmin@ultriontech.com")
    return TenancyActions(db).approve_registration_request(request_id, payload, reviewer_email)


@router.post("/registration-requests/{request_id}/reject", response_model=HospitalRegistrationDetail)
def reject_registration_request(
    request_id: str,
    payload: RegistrationRejectPayload,
    db: Session = Depends(get_transitional_sync_session),
    admin: dict[str, Any] = Depends(require_super_admin),
) -> HospitalRegistrationDetail:
    """Super Admin action to reject a registration application with reason."""
    reviewer_email = str(admin.get("sub") or "superadmin@ultriontech.com")
    return TenancyActions(db).reject_registration_request(request_id, payload, reviewer_email)



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
    status: str | None = Query(default=None),
    plan: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
) -> list[HospitalResponse]:
    return TenancyActions(db).list_hospitals(
        search=search,
        status_filter=status,
        plan_filter=plan,
        skip=skip,
        limit=limit,
    )



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


@router.get("/me/dashboard/summary", response_model=HospitalDashboardSummaryResponse)
def hospital_dashboard_summary(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> HospitalDashboardSummaryResponse:
    """Counters-only dashboard view for badges/widgets that don't need the
    full detail lists (~8 queries vs ~19 for the full dashboard)."""
    return TenancyActions(db).get_hospital_dashboard_summary(hospital_id=hospital_id)


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


@router.patch("/{hospital_uuid}", response_model=HospitalResponse)
def update_hospital(
    hospital_uuid: str,
    payload: HospitalUpdate,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_super_admin),
) -> HospitalResponse:
    """Super Admin action to update hospital tenant profile, plan, or branding."""
    return TenancyActions(db).update_hospital(hospital_uuid, payload)



@router.delete("/{hospital_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_hospital(
    hospital_uuid: str,
    db: Session = Depends(get_transitional_sync_session),
    settings: Settings = Depends(get_settings),
    _: dict[str, Any] = Depends(require_super_admin),
):
    if settings.environment.lower() == "production" and not getattr(settings, "allow_hospital_hard_delete", False):
        raise HTTPException(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            detail="Hard deletion of hospital tenants is disabled in production. Use Suspend or Archive instead.",
        )
    TenancyActions(db).delete_hospital(hospital_uuid)


@router.post("/{hospital_uuid}/suspend", response_model=HospitalResponse)
def suspend_hospital(
    hospital_uuid: str,
    payload: HospitalSuspendPayload,
    db: Session = Depends(get_transitional_sync_session),
    admin: dict[str, Any] = Depends(require_super_admin),
) -> HospitalResponse:
    """Super Admin action to suspend an operational hospital tenant."""
    actor_email = str(admin.get("sub") or "superadmin@ultriontech.com")
    return TenancyActions(db).suspend_hospital(hospital_uuid, payload, actor_email)


@router.post("/{hospital_uuid}/reactivate", response_model=HospitalResponse)
def reactivate_hospital(
    hospital_uuid: str,
    db: Session = Depends(get_transitional_sync_session),
    admin: dict[str, Any] = Depends(require_super_admin),
) -> HospitalResponse:
    """Super Admin action to reactivate a suspended hospital tenant."""
    actor_email = str(admin.get("sub") or "superadmin@ultriontech.com")
    return TenancyActions(db).reactivate_hospital(hospital_uuid, actor_email)


@router.post("/{hospital_uuid}/archive", response_model=HospitalResponse)
def archive_hospital(
    hospital_uuid: str,
    payload: HospitalArchivePayload,
    db: Session = Depends(get_transitional_sync_session),
    admin: dict[str, Any] = Depends(require_super_admin),
) -> HospitalResponse:
    """Super Admin action to archive a decommissioned hospital tenant."""
    actor_email = str(admin.get("sub") or "superadmin@ultriontech.com")
    return TenancyActions(db).archive_hospital(hospital_uuid, payload, actor_email)

