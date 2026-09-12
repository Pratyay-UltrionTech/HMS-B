"""
FastAPI router for Critical Care domain under /critical-care.

Covers Features 6–9:
6. ICU Patient Board: GET /critical-care/icu-board, PUT /critical-care/admissions/{id}/icu-profile
7. ICU Clinical Chart: POST /critical-care/admissions/{id}/flowsheet, GET /critical-care/admissions/{id}/flowsheet
8. Deterioration Alerts: POST /critical-care/alerts/calculate, GET /critical-care/alerts, POST /critical-care/alerts/{id}/acknowledge
9. Code Blue Management: POST /critical-care/code-blue, GET /critical-care/code-blue, POST /critical-care/code-blue/{id}/arrived, POST /critical-care/code-blue/{id}/events, POST /critical-care/code-blue/{id}/conclude

Conforms to UltrionTech-Backend-Template modules/critical_care/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.critical_care.actions.alert_engine_actions import (
    AcknowledgeDeteriorationAlertAction,
    CalculateDeteriorationScoreAction,
)
from hms_migration.modules.critical_care.actions.code_blue_actions import (
    ActivateCodeBlueAction,
    ConcludeCodeBlueAction,
    LogCodeBlueEventAction,
    MarkCodeBlueTeamArrivedAction,
)
from hms_migration.modules.critical_care.actions.flowsheet_actions import (
    ListIcuFlowsheetAction,
    RecordIcuFlowsheetAction,
)
from hms_migration.modules.critical_care.actions.icu_board_actions import (
    GetIcuBoardAction,
    UpdateIcuProfileAction,
)
from hms_migration.modules.critical_care.contracts.critical_care_contracts import (
    CodeBlueConcludeRequest,
    CodeBlueEventCreate,
    CodeBlueEventResponse,
    CodeBlueIncidentCreate,
    CodeBlueIncidentResponse,
    DeteriorationAlertAcknowledgeRequest,
    DeteriorationAlertCalculateRequest,
    DeteriorationAlertResponse,
    IcuBoardPatientResponse,
    IcuFlowsheetCreate,
    IcuFlowsheetResponse,
    IcuProfileUpdate,
)
from hms_migration.modules.critical_care.db.critical_care_repository import CriticalCareRepository
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/critical-care", tags=["critical-care"])


# --- Feature 6: ICU Patient Board ---
@router.get("/icu-board", response_model=list[IcuBoardPatientResponse])
def get_icu_board(
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Retrieve live intensivist ICU patient dashboard."""
    return GetIcuBoardAction(db, hospital_id).execute()


@router.put("/admissions/{admission_id}/icu-profile")
def update_icu_profile(
    admission_id: UUID,
    payload: IcuProfileUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Update ventilator, invasive lines, and critical care profile for an ICU admission."""
    profile = UpdateIcuProfileAction(db, hospital_id).execute(admission_id, payload, user)
    return {"status": "ok", "profile_id": str(profile.id)}


# --- Feature 7: ICU Clinical Chart & Flowsheet ---
@router.post(
    "/admissions/{admission_id}/flowsheet",
    response_model=IcuFlowsheetResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_icu_flowsheet(
    admission_id: UUID,
    payload: IcuFlowsheetCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Record hourly vitals, ventilator parameters, ABG, and intake/output fluids."""
    return RecordIcuFlowsheetAction(db, hospital_id).execute(admission_id, payload, user)


@router.get(
    "/admissions/{admission_id}/flowsheet",
    response_model=list[IcuFlowsheetResponse],
)
def list_icu_flowsheet(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Get chronological hourly flowsheet entries for an ICU admission."""
    return ListIcuFlowsheetAction(db, hospital_id).execute(admission_id)


# --- Feature 8: Deterioration Alerts (NEWS2) ---
@router.post("/alerts/calculate", response_model=DeteriorationAlertResponse)
def calculate_deterioration_score(
    payload: DeteriorationAlertCalculateRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Compute NEWS2 score from vital parameters and trigger clinical alerts when thresholds breached."""
    return CalculateDeteriorationScoreAction(db, hospital_id).execute(payload, user)


@router.get("/alerts", response_model=list[DeteriorationAlertResponse])
def list_deterioration_alerts(
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """List active clinical deterioration alerts for bedside nursing and escalation."""
    repo = CriticalCareRepository(db, hospital_id)
    alerts = repo.list_active_alerts()
    return [DeteriorationAlertResponse.model_validate(a) for a in alerts]


@router.post("/alerts/{alert_id}/acknowledge", response_model=DeteriorationAlertResponse)
def acknowledge_deterioration_alert(
    alert_id: UUID,
    payload: DeteriorationAlertAcknowledgeRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Acknowledge an active clinical deterioration alert."""
    return AcknowledgeDeteriorationAlertAction(db, hospital_id).execute(alert_id, payload.notes, user)


# --- Feature 9: Code Blue Management ---
@router.post(
    "/code-blue",
    response_model=CodeBlueIncidentResponse,
    status_code=status.HTTP_201_CREATED,
)
def activate_code_blue(
    payload: CodeBlueIncidentCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Activate in-hospital resuscitation Code Blue incident."""
    return ActivateCodeBlueAction(db, hospital_id).execute(payload, user)


@router.get("/code-blue", response_model=list[CodeBlueIncidentResponse])
def list_code_blue_incidents(
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """List all Code Blue resuscitation incidents."""
    repo = CriticalCareRepository(db, hospital_id)
    incidents = repo.list_code_blue_incidents()
    return [CodeBlueIncidentResponse.model_validate(i) for i in incidents]


@router.get("/code-blue/{incident_id}", response_model=CodeBlueIncidentResponse)
def get_code_blue_incident(
    incident_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Get Code Blue incident with full event timeline."""
    repo = CriticalCareRepository(db, hospital_id)
    inc = repo.get_code_blue_by_id(incident_id)
    if not inc:
        from fastapi import HTTPException
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code Blue incident not found")
    return CodeBlueIncidentResponse.model_validate(inc)


@router.post("/code-blue/{incident_id}/arrived", response_model=CodeBlueIncidentResponse)
def mark_code_blue_team_arrived(
    incident_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Timestamp arrival of the Code Blue resuscitation team."""
    return MarkCodeBlueTeamArrivedAction(db, hospital_id).execute(incident_id, user)


@router.post(
    "/code-blue/{incident_id}/events",
    response_model=CodeBlueEventResponse,
    status_code=status.HTTP_201_CREATED,
)
def log_code_blue_event(
    incident_id: UUID,
    payload: CodeBlueEventCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Log CPR cycle, defibrillator shock, or emergency drug in resuscitation timeline."""
    return LogCodeBlueEventAction(db, hospital_id).execute(incident_id, payload, user)


@router.post("/code-blue/{incident_id}/conclude", response_model=CodeBlueIncidentResponse)
def conclude_code_blue(
    incident_id: UUID,
    payload: CodeBlueConcludeRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Conclude resuscitation event and document final patient outcome (ROSC, ICU, deceased)."""
    return ConcludeCodeBlueAction(db, hospital_id).execute(incident_id, payload, user)
