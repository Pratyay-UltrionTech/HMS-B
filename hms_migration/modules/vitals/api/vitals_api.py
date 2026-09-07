"""
HTTP API router and route handlers for the Vitals module.

Thin controllers that parse HTTP parameters, resolve dependencies,
delegate to business actions, and return typed responses.
Consumes shared foundation: shared auth, shared exceptions, and postgres infrastructure.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres import get_transitional_sync_session as get_db
from hms_migration.modules.vitals.actions.create_vitals_action import CreateVitalsAction
from hms_migration.modules.vitals.actions.delete_vital_action import DeleteVitalAction
from hms_migration.modules.vitals.actions.get_today_vitals_action import GetTodayVitalsAction
from hms_migration.modules.vitals.actions.list_vitals_action import ListVitalsAction
from hms_migration.modules.vitals.actions.update_vital_action import UpdateVitalAction
from hms_migration.modules.vitals.contracts.vitals_contracts import (
    VitalBatchCreate,
    VitalItemUpdate,
    VitalReadingResponse,
    VitalsTodayItem,
)
from hms_migration.modules.vitals.db.vitals_repository import VitalsRepository
from hms_migration.modules.vitals.permissions.vitals_permissions import (
    get_vitals_hospital_context,
    require_vitals_access,
)

router = APIRouter(prefix="/vitals", tags=["vitals"])


@router.get("/today", response_model=list[VitalsTodayItem])
def list_today_bookings(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_vitals_access),
    hospital_id: UUID = Depends(get_vitals_hospital_context),
) -> list[VitalsTodayItem]:
    """Today's appointments sorted chronologically, with recorded vitals and auto-reversion."""
    repo = VitalsRepository(db=db, hospital_id=hospital_id)
    return GetTodayVitalsAction(repo).execute()


@router.get("", response_model=list[VitalReadingResponse])
def list_vitals(
    appointment_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_vitals_access),
    hospital_id: UUID = Depends(get_vitals_hospital_context),
) -> list[VitalReadingResponse]:
    """List recorded vitals filtered by appointment_id or patient_id."""
    repo = VitalsRepository(db=db, hospital_id=hospital_id)
    return ListVitalsAction(repo).execute(
        appointment_id=appointment_id,
        patient_id=patient_id,
    )


@router.post("", response_model=list[VitalReadingResponse], status_code=status.HTTP_201_CREATED)
def create_vitals(
    payload: VitalBatchCreate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_vitals_access),
    hospital_id: UUID = Depends(get_vitals_hospital_context),
) -> list[VitalReadingResponse]:
    """Batch create vital readings for a visit, transitioning visit to waiting with queue token."""
    repo = VitalsRepository(db=db, hospital_id=hospital_id)
    return CreateVitalsAction(repo).execute(payload=payload, user=user)


@router.put("/{vital_id}", response_model=VitalReadingResponse)
def update_vital(
    vital_id: UUID,
    payload: VitalItemUpdate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_vitals_access),
    hospital_id: UUID = Depends(get_vitals_hospital_context),
) -> VitalReadingResponse:
    """Update an existing vital reading record."""
    repo = VitalsRepository(db=db, hospital_id=hospital_id)
    return UpdateVitalAction(repo).execute(
        vital_id=vital_id,
        payload=payload,
        user=user,
    )


@router.delete("/{vital_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vital(
    vital_id: UUID,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_vitals_access),
    hospital_id: UUID = Depends(get_vitals_hospital_context),
) -> None:
    """Delete an existing vital reading record."""
    repo = VitalsRepository(db=db, hospital_id=hospital_id)
    DeleteVitalAction(repo).execute(vital_id=vital_id, user=user)
