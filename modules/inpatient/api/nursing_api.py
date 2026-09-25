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

from infrastructure.postgres.session import get_transitional_sync_session
from modules.inpatient.actions.care_plan_actions import (
    CreateNursingCarePlanAction,
    GetNursingCarePlanAction,
    ListNursingCarePlansAction,
    ReassessNursingCarePlanAction,
)
from modules.inpatient.actions.clinical_notes_actions import (
    CreateClinicalNoteAction,
    GetClinicalNoteAction,
    ListClinicalNotesAction,
)
from modules.inpatient.actions.emar_actions import (
    ListMedicationAdminRecordsAction,
    RecordMedicationExecutionAction,
    ScheduleMedicationAction,
)
from modules.inpatient.actions.handover_actions import (
    AcknowledgeShiftHandoverAction,
    CreateShiftHandoverAction,
    GetShiftHandoverAction,
    ListShiftHandoversAction,
)
from modules.inpatient.contracts.nursing_contracts import (
    IpdClinicalNoteCreate,
    IpdClinicalNoteResponse,
    IpdIntakeOutputCreate,
    IpdIntakeOutputResponse,
    IpdIntakeOutputSummary,
    IpdVitalSignCreate,
    IpdVitalSignResponse,
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
from modules.inpatient.actions.inpatient_observations_actions import (
    GetIntakeOutputSummaryAction,
    ListIntakeOutputAction,
    ListIpdVitalsAction,
    RecordIntakeOutputAction,
    RecordIpdVitalsAction,
)
from modules.inpatient.contracts.chart_contracts import AdmissionChartResponse
from modules.inpatient.services.admission_chart_service import AdmissionChartService
from modules.inpatient.entities.nursing_entities import (
    ClinicalNoteType,
    MedicationAdminStatus,
)
from shared.auth.dependencies import (
    get_hospital_context,
    require_any_permission,
    require_hospital_user,
    require_permission,
)

router = APIRouter(prefix="/ipd", tags=["nursing", "ipd"])

IPD_CHART_VIEW_PERMS = [("nurses", "view"), ("doctors", "view"), ("all_ipd", "view"), ("bed", "view")]
IPD_CLINICAL_VIEW_PERMS = [("nurses", "view"), ("doctors", "view"), ("all_ipd", "view")]


@router.get(
    "/admissions/{admission_id}/chart",
    response_model=AdmissionChartResponse,
    dependencies=[Depends(require_any_permission(IPD_CHART_VIEW_PERMS))],
)
def get_admission_chart(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Read the existing clinical story for one admission without duplicating it."""
    return AdmissionChartService(db, hospital_id).get(admission_id)


# ==========================================
# Feature 12: Nursing Care Planning
# ==========================================
@router.post(
    "/admissions/{admission_id}/care-plans",
    response_model=NursingCarePlanResponse,
    status_code=status.HTTP_201_CREATED,

    dependencies=[Depends(require_permission("nurses", "edit"))])
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
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
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
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
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
    dependencies=[Depends(require_permission("nurses", "edit"))],
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
    dependencies=[Depends(require_any_permission([("nurses", "edit"), ("doctors", "edit")]))],
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
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
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
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
)
def get_clinical_note(
    note_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetClinicalNoteAction(db, hospital_id).execute(note_id)


@router.post(
    "/admissions/{admission_id}/doctor-notes",
    response_model=IpdClinicalNoteResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("doctors", "edit"))],
)
def create_doctor_progress_note(
    admission_id: UUID,
    payload: IpdClinicalNoteCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Create an admission-bound doctor progress note without an OPD appointment."""
    doctor_payload = payload.model_copy(
        update={"note_type": ClinicalNoteType.doctor_progress}
    )
    return CreateClinicalNoteAction(db, hospital_id).execute(
        admission_id, doctor_payload, user
    )


@router.get(
    "/admissions/{admission_id}/doctor-notes",
    response_model=list[IpdClinicalNoteResponse],
    dependencies=[Depends(require_permission("doctors", "view"))],
)
def list_doctor_progress_notes(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """List historical doctor progress notes for one admission."""
    return ListClinicalNotesAction(db, hospital_id).execute(
        admission_id, note_type=ClinicalNoteType.doctor_progress
    )


# ==========================================
# Feature 14: Shift Handovers (SBAR)
# ==========================================
@router.post(
    "/admissions/{admission_id}/handovers",
    response_model=NursingShiftHandoverResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("nurses", "edit"))],
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
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
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
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
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
    dependencies=[Depends(require_permission("nurses", "edit"))],
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
    dependencies=[Depends(require_permission("nurses", "edit"))],
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
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
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
    dependencies=[Depends(require_permission("nurses", "edit"))],
)
def record_medication_emar_execution(
    record_id: UUID,
    payload: MedicationAdminRecordExecution,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return RecordMedicationExecutionAction(db, hospital_id).execute(record_id, payload, user)


# ==========================================
# Feature: Structured Inpatient Vitals & Observations
# ==========================================
@router.post(
    "/admissions/{admission_id}/vitals",
    response_model=IpdVitalSignResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission([("nurses", "edit"), ("doctors", "edit")]))],
)
def record_ipd_vitals(
    admission_id: UUID,
    payload: IpdVitalSignCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Record structured vital signs and clinical observations for an admitted patient."""
    return RecordIpdVitalsAction(db, hospital_id).execute(admission_id, payload, user)


@router.get(
    "/admissions/{admission_id}/vitals",
    response_model=list[IpdVitalSignResponse],
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
)
def list_ipd_vitals(
    admission_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """List chronological vital signs and observations for an admission."""
    return ListIpdVitalsAction(db, hospital_id).execute(admission_id, limit=limit)


# ==========================================
# Feature: Ward Intake & Output Observations
# ==========================================
@router.post(
    "/admissions/{admission_id}/intake-output",
    response_model=IpdIntakeOutputResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission([("nurses", "edit"), ("doctors", "edit")]))],
)
def record_intake_output(
    admission_id: UUID,
    payload: IpdIntakeOutputCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Record fluid intake or output observation for an inpatient."""
    return RecordIntakeOutputAction(db, hospital_id).execute(admission_id, payload, user)


@router.get(
    "/admissions/{admission_id}/intake-output",
    response_model=list[IpdIntakeOutputResponse],
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
)
def list_intake_output(
    admission_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """List chronological intake and output records for an admission."""
    return ListIntakeOutputAction(db, hospital_id).execute(admission_id, limit=limit)


@router.get(
    "/admissions/{admission_id}/intake-output/summary",
    response_model=IpdIntakeOutputSummary,
    dependencies=[Depends(require_any_permission(IPD_CLINICAL_VIEW_PERMS))],
)
def get_intake_output_summary(
    admission_id: UUID,
    hours: int = Query(default=24, ge=1, le=168),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Get fluid balance summary (total intake, output, net balance) for an admission."""
    return GetIntakeOutputSummaryAction(db, hospital_id).execute(admission_id, hours=hours)
