"""
FastAPI router for Nursing Domain & Merged Clinical Notes & eMAR under /ipd.

Features covered:
- Feature 12: Nursing Care Planning
- Feature 13: Nursing Notes (Merged into IPD Clinical Notes)
- Feature 14: Nursing Shift Handover (SBAR)
- Feature 15: Electronic Medication Administration Record (eMAR)
- Feature 18: High-Alert Dual Sign-Off enforcement in eMAR

Conforms to UltrionTech-Backend-Template modules/inpatient/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.inpatient.actions.care_plan_actions import (
    CreateNursingCarePlanAction,
    GetNursingCarePlanAction,
    ListNursingCarePlansAction,
    ReassessNursingCarePlanAction,
)
from hms_migration.modules.inpatient.actions.clinical_notes_actions import (
    CreateClinicalNoteAction,
    GetClinicalNoteAction,
    ListClinicalNotesAction,
)
from hms_migration.modules.inpatient.actions.emar_actions import (
    ListMedicationAdminRecordsAction,
    RecordMedicationExecutionAction,
    ScheduleMedicationAction,
)
from hms_migration.modules.inpatient.actions.handover_actions import (
    AcknowledgeShiftHandoverAction,
    CreateShiftHandoverAction,
    GetShiftHandoverAction,
    ListShiftHandoversAction,
)
from hms_migration.modules.inpatient.contracts.nursing_contracts import (
    IpdClinicalNoteCreate,
    IpdClinicalNoteResponse,
    MedicationAdminRecordExecution,
    MedicationAdminResponse,
    MedicationAdminScheduleCreate,
    NursingCarePlanCreate,
    NursingCarePlanReassess,
    NursingCarePlanResponse,
    NursingShiftHandoverAcknowledge,
    NursingShiftHandoverCreate,
    NursingShiftHandoverResponse,
)
from hms_migration.modules.inpatient.entities.nursing_entities import (
    ClinicalNoteType,
    MedicationAdminStatus,
)
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/ipd", tags=["nursing", "ipd"])


# ==========================================
# Feature 12: Nursing Care Planning
# ==========================================
@router.post(
    "/admissions/{admission_id}/care-plans",
    response_model=NursingCarePlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_care_plan(
    admission_id: UUID,
    payload: NursingCarePlanCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return CreateNursingCarePlanAction(db, hospital_id).execute(admission_id, payload, user)


@router.get(
    "/admissions/{admission_id}/care-plans",
    response_model=list[NursingCarePlanResponse],
)
def list_care_plans(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListNursingCarePlansAction(db, hospital_id).execute(admission_id)


@router.get(
    "/care-plans/{plan_id}",
    response_model=NursingCarePlanResponse,
)
def get_care_plan(
    plan_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetNursingCarePlanAction(db, hospital_id).execute(plan_id)


@router.put(
    "/care-plans/{plan_id}/reassess",
    response_model=NursingCarePlanResponse,
)
def reassess_care_plan(
    plan_id: UUID,
    payload: NursingCarePlanReassess,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ReassessNursingCarePlanAction(db, hospital_id).execute(plan_id, payload, user)


# ==========================================
# Feature 13: Merged IPD Clinical Notes
# ==========================================
@router.post(
    "/admissions/{admission_id}/clinical-notes",
    response_model=IpdClinicalNoteResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_clinical_note(
    admission_id: UUID,
    payload: IpdClinicalNoteCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return CreateClinicalNoteAction(db, hospital_id).execute(admission_id, payload, user)


@router.get(
    "/admissions/{admission_id}/clinical-notes",
    response_model=list[IpdClinicalNoteResponse],
)
def list_clinical_notes(
    admission_id: UUID,
    note_type: ClinicalNoteType | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListClinicalNotesAction(db, hospital_id).execute(admission_id, note_type=note_type)


@router.get(
    "/clinical-notes/{note_id}",
    response_model=IpdClinicalNoteResponse,
)
def get_clinical_note(
    note_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetClinicalNoteAction(db, hospital_id).execute(note_id)


# ==========================================
# Feature 14: Shift Handovers (SBAR)
# ==========================================
@router.post(
    "/admissions/{admission_id}/handovers",
    response_model=NursingShiftHandoverResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_shift_handover(
    admission_id: UUID,
    payload: NursingShiftHandoverCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return CreateShiftHandoverAction(db, hospital_id).execute(admission_id, payload, user)


@router.get(
    "/admissions/{admission_id}/handovers",
    response_model=list[NursingShiftHandoverResponse],
)
def list_shift_handovers(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListShiftHandoversAction(db, hospital_id).execute(admission_id)


@router.get(
    "/handovers/{handover_id}",
    response_model=NursingShiftHandoverResponse,
)
def get_shift_handover(
    handover_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetShiftHandoverAction(db, hospital_id).execute(handover_id)


@router.post(
    "/handovers/{handover_id}/acknowledge",
    response_model=NursingShiftHandoverResponse,
)
def acknowledge_shift_handover(
    handover_id: UUID,
    payload: NursingShiftHandoverAcknowledge | None = None,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return AcknowledgeShiftHandoverAction(db, hospital_id).execute(handover_id, payload, user)


# ==========================================
# Feature 15 & 18: eMAR & Dual Sign-Off
# ==========================================
@router.post(
    "/admissions/{admission_id}/emar",
    response_model=MedicationAdminResponse,
    status_code=status.HTTP_201_CREATED,
)
def schedule_medication_emar(
    admission_id: UUID,
    payload: MedicationAdminScheduleCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ScheduleMedicationAction(db, hospital_id).execute(admission_id, payload, user)


@router.get(
    "/admissions/{admission_id}/emar",
    response_model=list[MedicationAdminResponse],
)
def list_medication_emar(
    admission_id: UUID,
    medication_status: MedicationAdminStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListMedicationAdminRecordsAction(db, hospital_id).execute(
        admission_id, status=medication_status
    )


@router.put(
    "/emar/{record_id}/record",
    response_model=MedicationAdminResponse,
)
def record_medication_emar_execution(
    record_id: UUID,
    payload: MedicationAdminRecordExecution,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return RecordMedicationExecutionAction(db, hospital_id).execute(record_id, payload, user)
