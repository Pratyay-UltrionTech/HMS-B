"""
Business logic actions for Insurance & TPA Lifecycle.

Features covered:
- Feature 23: Insurance Policy Details & Admission Policy Linkage (Correction #4)
- Feature 24: Eligibility Verification
- Feature 25 & 26: Pre-Auth Request, Approval Tracking & Queries
- Feature 27 & 28: Claim Preparation & Submission Tracking (Correction #5)
- Feature 29: Rejection & Resubmission Workflow
- Feature 30: Cashless Settlement with Billing Payment Integration (Correction #5)
- Feature 31: TPA Performance Reporting
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
import uuid
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from hms_migration.modules.billing.entities.billing_entities import (
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentMethod,
)
from hms_migration.modules.inpatient.entities.admission import Admission
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
    TPAPerformanceSummary,
)
from hms_migration.modules.insurance.db.insurance_repository import InsuranceRepository
from hms_migration.modules.insurance.entities.insurance_entities import (
    ClaimStatus,
    DisputeStatus,
    InsuranceAdmissionPolicy,
    InsuranceCashlessSettlement,
    InsuranceClaimDispute,
    InsuranceClaimDossier,
    InsuranceClaimSubmission,
    InsuranceEligibilityCheck,
    InsurancePatientPolicy,
    InsurancePreAuthQuery,
    InsurancePreAuthRequest,
    PolicyStatus,
    PreAuthStatus,
    SettlementStatus,
    SubmissionStatus,
)
from hms_migration.modules.masters.entities.insurance_entities import InsuranceProvider
from hms_migration.modules.patients.entities.patient import Patient


class InsuranceActions:
    def __init__(self, db: Session, hospital_id: UUID, actor: dict[str, Any] | None = None) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.actor = actor or {}
        self.repo = InsuranceRepository(db, hospital_id)

    # -----------------------------------------------------------------------
    # Feature 23: Insurance Policy Details & Admission Policy Link
    # -----------------------------------------------------------------------

    def create_patient_policy(self, payload: PatientPolicyCreate) -> PatientPolicyResponse:
        # Tenant checks
        patient = self.db.query(Patient).filter(
            Patient.id == payload.patient_id, Patient.hospital_id == self.hospital_id
        ).first()
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        provider = self.db.query(InsuranceProvider).filter(
            InsuranceProvider.id == payload.provider_id, InsuranceProvider.hospital_id == self.hospital_id
        ).first()
        if not provider:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insurance provider not found")

        policy = InsurancePatientPolicy(
            hospital_id=self.hospital_id,
            patient_id=payload.patient_id,
            provider_id=payload.provider_id,
            policy_number=payload.policy_number,
            member_id=payload.member_id,
            corporate_name=payload.corporate_name,
            sum_insured=payload.sum_insured,
            balance_sum_insured=payload.balance_sum_insured,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            co_pay_percent=payload.co_pay_percent,
            room_rent_capping_per_day=payload.room_rent_capping_per_day,
            icu_capping_per_day=payload.icu_capping_per_day,
            status=PolicyStatus.active,
        )
        self.db.add(policy)
        self.db.commit()
        self.db.refresh(policy)
        return PatientPolicyResponse.model_validate(policy)

    def link_admission_policy(self, payload: AdmissionPolicyLinkCreate) -> AdmissionPolicyResponse:
        admission = self.db.query(Admission).filter(
            Admission.id == payload.admission_id, Admission.hospital_id == self.hospital_id
        ).first()
        if not admission:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        patient_policy = self.repo.get_patient_policy_by_id(payload.patient_policy_id)
        if not patient_policy:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient policy not found")

        link = InsuranceAdmissionPolicy(
            hospital_id=self.hospital_id,
            admission_id=payload.admission_id,
            patient_policy_id=payload.patient_policy_id,
            is_primary=payload.is_primary,
            notes=payload.notes,
        )
        self.db.add(link)
        self.db.commit()
        self.db.refresh(link)
        return AdmissionPolicyResponse.model_validate(link)

    # -----------------------------------------------------------------------
    # Feature 24: Eligibility Verification
    # -----------------------------------------------------------------------

    def verify_eligibility(self, payload: EligibilityCheckCreate) -> EligibilityCheckResponse:
        policy = self.repo.get_patient_policy_by_id(payload.patient_policy_id)
        if not policy:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")

        if payload.admission_id:
            admission = self.db.query(Admission).filter(
                Admission.id == payload.admission_id, Admission.hospital_id == self.hospital_id
            ).first()
            if not admission:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        actor_name = str(self.actor.get("email") or self.actor.get("username") or "Staff")

        check = InsuranceEligibilityCheck(
            hospital_id=self.hospital_id,
            patient_policy_id=payload.patient_policy_id,
            admission_id=payload.admission_id,
            is_eligible=payload.is_eligible,
            verification_source=payload.verification_source,
            verified_by=actor_name,
            reference_number=payload.reference_number,
            coverage_details=payload.coverage_details,
            remarks=payload.remarks,
        )
        self.db.add(check)
        self.db.commit()
        self.db.refresh(check)
        return EligibilityCheckResponse.model_validate(check)

    # -----------------------------------------------------------------------
    # Feature 25 & 26: Pre-Auth Request, Decisions & Queries
    # -----------------------------------------------------------------------

    def create_pre_auth_request(self, payload: PreAuthRequestCreate) -> PreAuthRequestResponse:
        admission_policy = self.repo.get_admission_policy_by_id(payload.admission_policy_id)
        if not admission_policy:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission policy link not found")

        pre_auth_num = f"PA-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(timezone.utc)

        pre_auth = InsurancePreAuthRequest(
            hospital_id=self.hospital_id,
            admission_policy_id=payload.admission_policy_id,
            pre_auth_number=pre_auth_num,
            request_type=payload.request_type,
            provisional_diagnosis=payload.provisional_diagnosis,
            treatment_plan=payload.treatment_plan,
            estimated_cost=payload.estimated_cost,
            requested_amount=payload.requested_amount,
            status=PreAuthStatus.submitted,
            submitted_at=now,
        )
        self.db.add(pre_auth)
        admission_policy.pre_auth_status = "pending"
        self.db.commit()
        self.db.refresh(pre_auth)
        return PreAuthRequestResponse.model_validate(pre_auth)

    def update_pre_auth_decision(self, pre_auth_id: UUID, payload: PreAuthDecisionUpdate) -> PreAuthRequestResponse:
        pre_auth = self.repo.get_pre_auth_by_id(pre_auth_id)
        if not pre_auth:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pre-auth request not found")

        # State transition validation (Section 15)
        valid_targets = {PreAuthStatus.approved, PreAuthStatus.rejected, PreAuthStatus.cancelled}
        if payload.status not in valid_targets:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid transition target status {payload.status}",
            )

        now = datetime.now(timezone.utc)
        pre_auth.status = payload.status
        pre_auth.approved_amount = payload.approved_amount if payload.status == PreAuthStatus.approved else 0.0
        pre_auth.approval_letter_ref = payload.approval_letter_ref
        pre_auth.denial_reason = payload.denial_reason
        pre_auth.decision_at = now

        # Update linked admission policy pre-auth status & approved amount
        admission_policy = pre_auth.admission_policy
        if admission_policy:
            if payload.status == PreAuthStatus.approved:
                admission_policy.pre_auth_status = "approved"
                admission_policy.approved_amount = payload.approved_amount
            elif payload.status == PreAuthStatus.rejected:
                admission_policy.pre_auth_status = "rejected"
            elif payload.status == PreAuthStatus.cancelled:
                admission_policy.pre_auth_status = "cancelled"

        self.db.commit()
        self.db.refresh(pre_auth)
        return PreAuthRequestResponse.model_validate(pre_auth)

    def raise_pre_auth_query(self, pre_auth_id: UUID, payload: PreAuthQueryCreate) -> PreAuthQueryResponse:
        pre_auth = self.repo.get_pre_auth_by_id(pre_auth_id)
        if not pre_auth:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pre-auth request not found")

        query = InsurancePreAuthQuery(
            hospital_id=self.hospital_id,
            pre_auth_id=pre_auth.id,
            query_type=payload.query_type,
            query_text=payload.query_text,
            status="pending",
        )
        pre_auth.status = PreAuthStatus.query_raised
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)
        return PreAuthQueryResponse.model_validate(query)

    def respond_pre_auth_query(self, query_id: UUID, payload: PreAuthQueryRespond) -> PreAuthQueryResponse:
        query = self.db.query(InsurancePreAuthQuery).filter(
            InsurancePreAuthQuery.id == query_id,
            InsurancePreAuthQuery.hospital_id == self.hospital_id,
        ).first()
        if not query:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Query not found")

        now = datetime.now(timezone.utc)
        query.response_text = payload.response_text
        query.response_submitted_at = now
        query.status = "answered"

        # Update parent pre-auth status back to submitted
        pre_auth = self.repo.get_pre_auth_by_id(query.pre_auth_id)
        if pre_auth and pre_auth.status == PreAuthStatus.query_raised:
            pre_auth.status = PreAuthStatus.submitted

        self.db.commit()
        self.db.refresh(query)
        return PreAuthQueryResponse.model_validate(query)

    # -----------------------------------------------------------------------
    # Feature 27: Claim Preparation (Dossier)
    # -----------------------------------------------------------------------

    def create_claim_dossier(self, payload: ClaimDossierCreate) -> ClaimDossierResponse:
        admission_policy = self.repo.get_admission_policy_by_id(payload.admission_policy_id)
        if not admission_policy:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission policy not found")

        # Reuse existing billing invoice (Correction #5)
        invoice = self.db.query(BillingInvoice).filter(
            BillingInvoice.id == payload.billing_invoice_id,
            BillingInvoice.hospital_id == self.hospital_id,
        ).first()
        if not invoice:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Billing invoice not found")

        claim_num = f"CLM-{uuid.uuid4().hex[:8].upper()}"

        dossier = InsuranceClaimDossier(
            hospital_id=self.hospital_id,
            claim_number=claim_num,
            admission_policy_id=payload.admission_policy_id,
            billing_invoice_id=payload.billing_invoice_id,
            pre_auth_id=payload.pre_auth_id,
            claimed_amount=payload.claimed_amount,
            checklist_verified=payload.checklist_verified,
            discharge_summary_verified=payload.discharge_summary_verified,
            investigation_reports_verified=payload.investigation_reports_verified,
            final_bill_verified=payload.final_bill_verified,
            status=ClaimStatus.draft,
        )
        self.db.add(dossier)
        self.db.commit()
        self.db.refresh(dossier)
        return ClaimDossierResponse.model_validate(dossier)

    def verify_claim_checklist(self, claim_id: UUID, payload: ClaimDossierVerify) -> ClaimDossierResponse:
        claim = self.repo.get_claim_by_id(claim_id)
        if not claim:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")

        claim.checklist_verified = payload.checklist_verified
        claim.discharge_summary_verified = payload.discharge_summary_verified
        claim.investigation_reports_verified = payload.investigation_reports_verified
        claim.final_bill_verified = payload.final_bill_verified

        if (
            claim.checklist_verified
            and claim.discharge_summary_verified
            and claim.investigation_reports_verified
            and claim.final_bill_verified
        ):
            claim.status = ClaimStatus.prepared

        self.db.commit()
        self.db.refresh(claim)
        return ClaimDossierResponse.model_validate(claim)

    # -----------------------------------------------------------------------
    # Feature 28: Claim Submission Tracking
    # -----------------------------------------------------------------------

    def submit_claim(self, claim_id: UUID, payload: ClaimSubmissionCreate) -> ClaimSubmissionDetailResponse:
        claim = self.repo.get_claim_by_id(claim_id)
        if not claim:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")

        if claim.status not in (ClaimStatus.prepared, ClaimStatus.draft):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot submit claim in status '{claim.status}'. Must be prepared or draft.",
            )

        now = datetime.now(timezone.utc)
        sub = InsuranceClaimSubmission(
            hospital_id=self.hospital_id,
            claim_id=claim.id,
            submission_date=now,
            submission_mode=payload.submission_mode,
            tracking_reference=payload.tracking_reference,
            courier_service_name=payload.courier_service_name,
            status=SubmissionStatus.submitted,
        )
        claim.status = ClaimStatus.submitted
        self.db.add(sub)
        self.db.commit()
        self.db.refresh(sub)
        return ClaimSubmissionDetailResponse.model_validate(sub)

    def acknowledge_submission(self, submission_id: UUID, payload: ClaimSubmissionAcknowledge) -> ClaimSubmissionDetailResponse:
        sub = self.repo.get_submission_by_id(submission_id)
        if not sub:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

        now = datetime.now(timezone.utc)
        sub.tpa_ack_number = payload.tpa_ack_number
        sub.tpa_ack_date = payload.tpa_ack_date or now
        sub.status = SubmissionStatus.acknowledged
        self.db.commit()
        self.db.refresh(sub)
        return ClaimSubmissionDetailResponse.model_validate(sub)

    def raise_submission_query(self, submission_id: UUID, payload: ClaimSubmissionQuery) -> ClaimSubmissionDetailResponse:
        sub = self.repo.get_submission_by_id(submission_id)
        if not sub:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

        now = datetime.now(timezone.utc)
        sub.query_details = payload.query_details
        sub.query_date = now
        sub.status = SubmissionStatus.query_raised
        self.db.commit()
        self.db.refresh(sub)
        return ClaimSubmissionDetailResponse.model_validate(sub)

    def respond_submission_query(self, submission_id: UUID, payload: ClaimSubmissionResponse) -> ClaimSubmissionDetailResponse:
        sub = self.repo.get_submission_by_id(submission_id)
        if not sub:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

        now = datetime.now(timezone.utc)
        sub.hospital_response = payload.hospital_response
        sub.response_date = now
        sub.status = SubmissionStatus.under_process
        self.db.commit()
        self.db.refresh(sub)
        return ClaimSubmissionDetailResponse.model_validate(sub)

    def record_submission_decision(self, submission_id: UUID, payload: ClaimSubmissionDecision) -> ClaimSubmissionDetailResponse:
        sub = self.repo.get_submission_by_id(submission_id)
        if not sub:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

        sub.status = payload.status
        sub.settled_amount = payload.settled_amount
        sub.insurer_decision = payload.insurer_decision

        claim = self.repo.get_claim_by_id(sub.claim_id)
        if claim:
            if payload.status == SubmissionStatus.settled:
                claim.status = ClaimStatus.settled
            elif payload.status == SubmissionStatus.rejected:
                claim.status = ClaimStatus.rejected

        self.db.commit()
        self.db.refresh(sub)
        return ClaimSubmissionDetailResponse.model_validate(sub)

    # -----------------------------------------------------------------------
    # Feature 29: Rejection & Resubmission
    # -----------------------------------------------------------------------

    def dispute_claim_rejection(self, claim_id: UUID, payload: ClaimDisputeCreate) -> ClaimDisputeResponse:
        claim = self.repo.get_claim_by_id(claim_id)
        if not claim:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")

        if claim.status != ClaimStatus.rejected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only rejected claims can be disputed. Current status: '{claim.status}'.",
            )

        dispute = InsuranceClaimDispute(
            hospital_id=self.hospital_id,
            claim_id=claim.id,
            submission_id=payload.submission_id,
            denial_code=payload.denial_code,
            denial_reason=payload.denial_reason,
            dispute_rationale=payload.dispute_rationale,
            status=DisputeStatus.filed,
        )
        claim.status = ClaimStatus.disputed
        self.db.add(dispute)
        self.db.commit()
        self.db.refresh(dispute)
        return ClaimDisputeResponse.model_validate(dispute)

    def resubmit_disputed_claim(self, dispute_id: UUID, payload: ClaimDisputeResubmit) -> ClaimDisputeResponse:
        dispute = self.repo.get_dispute_by_id(dispute_id)
        if not dispute:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found")

        now = datetime.now(timezone.utc)
        dispute.status = DisputeStatus.resubmitted
        dispute.resubmission_reference = payload.resubmission_reference
        dispute.resolved_at = now

        claim = self.repo.get_claim_by_id(dispute.claim_id)
        if claim:
            claim.status = ClaimStatus.submitted

        self.db.commit()
        self.db.refresh(dispute)
        return ClaimDisputeResponse.model_validate(dispute)

    # -----------------------------------------------------------------------
    # Feature 30: Cashless Settlement (Reusing billing_payments & invoices)
    # -----------------------------------------------------------------------

    def settle_cashless_claim(self, payload: CashlessSettlementCreate) -> CashlessSettlementResponse:
        claim = self.repo.get_claim_by_id(payload.claim_id)
        if not claim:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")

        invoice = self.db.query(BillingInvoice).filter(
            BillingInvoice.id == claim.billing_invoice_id,
            BillingInvoice.hospital_id == self.hospital_id,
        ).first()
        if not invoice:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked billing invoice not found")

        existing_settlement = self.repo.get_settlement_by_claim_id(claim.id)
        if existing_settlement:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Claim {claim.claim_number} is already settled with settlement {existing_settlement.settlement_number}.",
            )

        settlement_num = f"SET-{uuid.uuid4().hex[:8].upper()}"
        net_insurer = payload.approved_by_insurer_amount - payload.tds_amount
        if net_insurer < 0:
            net_insurer = 0.0

        settled_dt = payload.settled_date or date.today()

        # Creates payment in canonical billing system (Correction #5)
        billing_payment = None
        if net_insurer > 0:
            actor_name = str(self.actor.get("email") or self.actor.get("username") or "Insurance System")
            billing_payment = BillingPayment(
                hospital_id=self.hospital_id,
                patient_id=invoice.patient_id,
                amount=net_insurer,
                payment_date=settled_dt,
                payment_method=BillingPaymentMethod.bank_transfer,
                notes=f"Cashless insurance settlement {settlement_num} for claim {claim.claim_number}",
                received_by_name=actor_name,
            )
            self.db.add(billing_payment)
            self.db.flush()

        settlement = InsuranceCashlessSettlement(
            hospital_id=self.hospital_id,
            claim_id=claim.id,
            billing_invoice_id=invoice.id,
            settlement_number=settlement_num,
            total_claim_amount=claim.claimed_amount,
            approved_by_insurer_amount=payload.approved_by_insurer_amount,
            disallowed_deduction_amount=payload.disallowed_deduction_amount,
            tds_amount=payload.tds_amount,
            net_payable_by_insurer=net_insurer,
            patient_copay_payable=payload.patient_copay_payable,
            settlement_status=SettlementStatus.settled,
            payment_reference=payload.payment_reference,
            settled_date=settled_dt,
            billing_payment_id=billing_payment.id if billing_payment else None,
        )
        self.db.add(settlement)
        claim.status = ClaimStatus.settled

        self.db.commit()
        self.db.refresh(settlement)
        return CashlessSettlementResponse.model_validate(settlement)

    # -----------------------------------------------------------------------
    # Feature 31: TPA Performance Report
    # -----------------------------------------------------------------------

    def get_tpa_performance_report(self) -> TPAPerformanceReportResponse:
        providers = self.db.query(InsuranceProvider).filter(
            InsuranceProvider.hospital_id == self.hospital_id
        ).all()

        summaries: list[TPAPerformanceSummary] = []
        now = datetime.now(timezone.utc)

        for prov in providers:
            # Query all claims associated with this provider through admission_policies -> patient_policies
            claims = (
                self.db.query(InsuranceClaimDossier)
                .join(InsuranceAdmissionPolicy, InsuranceClaimDossier.admission_policy_id == InsuranceAdmissionPolicy.id)
                .join(InsurancePatientPolicy, InsuranceAdmissionPolicy.patient_policy_id == InsurancePatientPolicy.id)
                .filter(
                    InsuranceClaimDossier.hospital_id == self.hospital_id,
                    InsurancePatientPolicy.provider_id == prov.id,
                )
                .all()
            )

            total_claims = len(claims)
            settled_claims = len([c for c in claims if c.status == ClaimStatus.settled])
            rejected_claims = len([c for c in claims if c.status == ClaimStatus.rejected])
            pending_claims = total_claims - (settled_claims + rejected_claims)

            total_claimed = sum(c.claimed_amount for c in claims)
            
            settlements = (
                self.db.query(InsuranceCashlessSettlement)
                .join(InsuranceClaimDossier, InsuranceCashlessSettlement.claim_id == InsuranceClaimDossier.id)
                .join(InsuranceAdmissionPolicy, InsuranceClaimDossier.admission_policy_id == InsuranceAdmissionPolicy.id)
                .join(InsurancePatientPolicy, InsuranceAdmissionPolicy.patient_policy_id == InsurancePatientPolicy.id)
                .filter(
                    InsuranceCashlessSettlement.hospital_id == self.hospital_id,
                    InsurancePatientPolicy.provider_id == prov.id,
                )
                .all()
            )

            total_settled = sum(s.approved_by_insurer_amount for s in settlements)
            total_deductions = sum(s.disallowed_deduction_amount for s in settlements)

            approval_rate = (settled_claims / total_claims * 100.0) if total_claims > 0 else 0.0
            avg_deduction_pct = (total_deductions / total_claimed * 100.0) if total_claimed > 0 else 0.0

            summaries.append(
                TPAPerformanceSummary(
                    provider_id=prov.id,
                    provider_code=prov.code,
                    provider_name=prov.name,
                    category=prov.category,
                    total_claims=total_claims,
                    settled_claims=settled_claims,
                    rejected_claims=rejected_claims,
                    pending_claims=pending_claims,
                    approval_rate_percent=round(approval_rate, 2),
                    total_claimed_amount=round(total_claimed, 2),
                    total_settled_amount=round(total_settled, 2),
                    total_deduction_amount=round(total_deductions, 2),
                    avg_deduction_percent=round(avg_deduction_pct, 2),
                    avg_turnaround_days=float(prov.pre_auth_sla_hours) / 24.0,
                )
            )

        return TPAPerformanceReportResponse(
            hospital_id=self.hospital_id,
            generated_at=now,
            providers=summaries,
        )

    # -----------------------------------------------------------------------
    # Query / Read Actions (Finding F-03: Architectural consistency API -> Action -> DB)
    # -----------------------------------------------------------------------

    def get_patient_policies(self, patient_id: UUID) -> list[PatientPolicyResponse]:
        policies = self.repo.get_patient_policies(patient_id)
        return [PatientPolicyResponse.model_validate(p) for p in policies]

    def get_admission_policies(self, admission_id: UUID) -> list[AdmissionPolicyResponse]:
        links = self.repo.get_admission_policies(admission_id)
        return [AdmissionPolicyResponse.model_validate(link) for link in links]

    def get_pre_auth_request(self, pre_auth_id: UUID) -> PreAuthRequestResponse:
        pre_auth = self.repo.get_pre_auth_by_id(pre_auth_id)
        if not pre_auth:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pre-auth request not found")
        return PreAuthRequestResponse.model_validate(pre_auth)

    def get_claims(self, limit: int = 100) -> list[ClaimDossierResponse]:
        claims = self.repo.get_claims_for_hospital(limit=limit)
        return [ClaimDossierResponse.model_validate(c) for c in claims]

    def get_claim(self, claim_id: UUID) -> ClaimDossierResponse:
        claim = self.repo.get_claim_by_id(claim_id)
        if not claim:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
        return ClaimDossierResponse.model_validate(claim)

