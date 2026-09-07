"""
HTTP API router and route handlers for the Patient domain.

Conforms to UltrionTech-Backend-Template modules/patients/api/ specification.
Thin controllers that validate input parameters, resolve hospital/user dependencies,
delegate to business actions, and return typed responses.
Consumes shared foundation: shared auth, shared exceptions, and postgres infrastructure.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import (
    get_transitional_sync_session as get_db,
)
from hms_migration.modules.patients.actions.get_patient_profile_action import (
    GetPatientProfileAction,
)
from hms_migration.modules.patients.actions.list_patients_action import (
    ListPatientsAction,
)
from hms_migration.modules.patients.actions.register_patient_action import (
    RegisterPatientAction,
)
from hms_migration.modules.patients.actions.update_patient_action import (
    UpdatePatientAction,
)
from hms_migration.modules.patients.contracts.patients_contracts import (
    PatientDirectoryItem,
    PatientProfile,
    PatientRegister,
    PatientRegisterUpdate,
    PatientStatus,
)
from hms_migration.modules.patients.db.patients_repository import PatientRepository
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/registration/patients", tags=["patients"])


@router.post(
    "",
    response_model=PatientDirectoryItem,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new patient",
)
def register_patient(
    payload: PatientRegister,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PatientDirectoryItem:
    """Register a new patient within the current hospital tenant."""
    repo = PatientRepository(db=db, hospital_id=hospital_id)
    return RegisterPatientAction(repo).execute(payload=payload, user=user)


@router.get(
    "",
    response_model=list[PatientDirectoryItem],
    summary="List or search patients",
)
def list_patients(
    search: str | None = Query(default=None),
    status_filter: PatientStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[PatientDirectoryItem]:
    """Search and list patients within the current hospital tenant."""
    repo = PatientRepository(db=db, hospital_id=hospital_id)
    return ListPatientsAction(repo).execute(search=search, status_filter=status_filter)


@router.get(
    "/{patient_id}",
    response_model=PatientProfile,
    summary="Get patient 360 profile",
)
def get_patient_profile(
    patient_id: UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PatientProfile:
    """Retrieve full clinical and demographic profile for a patient."""
    repo = PatientRepository(db=db, hospital_id=hospital_id)
    return GetPatientProfileAction(repo).execute(patient_id=patient_id)


@router.put(
    "/{patient_id}",
    response_model=PatientDirectoryItem,
    summary="Update patient record",
)
def update_patient(
    patient_id: UUID,
    payload: PatientRegisterUpdate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> PatientDirectoryItem:
    """Update patient demographic and administrative details."""
    repo = PatientRepository(db=db, hospital_id=hospital_id)
    return UpdatePatientAction(repo).execute(
        patient_id=patient_id,
        payload=payload,
        user=user,
    )
