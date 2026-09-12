"""
Target-native SQLAlchemy entities for Insurance & TPA Lifecycle.

Features covered:
- Feature 22: Insurance Company & TPA Master (Canonical master: InsuranceProvider in masters module)
- Feature 23: Insurance Policy Details (Patient Policy + Admission Policy Linkage)
- Feature 24: Eligibility Verification (InsuranceEligibilityCheck)
- Feature 25: Pre-Auth Request & Estimation (InsurancePreAuthRequest)
- Feature 26: Approval Tracking & Query Management (InsurancePreAuthQuery)
- Feature 27: Claim Preparation (InsuranceClaimDossier - strictly workflow layer referencing billing_invoices)
- Feature 28: Claim Submission Tracking (InsuranceClaimSubmission)
- Feature 29: Rejection & Resubmission (InsuranceClaimDispute)
- Feature 30: Cashless Settlement (InsuranceCashlessSettlement - records split & links to billing_payments)
- Feature 31: TPA Performance Reporting (Aggregate view across providers and claim outcomes)
"""

from __future__ import annotations

from datetime import date, datetime
import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base

if TYPE_CHECKING:
    from hms_migration.modules.patients.entities.patient import Patient
    from hms_migration.modules.inpatient.entities.admission import Admission
    from hms_migration.modules.masters.entities.insurance_entities import InsuranceProvider
    from hms_migration.modules.billing.entities.billing_entities import BillingInvoice, BillingPayment


class PolicyStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    exhausted = "exhausted"
    cancelled = "cancelled"


class PreAuthStatus(str, enum.Enum):
    draft = "draft"
    submitted = "submitted"
    query_raised = "query_raised"
    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"


class PreAuthType(str, enum.Enum):
    initial = "initial"
    enhancement = "enhancement"
    emergency = "emergency"


class ClaimStatus(str, enum.Enum):
    draft = "draft"
    prepared = "prepared"
    submitted = "submitted"
    settled = "settled"
    rejected = "rejected"
    disputed = "disputed"


class SubmissionMode(str, enum.Enum):
    portal = "portal"
    physical_courier = "physical_courier"
    email = "email"


class SubmissionStatus(str, enum.Enum):
    submitted = "submitted"
    acknowledged = "acknowledged"
    under_process = "under_process"
    query_raised = "query_raised"
    settled = "settled"
    rejected = "rejected"


class SettlementStatus(str, enum.Enum):
    pending = "pending"
    settled = "settled"
    partially_settled = "partially_settled"


class DisputeStatus(str, enum.Enum):
    filed = "filed"
    under_review = "under_review"
    upheld = "upheld"
    overturned = "overturned"
    resubmitted = "resubmitted"


class InsurancePatientPolicy(Base):
    """Patient-level insurance policy record (Feature 23)."""

    __tablename__ = "insurance_patient_policies"
    __table_args__ = (
        UniqueConstraint("hospital_id", "policy_number", "member_id", name="uq_patient_policy_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    policy_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    member_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    corporate_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sum_insured: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    balance_sum_insured: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)
    co_pay_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    room_rent_capping_per_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    icu_capping_per_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[PolicyStatus] = mapped_column(
        Enum(PolicyStatus, name="policy_status"), nullable=False, default=PolicyStatus.active
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    admission_links: Mapped[list[InsuranceAdmissionPolicy]] = relationship(
        "InsuranceAdmissionPolicy", back_populates="policy", cascade="all, delete-orphan"
    )


class InsuranceAdmissionPolicy(Base):
    """Encounter/Admission-linked policy record (Correction #4)."""

    __tablename__ = "insurance_admission_policies"
    __table_args__ = (
        UniqueConstraint("hospital_id", "admission_id", "patient_policy_id", name="uq_admission_policy"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_patient_policies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pre_auth_status: Mapped[str] = mapped_column(String(32), default="not_required", nullable=False)
    approved_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    policy: Mapped[InsurancePatientPolicy] = relationship("InsurancePatientPolicy", back_populates="admission_links")
    pre_auth_requests: Mapped[list[InsurancePreAuthRequest]] = relationship(
        "InsurancePreAuthRequest", back_populates="admission_policy", cascade="all, delete-orphan"
    )
    claims: Mapped[list[InsuranceClaimDossier]] = relationship(
        "InsuranceClaimDossier", back_populates="admission_policy", cascade="all, delete-orphan"
    )


class InsuranceEligibilityCheck(Base):
    """Eligibility check execution record (Feature 24)."""

    __tablename__ = "insurance_eligibility_checks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_patient_policies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    check_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verification_source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    verified_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reference_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coverage_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InsurancePreAuthRequest(Base):
    """Pre-authorization request and tracking (Features 25 & 26)."""

    __tablename__ = "insurance_pre_auth_requests"
    __table_args__ = (
        UniqueConstraint("hospital_id", "pre_auth_number", name="uq_insurance_pre_auth_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_admission_policies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pre_auth_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    request_type: Mapped[PreAuthType] = mapped_column(
        Enum(PreAuthType, name="pre_auth_type"), nullable=False, default=PreAuthType.initial
    )
    provisional_diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    treatment_plan: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    requested_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    approved_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[PreAuthStatus] = mapped_column(
        Enum(PreAuthStatus, name="pre_auth_status"), nullable=False, default=PreAuthStatus.draft, index=True
    )
    denial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_letter_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    admission_policy: Mapped[InsuranceAdmissionPolicy] = relationship("InsuranceAdmissionPolicy", back_populates="pre_auth_requests")
    queries: Mapped[list[InsurancePreAuthQuery]] = relationship(
        "InsurancePreAuthQuery", back_populates="pre_auth", cascade="all, delete-orphan"
    )


class InsurancePreAuthQuery(Base):
    """Query raised by TPA / Insurer regarding pre-auth (Feature 26)."""

    __tablename__ = "insurance_pre_auth_queries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pre_auth_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_pre_auth_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    query_type: Mapped[str] = mapped_column(String(64), default="clinical_clarification", nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    pre_auth: Mapped[InsurancePreAuthRequest] = relationship("InsurancePreAuthRequest", back_populates="queries")


class InsuranceClaimDossier(Base):
    """Insurance claim preparation dossier over existing billing invoice (Feature 27)."""

    __tablename__ = "insurance_claim_dossiers"
    __table_args__ = (
        UniqueConstraint("hospital_id", "claim_number", name="uq_insurance_claim_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    claim_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    admission_policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_admission_policies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    billing_invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    pre_auth_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_pre_auth_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claimed_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    checklist_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discharge_summary_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    investigation_reports_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    final_bill_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[ClaimStatus] = mapped_column(
        Enum(ClaimStatus, name="claim_status"), nullable=False, default=ClaimStatus.draft, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    admission_policy: Mapped[InsuranceAdmissionPolicy] = relationship("InsuranceAdmissionPolicy", back_populates="claims")
    submissions: Mapped[list[InsuranceClaimSubmission]] = relationship(
        "InsuranceClaimSubmission", back_populates="claim", cascade="all, delete-orphan"
    )
    disputes: Mapped[list[InsuranceClaimDispute]] = relationship(
        "InsuranceClaimDispute", back_populates="claim", cascade="all, delete-orphan"
    )
    settlement: Mapped[InsuranceCashlessSettlement | None] = relationship(
        "InsuranceCashlessSettlement", back_populates="claim", uselist=False
    )


class InsuranceClaimSubmission(Base):
    """Submission and courier/acknowledgment tracking record (Feature 28)."""

    __tablename__ = "insurance_claim_submissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_claim_dossiers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    submission_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    submission_mode: Mapped[SubmissionMode] = mapped_column(
        Enum(SubmissionMode, name="submission_mode"), nullable=False, default=SubmissionMode.portal
    )
    tracking_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    courier_service_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tpa_ack_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tpa_ack_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus, name="submission_status"), nullable=False, default=SubmissionStatus.submitted
    )
    query_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hospital_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    insurer_decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    settled_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    claim: Mapped[InsuranceClaimDossier] = relationship("InsuranceClaimDossier", back_populates="submissions")


class InsuranceClaimDispute(Base):
    """Claim denial dispute and resubmission tracking (Feature 29)."""

    __tablename__ = "insurance_claim_disputes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_claim_dossiers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_claim_submissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    denial_code: Mapped[str] = mapped_column(String(64), nullable=False)
    denial_reason: Mapped[str] = mapped_column(Text, nullable=False)
    dispute_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[DisputeStatus] = mapped_column(
        Enum(DisputeStatus, name="dispute_status"), nullable=False, default=DisputeStatus.filed
    )
    resubmission_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    claim: Mapped[InsuranceClaimDossier] = relationship("InsuranceClaimDossier", back_populates="disputes")


class InsuranceCashlessSettlement(Base):
    """Cashless settlement recording insurer vs patient breakdown and linking to billing payment (Feature 30)."""

    __tablename__ = "insurance_cashless_settlements"
    __table_args__ = (
        UniqueConstraint("hospital_id", "settlement_number", name="uq_insurance_settlement_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insurance_claim_dossiers.id", ondelete="CASCADE"), nullable=False, index=True, unique=True
    )
    billing_invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    settlement_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    total_claim_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    approved_by_insurer_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    disallowed_deduction_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tds_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    net_payable_by_insurer: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    patient_copay_payable: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    settlement_status: Mapped[SettlementStatus] = mapped_column(
        Enum(SettlementStatus, name="settlement_status"), nullable=False, default=SettlementStatus.pending
    )
    payment_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    settled_date: Mapped[date] = mapped_column(Date, nullable=False)
    billing_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_payments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    claim: Mapped[InsuranceClaimDossier] = relationship("InsuranceClaimDossier", back_populates="settlement")
