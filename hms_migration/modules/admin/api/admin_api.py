"""
Target-native Admin API endpoints.

Prefix: /admin
Tags: ["admin"]
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.admin.actions.admin_actions import AdminActions
from hms_migration.modules.admin.contracts.admin_contracts import (
    AuditLogResponse,
    HospitalUserCreate,
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
    HospitalFacilitySettings,
    HospitalFacilitySettingsUpdate,
)
from hms_migration.shared.auth import (
    get_hospital_context,
    require_hospital_admin,
    require_hospital_user,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Modules ────────────────────────────────────────────────────────────────────
@router.get("/modules", response_model=list[ModuleInfo])
def list_modules(
    _: dict[str, Any] = Depends(require_hospital_user),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[ModuleInfo]:
    return AdminActions(db, hospital_id).list_modules()


# ── Roles ──────────────────────────────────────────────────────────────────────
@router.get("/roles", response_model=list[RoleResponse])
def list_roles(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[RoleResponse]:
    return AdminActions(db, hospital_id).list_roles()


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> RoleResponse:
    return AdminActions(db, hospital_id, actor).create_role(payload)


@router.put("/roles/{role_id}", response_model=RoleResponse)
def update_role(
    role_id: UUID,
    payload: RoleUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> RoleResponse:
    return AdminActions(db, hospital_id, actor).update_role(role_id, payload)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_role(
    role_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> Response:
    AdminActions(db, hospital_id, actor).delete_role(role_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Users ──────────────────────────────────────────────────────────────────────
@router.get("/users", response_model=list[HospitalUserResponse])
def list_users(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[HospitalUserResponse]:
    return AdminActions(db, hospital_id).list_users()


@router.post("/users", response_model=HospitalUserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: HospitalUserCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> HospitalUserResponse:
    return AdminActions(db, hospital_id, actor).create_user(payload)


@router.put("/users/{user_id}", response_model=HospitalUserResponse)
def update_user(
    user_id: UUID,
    payload: HospitalUserUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> HospitalUserResponse:
    return AdminActions(db, hospital_id, actor).update_user(user_id, payload)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
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


@router.put("/shift-roster", response_model=ShiftRosterEntry)
def assign_shift_roster(
    payload: ShiftRosterAssign,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> ShiftRosterEntry:
    return AdminActions(db, hospital_id, actor).assign_shift_roster(payload)


@router.post("/shift-roster/seed", response_model=ShiftRosterResponse)
def seed_shift_roster(
    payload: ShiftRosterSeed,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> ShiftRosterResponse:
    return AdminActions(db, hospital_id, actor).seed_shift_roster(payload)


@router.delete("/shift-roster", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
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


# ── Hospital Facility Settings (SCR-022) ───────────────────────────────────────
@router.get("/facility-settings", response_model=HospitalFacilitySettings)
def get_facility_settings(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> HospitalFacilitySettings:
    return AdminActions(db, hospital_id).get_facility_settings()


@router.put("/facility-settings", response_model=HospitalFacilitySettings)
def update_facility_settings(
    payload: HospitalFacilitySettingsUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> HospitalFacilitySettings:
    return AdminActions(db, hospital_id, actor).update_facility_settings(payload)

