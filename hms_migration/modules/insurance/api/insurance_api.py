"""
FastAPI router for Insurance & TPA Lifecycle endpoints.
Base prefix: /api/insurance
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.insurance.actions.insurance_actions import InsuranceActions
from hms_migration.modules.insurance.contracts.insurance_contracts import (
    AdmissionPolicyLinkCreate,
    AdmissionPolicyResponse,
    CashlessSettlementCreate,
    CashlessSettlementResponse,
    ClaimDisputeCreate,
    ClaimDisputeResponse,
    ClaimDisputeResubmit,
    ClaimDossierCreate,
    ClaimDossierResponse,
    ClaimDossierVerify,
    ClaimSubmissionAcknowledge,
    ClaimSubmissionCreate,
    ClaimSubmissionDecision,
    ClaimSubmissionDetailResponse,
    ClaimSubmissionQuery,
    ClaimSubmissionResponse,
    EligibilityCheckCreate,
    EligibilityCheckResponse,
    PatientPolicyCreate,
    PatientPolicyResponse,
    PreAuthDecisionUpdate,
    PreAuthQueryCreate,
    PreAuthQueryRespond,
    PreAuthQueryResponse,
    PreAuthRequestCreate,
    PreAuthRequestResponse,
    TPAPerformanceReportResponse,
)
from hms_migration.modules.insurance.db.insurance_repository import InsuranceRepository
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/insurance", tags=["Insurance & TPA"])


# ---------------------------------------------------------------------------
# Feature 23: Patient Policies & Admission Link
# ---------------------------------------------------------------------------

@router.post("/policies", response_model=PatientPolicyResponse, status_code=status.HTTP_201_CREATED)
def create_patient_policy(
    payload: PatientPolicyCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> PatientPolicyResponse:
    return InsuranceActions(db, hospital_id, current_user).create_patient_policy(payload)


@router.get("/patients/{patient_id}/policies", response_model=list[PatientPolicyResponse])
def get_patient_policies(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[PatientPolicyResponse]:
    return InsuranceActions(db, hospital_id, current_user).get_patient_policies(patient_id)


@router.post("/admissions/link-policy", response_model=AdmissionPolicyResponse, status_code=status.HTTP_201_CREATED)
def link_admission_policy(
    payload: AdmissionPolicyLinkCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> AdmissionPolicyResponse:
    return InsuranceActions(db, hospital_id, current_user).link_admission_policy(payload)


@router.get("/admissions/{admission_id}/policies", response_model=list[AdmissionPolicyResponse])
def get_admission_policies(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[AdmissionPolicyResponse]:
    return InsuranceActions(db, hospital_id, current_user).get_admission_policies(admission_id)


# ---------------------------------------------------------------------------
# Feature 24: Eligibility Verification
# ---------------------------------------------------------------------------

@router.post("/eligibility-checks", response_model=EligibilityCheckResponse, status_code=status.HTTP_201_CREATED)
def verify_eligibility(
    payload: EligibilityCheckCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> EligibilityCheckResponse:
    return InsuranceActions(db, hospital_id, current_user).verify_eligibility(payload)


# ---------------------------------------------------------------------------
# Feature 25 & 26: Pre-Auth Requests & Query Tracking
# ---------------------------------------------------------------------------

@router.post("/pre-auths", response_model=PreAuthRequestResponse, status_code=status.HTTP_201_CREATED)
def create_pre_auth_request(
    payload: PreAuthRequestCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> PreAuthRequestResponse:
    return InsuranceActions(db, hospital_id, current_user).create_pre_auth_request(payload)


@router.get("/pre-auths", response_model=list[PreAuthRequestResponse])
def get_pre_auth_requests(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[PreAuthRequestResponse]:
    records = InsuranceRepository(db, hospital_id).list_pre_auths_for_hospital(limit=limit)
    return [PreAuthRequestResponse.model_validate(r) for r in records]


@router.get("/pre-auths/{pre_auth_id}", response_model=PreAuthRequestResponse)
def get_pre_auth_request(
    pre_auth_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> PreAuthRequestResponse:
    return InsuranceActions(db, hospital_id, current_user).get_pre_auth_request(pre_auth_id)


@router.patch("/pre-auths/{pre_auth_id}/decision", response_model=PreAuthRequestResponse)
def update_pre_auth_decision(
    pre_auth_id: UUID,
    payload: PreAuthDecisionUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> PreAuthRequestResponse:
    return InsuranceActions(db, hospital_id, current_user).update_pre_auth_decision(pre_auth_id, payload)


@router.post("/pre-auths/{pre_auth_id}/queries", response_model=PreAuthQueryResponse, status_code=status.HTTP_201_CREATED)
def raise_pre_auth_query(
    pre_auth_id: UUID,
    payload: PreAuthQueryCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> PreAuthQueryResponse:
    return InsuranceActions(db, hospital_id, current_user).raise_pre_auth_query(pre_auth_id, payload)


@router.post("/pre-auth-queries/{query_id}/respond", response_model=PreAuthQueryResponse)
def respond_pre_auth_query(
    query_id: UUID,
    payload: PreAuthQueryRespond,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> PreAuthQueryResponse:
    return InsuranceActions(db, hospital_id, current_user).respond_pre_auth_query(query_id, payload)


# ---------------------------------------------------------------------------
# Feature 27: Claim Preparation
# ---------------------------------------------------------------------------

@router.post("/claims", response_model=ClaimDossierResponse, status_code=status.HTTP_201_CREATED)
def create_claim_dossier(
    payload: ClaimDossierCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimDossierResponse:
    return InsuranceActions(db, hospital_id, current_user).create_claim_dossier(payload)


@router.get("/claims", response_model=list[ClaimDossierResponse])
def get_claims(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[ClaimDossierResponse]:
    return InsuranceActions(db, hospital_id, current_user).get_claims(limit=limit)


@router.get("/claims/{claim_id}", response_model=ClaimDossierResponse)
def get_claim(
    claim_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimDossierResponse:
    return InsuranceActions(db, hospital_id, current_user).get_claim(claim_id)


@router.patch("/claims/{claim_id}/verify-checklist", response_model=ClaimDossierResponse)
def verify_claim_checklist(
    claim_id: UUID,
    payload: ClaimDossierVerify,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimDossierResponse:
    return InsuranceActions(db, hospital_id, current_user).verify_claim_checklist(claim_id, payload)


# ---------------------------------------------------------------------------
# Feature 28: Claim Submission Tracking
# ---------------------------------------------------------------------------

@router.post("/claims/{claim_id}/submit", response_model=ClaimSubmissionDetailResponse, status_code=status.HTTP_201_CREATED)
def submit_claim(
    claim_id: UUID,
    payload: ClaimSubmissionCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimSubmissionDetailResponse:
    return InsuranceActions(db, hospital_id, current_user).submit_claim(claim_id, payload)


@router.patch("/submissions/{submission_id}/acknowledge", response_model=ClaimSubmissionDetailResponse)
def acknowledge_submission(
    submission_id: UUID,
    payload: ClaimSubmissionAcknowledge,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimSubmissionDetailResponse:
    return InsuranceActions(db, hospital_id, current_user).acknowledge_submission(submission_id, payload)


@router.post("/submissions/{submission_id}/queries", response_model=ClaimSubmissionDetailResponse)
def raise_submission_query(
    submission_id: UUID,
    payload: ClaimSubmissionQuery,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimSubmissionDetailResponse:
    return InsuranceActions(db, hospital_id, current_user).raise_submission_query(submission_id, payload)


@router.post("/submissions/{submission_id}/respond", response_model=ClaimSubmissionDetailResponse)
def respond_submission_query(
    submission_id: UUID,
    payload: ClaimSubmissionResponse,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimSubmissionDetailResponse:
    return InsuranceActions(db, hospital_id, current_user).respond_submission_query(submission_id, payload)


@router.patch("/submissions/{submission_id}/decision", response_model=ClaimSubmissionDetailResponse)
def record_submission_decision(
    submission_id: UUID,
    payload: ClaimSubmissionDecision,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimSubmissionDetailResponse:
    return InsuranceActions(db, hospital_id, current_user).record_submission_decision(submission_id, payload)


# ---------------------------------------------------------------------------
# Feature 29: Rejection & Resubmission
# ---------------------------------------------------------------------------

@router.post("/claims/{claim_id}/dispute", response_model=ClaimDisputeResponse, status_code=status.HTTP_201_CREATED)
def dispute_claim_rejection(
    claim_id: UUID,
    payload: ClaimDisputeCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimDisputeResponse:
    return InsuranceActions(db, hospital_id, current_user).dispute_claim_rejection(claim_id, payload)


@router.post("/disputes/{dispute_id}/resubmit", response_model=ClaimDisputeResponse)
def resubmit_disputed_claim(
    dispute_id: UUID,
    payload: ClaimDisputeResubmit,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ClaimDisputeResponse:
    return InsuranceActions(db, hospital_id, current_user).resubmit_disputed_claim(dispute_id, payload)


# ---------------------------------------------------------------------------
@router.get("/settlements", response_model=list[CashlessSettlementResponse])
def list_cashless_settlements(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[CashlessSettlementResponse]:
    records = InsuranceRepository(db, hospital_id).list_settlements_for_hospital(limit=limit)
    return [CashlessSettlementResponse.model_validate(r) for r in records]


@router.post("/settlements", response_model=CashlessSettlementResponse, status_code=status.HTTP_201_CREATED)
def settle_cashless_claim(
    payload: CashlessSettlementCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> CashlessSettlementResponse:
    return InsuranceActions(db, hospital_id, current_user).settle_cashless_claim(payload)


# ---------------------------------------------------------------------------
# Feature 31: TPA Performance Report
# ---------------------------------------------------------------------------

@router.get("/reports/tpa-performance", response_model=TPAPerformanceReportResponse)
def get_tpa_performance_report(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> TPAPerformanceReportResponse:
    return InsuranceActions(db, hospital_id, current_user).get_tpa_performance_report()
