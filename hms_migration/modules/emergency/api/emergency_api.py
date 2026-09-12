"""
FastAPI router for Emergency Department domain under /emergency.

Covers Features 1–5:
1. Emergency Case Registration: POST /emergency/encounters
2. Emergency Triage Assessment: POST /emergency/encounters/{id}/triage
3. Triage Queue: GET /emergency/queue & POST /emergency/encounters/{id}/escalate
4. Emergency Orders & Treatment: POST /emergency/encounters/{id}/orders, GET /emergency/encounters/{id}/orders, POST /emergency/orders/{id}/execute
5. Emergency Disposition: POST /emergency/encounters/{id}/disposition

Conforms to UltrionTech-Backend-Template modules/emergency/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.emergency.actions.emergency_actions import (
    EscalateEmergencyEncounterAction,
    GetEmergencyQueueAction,
    PerformEmergencyTriageAction,
    RecordEmergencyDispositionAction,
    RegisterEmergencyEncounterAction,
)
from hms_migration.modules.emergency.actions.treatment_actions import (
    CreateEmergencyOrderAction,
    ExecuteEmergencyOrderAction,
    ListEmergencyOrdersAction,
)
from hms_migration.modules.emergency.contracts.emergency_contracts import (
    EmergencyDispositionCreate,
    EmergencyDispositionResponse,
    EmergencyEncounterCreate,
    EmergencyEncounterResponse,
    EmergencyEscalateRequest,
    EmergencyOrderCreate,
    EmergencyOrderExecute,
    EmergencyOrderResponse,
    EmergencyQueueItemResponse,
    EmergencyTriageCreate,
    EmergencyTriageResponse,
)
from hms_migration.modules.emergency.db.emergency_repository import EmergencyRepository
from hms_migration.modules.emergency.permissions.emergency_permissions import (
    get_emergency_hospital_context,
    require_emergency_access,
)

router = APIRouter(prefix="/emergency", tags=["emergency"])


# --- Feature 1: Emergency Case Registration ---
@router.post(
    "/encounters",
    response_model=EmergencyEncounterResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_emergency_encounter(
    payload: EmergencyEncounterCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """Register an emergency patient arrival and generate encounter with ER sequence ID."""
    return RegisterEmergencyEncounterAction(db, hospital_id).execute(payload, user)


@router.get(
    "/encounters",
    response_model=list[EmergencyEncounterResponse],
)
def list_emergency_encounters(
    search: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """List emergency encounters for the hospital."""
    repo = EmergencyRepository(db, hospital_id)
    encounters = repo.list_encounters(search=search)
    return [EmergencyEncounterResponse.model_validate(e) for e in encounters]


@router.get(
    "/encounters/{encounter_id}",
    response_model=EmergencyEncounterResponse,
)
def get_emergency_encounter(
    encounter_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """Get detail for a specific emergency encounter."""
    repo = EmergencyRepository(db, hospital_id)
    enc = repo.get_encounter_by_id(encounter_id)
    if not enc:
        from fastapi import HTTPException
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Emergency encounter not found")
    return EmergencyEncounterResponse.model_validate(enc)


# --- Feature 2: Emergency Triage Assessment ---
@router.post(
    "/encounters/{encounter_id}/triage",
    response_model=EmergencyTriageResponse,
    status_code=status.HTTP_201_CREATED,
)
def perform_emergency_triage(
    encounter_id: UUID,
    payload: EmergencyTriageCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """Perform clinical triage evaluation (acuity 1-5, AVPU, vitals, red flags)."""
    return PerformEmergencyTriageAction(db, hospital_id).execute(encounter_id, payload, user)


# --- Feature 3: Live Triage Queue & Escalation ---
@router.get(
    "/queue",
    response_model=list[EmergencyQueueItemResponse],
)
def get_triage_queue(
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """Get live priority-based emergency queue sorted strictly by clinical urgency."""
    return GetEmergencyQueueAction(db, hospital_id).execute()


@router.post(
    "/encounters/{encounter_id}/escalate",
    response_model=EmergencyEncounterResponse,
)
def escalate_emergency_encounter(
    encounter_id: UUID,
    payload: EmergencyEscalateRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """Escalate a deteriorating patient in the emergency queue."""
    return EscalateEmergencyEncounterAction(db, hospital_id).execute(
        encounter_id, payload.escalation_reason, user
    )


# --- Feature 4: Emergency Orders & Treatment ---
@router.post(
    "/encounters/{encounter_id}/orders",
    response_model=EmergencyOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_emergency_order(
    encounter_id: UUID,
    payload: EmergencyOrderCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """Record rapid emergency treatment, STAT medication, or verbal orders."""
    return CreateEmergencyOrderAction(db, hospital_id).execute(encounter_id, payload, user)


@router.get(
    "/encounters/{encounter_id}/orders",
    response_model=list[EmergencyOrderResponse],
)
def list_emergency_orders(
    encounter_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """List orders placed on an emergency encounter."""
    return ListEmergencyOrdersAction(db, hospital_id).execute(encounter_id)


@router.post(
    "/orders/{order_id}/execute",
    response_model=EmergencyOrderResponse,
)
def execute_emergency_order(
    order_id: UUID,
    payload: EmergencyOrderExecute,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """Log nurse execution of emergency treatment or verbal orders."""
    return ExecuteEmergencyOrderAction(db, hospital_id).execute(order_id, payload, user)


# --- Feature 5: Emergency Disposition ---
@router.post(
    "/encounters/{encounter_id}/disposition",
    response_model=EmergencyDispositionResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_emergency_disposition(
    encounter_id: UUID,
    payload: EmergencyDispositionCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_emergency_access),
    hospital_id: UUID = Depends(get_emergency_hospital_context),
):
    """Record final emergency outcome (admission, observation, discharge, referral, LAMA)."""
    return RecordEmergencyDispositionAction(db, hospital_id).execute(encounter_id, payload, user)
