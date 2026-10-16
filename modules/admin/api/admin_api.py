"""
Target-native Admin API endpoints.

Prefix: /admin
Tags: ["admin"]
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from infrastructure.postgres.session import get_transitional_sync_session
from modules.admin.actions.admin_actions import AdminActions
from modules.admin.contracts.admin_contracts import (
    AuditLogResponse,
    HospitalUserCreate,
    HospitalUserPage,
    HospitalUserResponse,
    HospitalUserUpdate,
    ModuleInfo,
    RoleCreate,
    RoleResponse,
    RoleUpdate,
    ShiftRosterAssign,
    ShiftRosterEntry,
    ShiftRosterResponse,
    ShiftRosterSeed,
    ShiftRosterSnapshot,
    StaffRosterSegmentItem,
    RosterDutySegmentCreate,
    RosterDutySegmentUpdate,
    StaffRosterDayUpdate,
    HospitalFacilitySettings,
    HospitalFacilitySettingsUpdate,
    WorkPatternSegmentItem,
    StaffWorkPatternResponse,
    StaffWorkPatternUpdate,
    WorkPatternSegmentCreate,
    WorkPatternSegmentUpdate,
)
from shared.auth import (
    get_current_user,
    get_hospital_context,
    require_hospital_admin,
    require_hospital_user,
    require_permission,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Modules ────────────────────────────────────────────────────────────────────
# Module catalog is platform-static (BASIC_MODULE_KEYS) — it is not scoped to a
# hospital tenant. Hospital admins/staff need it for the permission-matrix editor
# and the super_admin Dashboard needs it for the Module Catalog page, but a
# super_admin token carries no hospital_uuid, so the hospital-scoped
# require_hospital_user / get_hospital_context chain rejects it with 403
# "Hospital access required". Accept any authenticated principal here instead.
@router.get("/modules", response_model=list[ModuleInfo])
def list_modules(
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_transitional_sync_session),
) -> list[ModuleInfo]:
    if user.get("role") not in {"super_admin", "hospital_admin", "hospital_staff"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hospital access required")
    hospital_uuid_raw = user.get("hospital_uuid")
    try:
        hospital_id = UUID(str(hospital_uuid_raw)) if hospital_uuid_raw else uuid4()
    except (ValueError, TypeError, AttributeError):
        hospital_id = uuid4()
    return AdminActions(db, hospital_id).list_modules()


# ── Roles ──────────────────────────────────────────────────────────────────────
@router.get("/roles", response_model=list[RoleResponse])
def list_roles(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_permission("admin", "view")),
) -> list[RoleResponse]:
    return AdminActions(db, hospital_id).list_roles()


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("admin", "edit"))])
def create_role(
    payload: RoleCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> RoleResponse:
    return AdminActions(db, hospital_id, actor).create_role(payload)


@router.put("/roles/{role_id}", response_model=RoleResponse, dependencies=[Depends(require_permission("admin", "edit"))])
def update_role(
    role_id: UUID,
    payload: RoleUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> RoleResponse:
    return AdminActions(db, hospital_id, actor).update_role(role_id, payload)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
    dependencies=[Depends(require_permission("admin", "edit"))])
def delete_role(
    role_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> Response:
    AdminActions(db, hospital_id, actor).delete_role(role_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Users ──────────────────────────────────────────────────────────────────────
@router.get("/users", response_model=HospitalUserPage)
def list_users(
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    search: str | None = Query(None, max_length=255), is_active: bool | None = None,
    role_id: UUID | None = None, department_id: UUID | None = None,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_permission("admin", "view")),
) -> HospitalUserPage:
    return AdminActions(db, hospital_id).list_users_page(page,page_size,search,is_active,role_id,department_id)


@router.get("/users/{user_id}", response_model=HospitalUserResponse)
def get_user(user_id: UUID, db: Session = Depends(get_transitional_sync_session),
             hospital_id: UUID = Depends(get_hospital_context),
             _: dict[str, Any] = Depends(require_permission("admin", "view"))) -> HospitalUserResponse:
    return AdminActions(db, hospital_id).get_user(user_id)


@router.post("/users", response_model=HospitalUserResponse, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("admin", "edit"))])
def create_user(
    payload: HospitalUserCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> HospitalUserResponse:
    return AdminActions(db, hospital_id, actor).create_user(payload)


@router.put("/users/{user_id}", response_model=HospitalUserResponse, dependencies=[Depends(require_permission("admin", "edit"))])
def update_user(
    user_id: UUID,
    payload: HospitalUserUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> HospitalUserResponse:
    return AdminActions(db, hospital_id, actor).update_user(user_id, payload)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
    dependencies=[Depends(require_permission("admin", "edit"))])
def delete_user(
    user_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> Response:
    AdminActions(db, hospital_id, actor).delete_user(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Audit Logs ─────────────────────────────────────────────────────────────────
@router.get("/audit-logs", response_model=list[AuditLogResponse])
def list_audit_logs(
    search: str | None = Query(default=None),
    action: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_admin),
) -> list[AuditLogResponse]:
    return AdminActions(db, hospital_id).list_audit_logs(
        search=search,
        action=action,
        entity_type=entity_type,
        limit=limit,
    )


# ── Daily Shift Roster ─────────────────────────────────────────────────────────
@router.get("/shift-roster", response_model=ShiftRosterResponse)
def get_shift_roster(
    roster_date: date = Query(..., description="YYYY-MM-DD"),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> ShiftRosterResponse:
    return AdminActions(db, hospital_id).get_shift_roster(roster_date)


@router.put("/shift-roster", response_model=ShiftRosterEntry, dependencies=[Depends(require_permission("admin", "edit"))])
def assign_shift_roster(
    payload: ShiftRosterAssign,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> ShiftRosterEntry:
    return AdminActions(db, hospital_id, actor).assign_shift_roster(payload)


@router.post("/shift-roster/seed", response_model=ShiftRosterResponse, dependencies=[Depends(require_permission("admin", "edit"))])
def seed_shift_roster(
    payload: ShiftRosterSeed,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> ShiftRosterResponse:
    return AdminActions(db, hospital_id, actor).seed_shift_roster(payload)


@router.delete("/shift-roster", status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
    dependencies=[Depends(require_permission("admin", "edit"))])
def clear_shift_roster_overrides(
    roster_date: date = Query(...),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> Response:
    AdminActions(db, hospital_id, actor).clear_shift_roster_overrides(roster_date)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/shift-roster/snapshot", response_model=ShiftRosterSnapshot)
def get_shift_roster_snapshot(
    roster_date: date = Query(...),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> ShiftRosterSnapshot:
    return AdminActions(db, hospital_id).get_shift_roster_snapshot(roster_date)


@router.post("/shift-roster/segments", response_model=StaffRosterSegmentItem, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("admin", "edit"))])
def add_duty_segment(
    payload: RosterDutySegmentCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> StaffRosterSegmentItem:
    return AdminActions(db, hospital_id, actor).add_duty_segment(payload)


@router.put("/shift-roster/segments/{segment_id}", response_model=StaffRosterSegmentItem,
    dependencies=[Depends(require_permission("admin", "edit"))])
def update_duty_segment(
    segment_id: UUID,
    payload: RosterDutySegmentUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> StaffRosterSegmentItem:
    return AdminActions(db, hospital_id, actor).update_duty_segment(segment_id, payload)


@router.delete("/shift-roster/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
    dependencies=[Depends(require_permission("admin", "edit"))])
def delete_duty_segment(
    segment_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> Response:
    AdminActions(db, hospital_id, actor).delete_duty_segment(segment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/shift-roster/day", response_model=ShiftRosterEntry,
    dependencies=[Depends(require_permission("admin", "edit"))])
def update_roster_day(
    payload: StaffRosterDayUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> ShiftRosterEntry:
    return AdminActions(db, hospital_id, actor).update_roster_day(payload)


# ── Staff Recurring Work Patterns ──────────────────────────────────────────────
@router.get("/staff/{user_id}/work-pattern", response_model=StaffWorkPatternResponse)
def get_staff_work_pattern(
    user_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> StaffWorkPatternResponse:
    return AdminActions(db, hospital_id).get_staff_work_pattern(user_id)


@router.put("/staff/{user_id}/work-pattern", response_model=StaffWorkPatternResponse,
    dependencies=[Depends(require_permission("admin", "edit"))])
def update_staff_work_pattern(
    user_id: UUID,
    payload: StaffWorkPatternUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> StaffWorkPatternResponse:
    return AdminActions(db, hospital_id, actor).update_staff_work_pattern(user_id, payload)


@router.post("/staff/{user_id}/work-pattern/segments", response_model=WorkPatternSegmentItem, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("admin", "edit"))])
def add_work_pattern_segment(
    user_id: UUID,
    payload: WorkPatternSegmentCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> WorkPatternSegmentItem:
    return AdminActions(db, hospital_id, actor).add_work_pattern_segment(user_id, payload)


@router.delete("/staff/{user_id}/work-pattern/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
    dependencies=[Depends(require_permission("admin", "edit"))])
def delete_work_pattern_segment(
    user_id: UUID,
    segment_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> Response:
    AdminActions(db, hospital_id, actor).delete_work_pattern_segment(user_id, segment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/staff/{user_id}/work-pattern/days/{day_of_week}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
    dependencies=[Depends(require_permission("admin", "edit"))])
def clear_work_pattern_day(
    user_id: UUID,
    day_of_week: int,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> Response:
    AdminActions(db, hospital_id, actor).clear_work_pattern_day(user_id, day_of_week)
    return Response(status_code=status.HTTP_204_NO_CONTENT)



# ── Hospital Facility Settings (SCR-022) ───────────────────────────────────────
@router.get("/facility-settings", response_model=HospitalFacilitySettings)
def get_facility_settings(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> HospitalFacilitySettings:
    return AdminActions(db, hospital_id).get_facility_settings()


@router.put("/facility-settings", response_model=HospitalFacilitySettings, dependencies=[Depends(require_permission("admin", "edit"))])
def update_facility_settings(
    payload: HospitalFacilitySettingsUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> HospitalFacilitySettings:
    return AdminActions(db, hospital_id, actor).update_facility_settings(payload)


# ── Authorization Simulation & Diagnostics (Admin Only) ──────────────────────
from modules.admin.contracts.admin_contracts import AuthSimulateRequest, AuthSimulateResponse
from shared.auth.service import authorization


@router.post("/authorization/simulate", response_model=AuthSimulateResponse)
def simulate_authorization(
    payload: AuthSimulateRequest,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_admin),
) -> AuthSimulateResponse:
    from modules.doctors.entities.doctor import HospitalUser
    user_row = db.query(HospitalUser).filter(
        HospitalUser.id == UUID(payload.user_id),
        HospitalUser.hospital_id == hospital_id,
    ).first()
    if not user_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    simulated_user = {
        "user_id": str(user_row.id),
        "email": user_row.email,
        "name": user_row.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_id),
    }

    class MockResource:
        pass

    mock_resource = None
    if payload.department_id or payload.ward_id or payload.resource_hospital_id or payload.resource_creator_id:
        mock_resource = MockResource()
        mock_resource.hospital_id = payload.resource_hospital_id or str(hospital_id)
        if payload.department_id:
            mock_resource.department_id = payload.department_id
        if payload.ward_id:
            mock_resource.ward_id = payload.ward_id
        if payload.resource_creator_id:
            mock_resource.created_by_user_id = payload.resource_creator_id

    result = authorization.simulate(
        user=simulated_user,
        action_spec=payload.action,
        resource=mock_resource,
        db=db,
    )
    return AuthSimulateResponse(
        decision=result["decision"],
        details=result["details"],
    )

