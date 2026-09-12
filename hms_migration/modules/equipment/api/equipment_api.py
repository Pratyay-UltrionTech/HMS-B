"""
Equipment API endpoints adhering to UltrionTech-Backend-Template and legacy route parity.

Prefix: /equipment
Tags: ["equipment"]
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.equipment.actions.equipment_actions import EquipmentActions
from hms_migration.modules.equipment.contracts.equipment_contracts import (
    AmcUpdate,
    AssignmentCreate,
    AssignmentResponse,
    EquipCategoryCreate,
    EquipCategoryResponse,
    EquipCategoryUpdate,
    EquipDashboardResponse,
    EquipmentCreate,
    EquipmentResponse,
    EquipmentUpdate,
    MaintenanceComplete,
    MaintenanceCreate,
    MaintenanceResponse,
    RequestAction,
    RequestCreate,
    RequestResponse,
    ServiceLogCreate,
    ServiceLogResponse,
)
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/equipment", tags=["equipment"])


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_model=EquipDashboardResponse)
def dashboard(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> EquipDashboardResponse:
    return EquipmentActions(db, hospital_id, user).get_dashboard()


# ── Categories ────────────────────────────────────────────────────────────────

@router.get("/categories", response_model=list[EquipCategoryResponse])
def list_categories(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[EquipCategoryResponse]:
    return EquipmentActions(db, hospital_id, user).list_categories()


@router.post("/categories", response_model=EquipCategoryResponse, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: EquipCategoryCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> EquipCategoryResponse:
    return EquipmentActions(db, hospital_id, user).create_category(payload)


@router.put("/categories/{category_id}", response_model=EquipCategoryResponse)
def update_category(
    category_id: UUID,
    payload: EquipCategoryUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> EquipCategoryResponse:
    return EquipmentActions(db, hospital_id, user).update_category(category_id, payload)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    EquipmentActions(db, hospital_id, user).delete_category(category_id)


# ── Inventory ─────────────────────────────────────────────────────────────────

@router.get("/items", response_model=list[EquipmentResponse])
def list_items(
    search: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    category_id: UUID | None = None,
    amc_only: bool | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[EquipmentResponse]:
    return EquipmentActions(db, hospital_id, user).list_items(
        search=search,
        status_filter=status_filter,
        category_id=category_id,
        amc_only=amc_only,
    )


@router.post("/items", response_model=EquipmentResponse, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: EquipmentCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> EquipmentResponse:
    return EquipmentActions(db, hospital_id, user).create_item(payload)


@router.put("/items/{item_id}", response_model=EquipmentResponse)
def update_item(
    item_id: UUID,
    payload: EquipmentUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> EquipmentResponse:
    return EquipmentActions(db, hospital_id, user).update_item(item_id, payload)


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(
    item_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    EquipmentActions(db, hospital_id, user).delete_item(item_id)
    return None


@router.put("/items/{item_id}/amc", response_model=EquipmentResponse)
def update_amc(
    item_id: UUID,
    payload: AmcUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> EquipmentResponse:
    return EquipmentActions(db, hospital_id, user).update_amc(item_id, payload)


# ── Assignments ───────────────────────────────────────────────────────────────

@router.get("/assignments", response_model=list[AssignmentResponse])
def list_assignments(
    active_only: bool | None = Query(True),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[AssignmentResponse]:
    return EquipmentActions(db, hospital_id, user).list_assignments(active_only=active_only)


@router.post("/assignments", response_model=AssignmentResponse, status_code=status.HTTP_201_CREATED)
def create_assignment(
    payload: AssignmentCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AssignmentResponse:
    return EquipmentActions(db, hospital_id, user).create_assignment(payload)


@router.post("/assignments/{assignment_id}/return", response_model=AssignmentResponse)
def return_assignment(
    assignment_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> AssignmentResponse:
    return EquipmentActions(db, hospital_id, user).return_assignment(assignment_id)


# ── Maintenance ───────────────────────────────────────────────────────────────

@router.get("/maintenance", response_model=list[MaintenanceResponse])
def list_maintenance(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[MaintenanceResponse]:
    return EquipmentActions(db, hospital_id, user).list_maintenance()


@router.post("/maintenance", response_model=MaintenanceResponse, status_code=status.HTTP_201_CREATED)
def schedule_maintenance(
    payload: MaintenanceCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> MaintenanceResponse:
    return EquipmentActions(db, hospital_id, user).schedule_maintenance(payload)


@router.post("/maintenance/{maintenance_id}/complete", response_model=MaintenanceResponse)
def complete_maintenance(
    maintenance_id: UUID,
    payload: MaintenanceComplete,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> MaintenanceResponse:
    return EquipmentActions(db, hospital_id, user).complete_maintenance(maintenance_id, payload)


# ── Service History ───────────────────────────────────────────────────────────

@router.get("/service-logs", response_model=list[ServiceLogResponse])
def list_service_logs(
    equipment_id: UUID | None = None,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[ServiceLogResponse]:
    return EquipmentActions(db, hospital_id, user).list_service_logs(equipment_id=equipment_id)


@router.post("/service-logs", response_model=ServiceLogResponse, status_code=status.HTTP_201_CREATED)
def create_service_log(
    payload: ServiceLogCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> ServiceLogResponse:
    return EquipmentActions(db, hospital_id, user).create_service_log(payload)


# ── Requests ──────────────────────────────────────────────────────────────────

@router.get("/requests", response_model=list[RequestResponse])
def list_requests(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[RequestResponse]:
    return EquipmentActions(db, hospital_id, user).list_requests(status_filter=status_filter)


@router.post("/requests", response_model=RequestResponse, status_code=status.HTTP_201_CREATED)
def create_request(
    payload: RequestCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RequestResponse:
    return EquipmentActions(db, hospital_id, user).create_request(payload)


@router.post("/requests/{request_id}/approve", response_model=RequestResponse)
def approve_request(
    request_id: UUID,
    payload: RequestAction | None = None,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RequestResponse:
    return EquipmentActions(db, hospital_id, user).approve_request(request_id, payload)


@router.post("/requests/{request_id}/reject", response_model=RequestResponse)
def reject_request(
    request_id: UUID,
    payload: RequestAction | None = None,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RequestResponse:
    return EquipmentActions(db, hospital_id, user).reject_request(request_id, payload)


@router.post("/requests/{request_id}/assign", response_model=RequestResponse)
def assign_request(
    request_id: UUID,
    payload: RequestAction,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> RequestResponse:
    return EquipmentActions(db, hospital_id, user).assign_request(request_id, payload)
