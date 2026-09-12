"""
FastAPI router for Ambulance domain under /ambulance.

Covers Features 10 & 11:
10. Ambulance Fleet: POST /ambulance/vehicles, GET /ambulance/vehicles, PUT /ambulance/vehicles/{id}
11. Ambulance Dispatch: POST /ambulance/dispatches, GET /ambulance/dispatches, PUT /ambulance/dispatches/{id}/status

Conforms to UltrionTech-Backend-Template modules/ambulance/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.ambulance.actions.dispatch_actions import (
    CreateAmbulanceDispatchAction,
    UpdateAmbulanceDispatchStatusAction,
)
from hms_migration.modules.ambulance.actions.fleet_actions import (
    CreateAmbulanceVehicleAction,
    UpdateAmbulanceVehicleAction,
)
from hms_migration.modules.ambulance.contracts.ambulance_contracts import (
    AmbulanceDispatchCreate,
    AmbulanceDispatchResponse,
    AmbulanceDispatchUpdateStatus,
    AmbulanceVehicleCreate,
    AmbulanceVehicleResponse,
    AmbulanceVehicleUpdate,
)
from hms_migration.modules.ambulance.db.ambulance_repository import AmbulanceRepository
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/ambulance", tags=["ambulance"])


# --- Feature 10: Ambulance Fleet Management ---
@router.post(
    "/vehicles",
    response_model=AmbulanceVehicleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_ambulance_vehicle(
    payload: AmbulanceVehicleCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Register an ambulance vehicle in the hospital fleet."""
    return CreateAmbulanceVehicleAction(db, hospital_id).execute(payload, user)


@router.get("/vehicles", response_model=list[AmbulanceVehicleResponse])
def list_ambulance_vehicles(
    available_only: bool = Query(default=False),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """List ambulance vehicles in hospital fleet."""
    repo = AmbulanceRepository(db, hospital_id)
    vehicles = repo.list_vehicles(available_only=available_only)
    return [AmbulanceVehicleResponse.model_validate(v) for v in vehicles]


@router.put("/vehicles/{vehicle_id}", response_model=AmbulanceVehicleResponse)
def update_ambulance_vehicle(
    vehicle_id: UUID,
    payload: AmbulanceVehicleUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Update status, equipment, or active crew of an ambulance vehicle."""
    return UpdateAmbulanceVehicleAction(db, hospital_id).execute(vehicle_id, payload, user)


# --- Feature 11: Ambulance Dispatch & Trip Tracking ---
@router.post(
    "/dispatches",
    response_model=AmbulanceDispatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_ambulance_dispatch(
    payload: AmbulanceDispatchCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Assign available ambulance to emergency request and initiate dispatch."""
    return CreateAmbulanceDispatchAction(db, hospital_id).execute(payload, user)


@router.get("/dispatches", response_model=list[AmbulanceDispatchResponse])
def list_ambulance_dispatches(
    active_only: bool = Query(default=False),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """List ambulance dispatches and active trips."""
    repo = AmbulanceRepository(db, hospital_id)
    dispatches = repo.list_dispatches(active_only=active_only)
    return [AmbulanceDispatchResponse.model_validate(d) for d in dispatches]


@router.get("/dispatches/{dispatch_id}", response_model=AmbulanceDispatchResponse)
def get_ambulance_dispatch(
    dispatch_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Get single dispatch trip details."""
    repo = AmbulanceRepository(db, hospital_id)
    disp = repo.get_dispatch_by_id(dispatch_id)
    if not disp:
        from fastapi import HTTPException
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispatch record not found")
    return AmbulanceDispatchResponse.model_validate(disp)


@router.put("/dispatches/{dispatch_id}/status", response_model=AmbulanceDispatchResponse)
def update_ambulance_dispatch_status(
    dispatch_id: UUID,
    payload: AmbulanceDispatchUpdateStatus,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Update dispatch status (at_scene, transporting, completed, cancelled)."""
    return UpdateAmbulanceDispatchStatusAction(db, hospital_id).execute(dispatch_id, payload, user)
