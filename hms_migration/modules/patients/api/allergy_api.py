"""
FastAPI router for Patient Allergies & Safety Interception (Feature 16).

Conforms to UltrionTech-Backend-Template modules/patients/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.patients.actions.allergy_actions import (
    AddPatientAllergyAction,
    CheckPatientAllergyAlertAction,
    DeactivatePatientAllergyAction,
    ListPatientAllergiesAction,
)
from hms_migration.modules.patients.contracts.allergy_contracts import (
    AllergyAlertWarning,
    CheckAllergyAlertRequest,
    PatientAllergyCreate,
    PatientAllergyResponse,
)
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/patients", tags=["patients", "allergies"])


@router.post(
    "/{patient_id}/allergies",
    response_model=PatientAllergyResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_patient_allergy(
    patient_id: UUID,
    payload: PatientAllergyCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Record a diagnosed drug, food, or environmental allergy for a patient."""
    return AddPatientAllergyAction(db, hospital_id).execute(patient_id, payload, user)


@router.get(
    "/{patient_id}/allergies",
    response_model=list[PatientAllergyResponse],
)
def list_patient_allergies(
    patient_id: UUID,
    active_only: bool = Query(default=True),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Retrieve recorded allergies for a patient."""
    return ListPatientAllergiesAction(db, hospital_id).execute(patient_id, active_only=active_only)


@router.delete(
    "/allergies/{allergy_id}",
    response_model=PatientAllergyResponse,
)
def deactivate_patient_allergy(
    allergy_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Deactivate an allergy record that is no longer clinically applicable."""
    return DeactivatePatientAllergyAction(db, hospital_id).execute(allergy_id, user)


@router.post(
    "/{patient_id}/check-allergy-alert",
    response_model=AllergyAlertWarning,
)
def check_allergy_alert(
    patient_id: UUID,
    payload: CheckAllergyAlertRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """
    Evaluate medication prescription or administration against active patient allergies.
    Returns structured conflict alerts requiring clinical override if a risk is detected.
    """
    return CheckPatientAllergyAlertAction(db, hospital_id).execute(patient_id, payload)
