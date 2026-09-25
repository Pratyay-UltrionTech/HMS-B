"""
FastAPI router for IPD Form Submissions under /ipd.

Conforms to UltrionTech-Backend-Template modules/inpatient/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from infrastructure.postgres.session import get_transitional_sync_session
from modules.inpatient.actions.care_team_actions import (
    AddCareTeamMemberAction,
    EndCareTeamAssignmentAction,
    ListCareTeamAction,
)
from modules.inpatient.actions.discharge_exception_actions import (
    DecideDischargeExceptionAction,
    ListDischargeExceptionsAction,
    RequestDischargeExceptionAction,
)
from modules.inpatient.actions.form_addendum_actions import (
    CreateFormAddendumAction,
    ListFormAddendaAction,
)
from modules.inpatient.actions.ipd_actions import (
    CreateFormSubmissionAction,
    GetFormSubmissionAction,
    ListFormSubmissionsAction,
    UpdateFormSubmissionAction,
    ViewFormSubmissionHtmlAction,
)
from modules.inpatient.actions.medication_order_actions import (
    AdministerPrnMedicationAction,
    CreateMedicationOrderAction,
    DiscontinueMedicationOrderAction,
    ListMedicationOrdersAction,
)
from modules.inpatient.actions.discharge_medication_actions import (
    GetDischargeMedicationStatusAction,
    GetSuggestedDischargeMedsAction,
    RecordNoDischargeMedsAction,
)
from modules.inpatient.contracts.inpatient_contracts import (
    AdministerPrnMedicationRequest,
    AdmissionCareTeamCreate,
    AdmissionCareTeamResponse,
    DischargeFinancialExceptionCreate,
    DischargeFinancialExceptionDecision,
    DischargeFinancialExceptionResponse,
    DischargeMedicationStatusResponse,
    DiscontinueMedicationOrderRequest,
    IpdFormAddendumCreate,
    IpdFormAddendumResponse,
    IpdFormSubmissionCreate,
    IpdFormSubmissionResponse,
    IpdFormSubmissionUpdate,
    IpdMedicationOrderCreate,
    IpdMedicationOrderResponse,
    NoDischargeMedsRequest,
    SuggestedDischargeMedication,
)
from modules.inpatient.contracts.nursing_contracts import MedicationAdminResponse
from modules.inpatient.entities.admission import IpdFormSubmissionStatus
from modules.inpatient.entities.discharge_exception import ExceptionStatus
from modules.inpatient.entities.medication_order import MedicationOrderStatus
from shared.auth.dependencies import (
    get_hospital_context,
    require_any_permission,
    require_hospital_user,
    require_permission,
)

router = APIRouter(prefix="/ipd", tags=["ipd"])


@router.post(
    "/form-submissions",
    response_model=IpdFormSubmissionResponse,
    status_code=status.HTTP_201_CREATED,

    dependencies=[Depends(require_any_permission([("all_ipd", "edit"), ("doctors", "edit"), ("nurses", "edit")]))])
def create_form_submission(
    payload: IpdFormSubmissionCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return CreateFormSubmissionAction(db).execute(hospital_id, payload, user)


@router.put("/form-submissions/{submission_id}", response_model=IpdFormSubmissionResponse,
    dependencies=[Depends(require_any_permission([("all_ipd", "edit"), ("doctors", "edit"), ("nurses", "edit")]))])
def update_form_submission(
    submission_id: UUID,
    payload: IpdFormSubmissionUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return UpdateFormSubmissionAction(db).execute(hospital_id, submission_id, payload, user)


@router.get(
    "/form-submissions", response_model=list[IpdFormSubmissionResponse],
    dependencies=[Depends(require_any_permission([("all_ipd", "view"), ("doctors", "view"), ("nurses", "view")]))],
)
def list_form_submissions(
    patient_id: UUID | None = Query(default=None),
    admission_id: UUID | None = Query(default=None),
    form_id: str | None = Query(default=None),
    status_filter: IpdFormSubmissionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListFormSubmissionsAction(db).execute(
        hospital_id, patient_id, admission_id, form_id, status_filter, limit, offset
    )


@router.get(
    "/form-submissions/{submission_id}", response_model=IpdFormSubmissionResponse,
    dependencies=[Depends(require_any_permission([("all_ipd", "view"), ("doctors", "view"), ("nurses", "view")]))],
)
def get_form_submission(
    submission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetFormSubmissionAction(db).execute(hospital_id, submission_id)


@router.get(
    "/form-submissions/{submission_id}/view",
    dependencies=[Depends(require_any_permission([("all_ipd", "view"), ("doctors", "view"), ("nurses", "view")]))],
)
def view_form_submission_html(
    submission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ViewFormSubmissionHtmlAction(db).execute(hospital_id, submission_id)


@router.post(
    "/form-submissions/{submission_id}/addenda",
    response_model=IpdFormAddendumResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission([("all_ipd", "edit"), ("doctors", "edit"), ("nurses", "edit")]))],
)
def create_form_addendum(
    submission_id: UUID,
    payload: IpdFormAddendumCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return CreateFormAddendumAction(db).execute(hospital_id, submission_id, payload, user)


@router.get(
    "/form-submissions/{submission_id}/addenda",
    response_model=list[IpdFormAddendumResponse],
    dependencies=[Depends(require_any_permission([("all_ipd", "view"), ("doctors", "view"), ("nurses", "view")]))],
)
def list_form_addenda(
    submission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListFormAddendaAction(db).execute(hospital_id, submission_id)


# ---------------------------------------------------------------------------
# Care Team Endpoints
# ---------------------------------------------------------------------------

@router.get("/admissions/{admission_id}/care-team", response_model=list[AdmissionCareTeamResponse])
def list_care_team(
    admission_id: UUID,
    active_only: bool = Query(default=False),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListCareTeamAction(db, hospital_id).execute(admission_id, active_only=active_only)


@router.post(
    "/admissions/{admission_id}/care-team",
    response_model=AdmissionCareTeamResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("all_ipd", "edit"))],
)
def add_care_team_member(
    admission_id: UUID,
    payload: AdmissionCareTeamCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return AddCareTeamMemberAction(db, hospital_id).execute(admission_id, payload, user)


@router.delete(
    "/admissions/{admission_id}/care-team/{member_id}",
    response_model=AdmissionCareTeamResponse,
    dependencies=[Depends(require_permission("all_ipd", "edit"))],
)
def end_care_team_member(
    admission_id: UUID,
    member_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return EndCareTeamAssignmentAction(db, hospital_id).execute(member_id, user)


# ---------------------------------------------------------------------------
# Inpatient Medication Order Endpoints
# ---------------------------------------------------------------------------

@router.get("/admissions/{admission_id}/medication-orders", response_model=list[IpdMedicationOrderResponse])
def list_medication_orders(
    admission_id: UUID,
    order_status: MedicationOrderStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListMedicationOrdersAction(db, hospital_id).execute(admission_id, status=order_status)


@router.post(
    "/admissions/{admission_id}/medication-orders",
    response_model=IpdMedicationOrderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("doctors", "edit"))],
)
def create_medication_order(
    admission_id: UUID,
    payload: IpdMedicationOrderCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return CreateMedicationOrderAction(db, hospital_id).execute(admission_id, payload, user)


@router.post(
    "/admissions/{admission_id}/medication-orders/{order_id}/discontinue",
    response_model=IpdMedicationOrderResponse,
    dependencies=[Depends(require_permission("doctors", "edit"))],
)
def discontinue_medication_order(
    admission_id: UUID,
    order_id: UUID,
    payload: DiscontinueMedicationOrderRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return DiscontinueMedicationOrderAction(db, hospital_id).execute(order_id, payload, user)


@router.post(
    "/admissions/{admission_id}/medication-orders/{order_id}/administer-prn",
    response_model=MedicationAdminResponse,
    dependencies=[Depends(require_any_permission([("nurses", "edit"), ("doctors", "edit")]))],
)
def administer_prn_medication(
    admission_id: UUID,
    order_id: UUID,
    payload: AdministerPrnMedicationRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return AdministerPrnMedicationAction(db, hospital_id).execute(order_id, payload, user)



# ---------------------------------------------------------------------------
# Discharge Financial Exception Endpoints
# ---------------------------------------------------------------------------

@router.get("/discharge-exceptions", response_model=list[DischargeFinancialExceptionResponse])
def list_discharge_exceptions(
    admission_id: UUID | None = Query(default=None),
    exception_status: ExceptionStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListDischargeExceptionsAction(db, hospital_id).execute(
        admission_id=admission_id, status=exception_status
    )


@router.post(
    "/admissions/{admission_id}/discharge-exceptions",
    response_model=DischargeFinancialExceptionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("all_ipd", "edit"))],
)
def request_discharge_exception(
    admission_id: UUID,
    payload: DischargeFinancialExceptionCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return RequestDischargeExceptionAction(db, hospital_id).execute(admission_id, payload, user)


@router.post(
    "/discharge-exceptions/{exception_id}/decide",
    response_model=DischargeFinancialExceptionResponse,
    dependencies=[Depends(require_permission("billing", "approve"))],
)
def decide_discharge_exception(
    exception_id: UUID,
    payload: DischargeFinancialExceptionDecision,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return DecideDischargeExceptionAction(db, hospital_id).execute(exception_id, payload, user)


# ---------------------------------------------------------------------------
# Discharge Prescription / Take-Home Medication Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/admissions/{admission_id}/discharge-prescription-status",
    response_model=DischargeMedicationStatusResponse,
    dependencies=[Depends(require_any_permission([("all_ipd", "view"), ("doctors", "view"), ("nurses", "view")]))],
)
def get_discharge_prescription_status(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetDischargeMedicationStatusAction(db, hospital_id).execute(admission_id)


@router.post(
    "/admissions/{admission_id}/no-discharge-meds",
    response_model=DischargeMedicationStatusResponse,
    dependencies=[Depends(require_any_permission([("doctors", "edit"), ("all_ipd", "edit")]))],
)
def record_no_discharge_meds(
    admission_id: UUID,
    payload: NoDischargeMedsRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return RecordNoDischargeMedsAction(db, hospital_id).execute(admission_id, payload, user)


@router.get(
    "/admissions/{admission_id}/suggested-discharge-meds",
    response_model=list[SuggestedDischargeMedication],
    dependencies=[Depends(require_any_permission([("doctors", "view"), ("all_ipd", "view")]))],
)
def get_suggested_discharge_meds(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetSuggestedDischargeMedsAction(db, hospital_id).execute(admission_id)
