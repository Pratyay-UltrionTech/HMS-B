"""
Target-native Pydantic contracts for Insurance & TPA Lifecycle domain.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from hms_migration.modules.insurance.entities.insurance_entities import (
    ClaimStatus,
    DisputeStatus,
    PolicyStatus,
    PreAuthStatus,
    PreAuthType,
    SettlementStatus,
    SubmissionMode,
    SubmissionStatus,
)


# ---------------------------------------------------------------------------
# Feature 23: Patient Policy & Admission Policy Link
# ---------------------------------------------------------------------------

class PatientPolicyCreate(BaseModel):
    patient_id: UUID
    provider_id: UUID
    policy_number: str = Field(min_length=1, max_length=64)
    member_id: str = Field(min_length=1, max_length=64)
    corporate_name: str | None = None
    sum_insured: float = Field(ge=0.0)
    balance_sum_insured: float = Field(ge=0.0)
    valid_from: date
    valid_to: date
    co_pay_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    room_rent_capping_per_day: float | None = Field(default=None, ge=0.0)
    icu_capping_per_day: float | None = Field(default=None, ge=0.0)


class PatientPolicyResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_id: UUID
    provider_id: UUID
    policy_number: str
    member_id: str
    corporate_name: str | None
    sum_insured: float
    balance_sum_insured: float
    valid_from: date
    valid_to: date
    co_pay_percent: float
    room_rent_capping_per_day: float | None
    icu_capping_per_day: float | None
    status: PolicyStatus
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AdmissionPolicyLinkCreate(BaseModel):
    admission_id: UUID
    patient_policy_id: UUID
    is_primary: bool = True
    notes: str | None = None


class AdmissionPolicyResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    admission_id: UUID
    patient_policy_id: UUID
    is_primary: bool
    pre_auth_status: str
    approved_amount: float
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 24: Eligibility Verification
# ---------------------------------------------------------------------------

class EligibilityCheckCreate(BaseModel):
    patient_policy_id: UUID
    admission_id: UUID | None = None
    verification_source: str = Field(default="portal", max_length=64)
    is_eligible: bool
    reference_number: str | None = Field(default=None, max_length=64)
    coverage_details: str | None = None
    remarks: str | None = None


class EligibilityCheckResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    patient_policy_id: UUID
    admission_id: UUID | None
    check_date: datetime
    is_eligible: bool
    verification_source: str
    verified_by: str
    reference_number: str | None
    coverage_details: str | None
    remarks: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 25 & 26: Pre-Auth Request & Query Tracking
# ---------------------------------------------------------------------------

class PreAuthRequestCreate(BaseModel):
    admission_policy_id: UUID
    request_type: PreAuthType = PreAuthType.initial
    provisional_diagnosis: str = Field(min_length=1)
    treatment_plan: str = Field(min_length=1)
    estimated_cost: float = Field(gt=0.0)
    requested_amount: float = Field(gt=0.0)


class PreAuthDecisionUpdate(BaseModel):
    status: PreAuthStatus
    approved_amount: float = Field(default=0.0, ge=0.0)
    approval_letter_ref: str | None = None
    denial_reason: str | None = None


class PreAuthQueryCreate(BaseModel):
    query_type: str = Field(default="clinical_clarification", max_length=64)
    query_text: str = Field(min_length=1)


class PreAuthQueryRespond(BaseModel):
    response_text: str = Field(min_length=1)


class PreAuthQueryResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    pre_auth_id: UUID
    query_type: str
    query_text: str
    query_raised_at: datetime
    response_text: str | None
    response_submitted_at: datetime | None
    status: str

    model_config = {"from_attributes": True}


class PreAuthRequestResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    admission_policy_id: UUID
    pre_auth_number: str
    request_type: PreAuthType
    provisional_diagnosis: str
    treatment_plan: str
    estimated_cost: float
    requested_amount: float
    approved_amount: float
    status: PreAuthStatus
    denial_reason: str | None
    approval_letter_ref: str | None
    submitted_at: datetime | None
    decision_at: datetime | None
    created_at: datetime
    updated_at: datetime
    queries: list[PreAuthQueryResponse] = []

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 27: Claim Preparation
# ---------------------------------------------------------------------------

class ClaimDossierCreate(BaseModel):
    admission_policy_id: UUID
    billing_invoice_id: UUID
    pre_auth_id: UUID | None = None
    claimed_amount: float = Field(gt=0.0)
    checklist_verified: bool = False
    discharge_summary_verified: bool = False
    investigation_reports_verified: bool = False
    final_bill_verified: bool = False


class ClaimDossierVerify(BaseModel):
    checklist_verified: bool = True
    discharge_summary_verified: bool = True
    investigation_reports_verified: bool = True
    final_bill_verified: bool = True


class ClaimDossierResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    claim_number: str
    admission_policy_id: UUID
    billing_invoice_id: UUID
    pre_auth_id: UUID | None
    claimed_amount: float
    checklist_verified: bool
    discharge_summary_verified: bool
    investigation_reports_verified: bool
    final_bill_verified: bool
    status: ClaimStatus
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 28: Claim Submission Tracking
# ---------------------------------------------------------------------------

class ClaimSubmissionCreate(BaseModel):
    submission_mode: SubmissionMode = SubmissionMode.portal
    tracking_reference: str | None = None
    courier_service_name: str | None = None


class ClaimSubmissionAcknowledge(BaseModel):
    tpa_ack_number: str = Field(min_length=1, max_length=64)
    tpa_ack_date: datetime | None = None


class ClaimSubmissionQuery(BaseModel):
    query_details: str = Field(min_length=1)


class ClaimSubmissionResponse(BaseModel):
    hospital_response: str = Field(min_length=1)


class ClaimSubmissionDecision(BaseModel):
    status: SubmissionStatus
    settled_amount: float = Field(default=0.0, ge=0.0)
    insurer_decision: str = Field(min_length=1, max_length=64)


class ClaimSubmissionDetailResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    claim_id: UUID
    submission_date: datetime
    submission_mode: SubmissionMode
    tracking_reference: str | None
    courier_service_name: str | None
    tpa_ack_number: str | None
    tpa_ack_date: datetime | None
    status: SubmissionStatus
    query_details: str | None
    query_date: datetime | None
    hospital_response: str | None
    response_date: datetime | None
    insurer_decision: str | None
    settled_amount: float
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 29: Rejection & Resubmission
# ---------------------------------------------------------------------------

class ClaimDisputeCreate(BaseModel):
    submission_id: UUID | None = None
    denial_code: str = Field(min_length=1, max_length=64)
    denial_reason: str = Field(min_length=1)
    dispute_rationale: str = Field(min_length=1)


class ClaimDisputeResubmit(BaseModel):
    resubmission_reference: str = Field(min_length=1, max_length=128)


class ClaimDisputeResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    claim_id: UUID
    submission_id: UUID | None
    denial_code: str
    denial_reason: str
    dispute_rationale: str
    status: DisputeStatus
    resubmission_reference: str | None
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 30: Cashless Settlement
# ---------------------------------------------------------------------------

class CashlessSettlementCreate(BaseModel):
    claim_id: UUID
    approved_by_insurer_amount: float = Field(ge=0.0)
    disallowed_deduction_amount: float = Field(default=0.0, ge=0.0)
    tds_amount: float = Field(default=0.0, ge=0.0)
    patient_copay_payable: float = Field(default=0.0, ge=0.0)
    payment_reference: str | None = None
    settled_date: date | None = None


class CashlessSettlementResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    claim_id: UUID
    billing_invoice_id: UUID
    settlement_number: str
    total_claim_amount: float
    approved_by_insurer_amount: float
    disallowed_deduction_amount: float
    tds_amount: float
    net_payable_by_insurer: float
    patient_copay_payable: float
    settlement_status: SettlementStatus
    payment_reference: str | None
    settled_date: date
    billing_payment_id: UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 31: TPA Performance Report
# ---------------------------------------------------------------------------

class TPAPerformanceSummary(BaseModel):
    provider_id: UUID
    provider_code: str
    provider_name: str
    category: str
    total_claims: int
    settled_claims: int
    rejected_claims: int
    pending_claims: int
    approval_rate_percent: float
    total_claimed_amount: float
    total_settled_amount: float
    total_deduction_amount: float
    avg_deduction_percent: float
    avg_turnaround_days: float


class TPAPerformanceReportResponse(BaseModel):
    hospital_id: UUID
    generated_at: datetime
    providers: list[TPAPerformanceSummary]
