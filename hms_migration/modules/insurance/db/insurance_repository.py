"""
Database repository for Insurance & TPA Lifecycle.
All operations are hospital-tenant isolated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, selectinload

from hms_migration.modules.insurance.entities.insurance_entities import (
    InsuranceAdmissionPolicy,
    InsuranceCashlessSettlement,
    InsuranceClaimDispute,
    InsuranceClaimDossier,
    InsuranceClaimSubmission,
    InsuranceEligibilityCheck,
    InsurancePatientPolicy,
    InsurancePreAuthQuery,
    InsurancePreAuthRequest,
)


class InsuranceRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    # -----------------------------------------------------------------------
    # Feature 23: Policies & Admission Link
    # -----------------------------------------------------------------------

    def get_patient_policies(self, patient_id: UUID) -> list[InsurancePatientPolicy]:
        stmt = (
            select(InsurancePatientPolicy)
            .where(
                InsurancePatientPolicy.hospital_id == self.hospital_id,
                InsurancePatientPolicy.patient_id == patient_id,
            )
            .order_by(desc(InsurancePatientPolicy.created_at))
        )
        return list(self.db.scalars(stmt).all())

    def get_patient_policy_by_id(self, policy_id: UUID) -> InsurancePatientPolicy | None:
        stmt = select(InsurancePatientPolicy).where(
            InsurancePatientPolicy.id == policy_id,
            InsurancePatientPolicy.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_admission_policies(self, admission_id: UUID) -> list[InsuranceAdmissionPolicy]:
        stmt = (
            select(InsuranceAdmissionPolicy)
            .where(
                InsuranceAdmissionPolicy.hospital_id == self.hospital_id,
                InsuranceAdmissionPolicy.admission_id == admission_id,
            )
            .order_by(desc(InsuranceAdmissionPolicy.is_primary))
        )
        return list(self.db.scalars(stmt).all())

    def get_admission_policy_by_id(self, link_id: UUID) -> InsuranceAdmissionPolicy | None:
        stmt = select(InsuranceAdmissionPolicy).where(
            InsuranceAdmissionPolicy.id == link_id,
            InsuranceAdmissionPolicy.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    # -----------------------------------------------------------------------
    # Feature 24: Eligibility Checks
    # -----------------------------------------------------------------------

    def get_eligibility_checks_for_policy(self, policy_id: UUID) -> list[InsuranceEligibilityCheck]:
        stmt = (
            select(InsuranceEligibilityCheck)
            .where(
                InsuranceEligibilityCheck.hospital_id == self.hospital_id,
                InsuranceEligibilityCheck.patient_policy_id == policy_id,
            )
            .order_by(desc(InsuranceEligibilityCheck.check_date))
        )
        return list(self.db.scalars(stmt).all())

    # -----------------------------------------------------------------------
    # Features 25 & 26: Pre-Auth Requests & Queries
    # -----------------------------------------------------------------------

    def get_pre_auth_by_id(self, pre_auth_id: UUID) -> InsurancePreAuthRequest | None:
        stmt = (
            select(InsurancePreAuthRequest)
            .options(selectinload(InsurancePreAuthRequest.queries))
            .where(
                InsurancePreAuthRequest.id == pre_auth_id,
                InsurancePreAuthRequest.hospital_id == self.hospital_id,
            )
        )
        return self.db.scalars(stmt).first()

    def list_pre_auths_for_hospital(self, limit: int = 100) -> list[InsurancePreAuthRequest]:
        stmt = (
            select(InsurancePreAuthRequest)
            .options(selectinload(InsurancePreAuthRequest.queries))
            .where(InsurancePreAuthRequest.hospital_id == self.hospital_id)
            .order_by(desc(InsurancePreAuthRequest.created_at))
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    # -----------------------------------------------------------------------
    # Feature 27 & 28: Claim Dossier & Submissions
    # -----------------------------------------------------------------------

    def get_claim_by_id(self, claim_id: UUID) -> InsuranceClaimDossier | None:
        stmt = (
            select(InsuranceClaimDossier)
            .options(
                selectinload(InsuranceClaimDossier.submissions),
                selectinload(InsuranceClaimDossier.disputes),
                selectinload(InsuranceClaimDossier.settlement),
            )
            .where(
                InsuranceClaimDossier.id == claim_id,
                InsuranceClaimDossier.hospital_id == self.hospital_id,
            )
        )
        return self.db.scalars(stmt).first()

    def get_claims_for_hospital(self, limit: int = 100) -> list[InsuranceClaimDossier]:
        stmt = (
            select(InsuranceClaimDossier)
            .options(
                selectinload(InsuranceClaimDossier.submissions),
                selectinload(InsuranceClaimDossier.disputes),
                selectinload(InsuranceClaimDossier.settlement),
            )
            .where(InsuranceClaimDossier.hospital_id == self.hospital_id)
            .order_by(desc(InsuranceClaimDossier.created_at))
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    def get_submission_by_id(self, submission_id: UUID) -> InsuranceClaimSubmission | None:
        stmt = select(InsuranceClaimSubmission).where(
            InsuranceClaimSubmission.id == submission_id,
            InsuranceClaimSubmission.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    # -----------------------------------------------------------------------
    # Feature 29: Disputes
    # -----------------------------------------------------------------------

    def get_dispute_by_id(self, dispute_id: UUID) -> InsuranceClaimDispute | None:
        stmt = select(InsuranceClaimDispute).where(
            InsuranceClaimDispute.id == dispute_id,
            InsuranceClaimDispute.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    # -----------------------------------------------------------------------
    # Feature 30: Cashless Settlements
    # -----------------------------------------------------------------------

    def get_settlement_by_claim_id(self, claim_id: UUID) -> InsuranceCashlessSettlement | None:
        stmt = select(InsuranceCashlessSettlement).where(
            InsuranceCashlessSettlement.claim_id == claim_id,
            InsuranceCashlessSettlement.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def list_settlements_for_hospital(self, limit: int = 100) -> list[InsuranceCashlessSettlement]:
        stmt = (
            select(InsuranceCashlessSettlement)
            .where(InsuranceCashlessSettlement.hospital_id == self.hospital_id)
            .order_by(desc(InsuranceCashlessSettlement.created_at))
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())
