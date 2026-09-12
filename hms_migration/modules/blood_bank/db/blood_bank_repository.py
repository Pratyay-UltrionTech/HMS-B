"""
Database repository for Blood Bank Foundation.
All operations are hospital-tenant isolated.
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from hms_migration.modules.blood_bank.entities.blood_bank_entities import (
    BloodComponentSeparation,
    BloodCrossMatch,
    BloodDonation,
    BloodDonor,
    BloodGroup,
    BloodIssue,
    BloodReturn,
    BloodTransfusion,
    BloodUnit,
    BloodUnitStatus,
)


class BloodBankRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    # -----------------------------------------------------------------------
    # Feature 32: Donors & Donations
    # -----------------------------------------------------------------------

    def get_donor_by_id(self, donor_id: UUID) -> BloodDonor | None:
        stmt = select(BloodDonor).where(
            BloodDonor.id == donor_id,
            BloodDonor.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_donors(self, limit: int = 100) -> list[BloodDonor]:
        stmt = (
            select(BloodDonor)
            .where(BloodDonor.hospital_id == self.hospital_id)
            .order_by(desc(BloodDonor.created_at))
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    def get_donation_by_id(self, donation_id: UUID) -> BloodDonation | None:
        stmt = select(BloodDonation).where(
            BloodDonation.id == donation_id,
            BloodDonation.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_donations_for_donor(self, donor_id: UUID) -> list[BloodDonation]:
        stmt = (
            select(BloodDonation)
            .where(
                BloodDonation.hospital_id == self.hospital_id,
                BloodDonation.donor_id == donor_id,
            )
            .order_by(desc(BloodDonation.donation_date))
        )
        return list(self.db.scalars(stmt).all())

    # -----------------------------------------------------------------------
    # Feature 33 & 34: Units & Components
    # -----------------------------------------------------------------------

    def get_unit_by_id(self, unit_id: UUID) -> BloodUnit | None:
        stmt = select(BloodUnit).where(
            BloodUnit.id == unit_id,
            BloodUnit.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_units(
        self,
        blood_group: BloodGroup | None = None,
        status: BloodUnitStatus | None = None,
        limit: int = 100,
    ) -> list[BloodUnit]:
        stmt = select(BloodUnit).where(BloodUnit.hospital_id == self.hospital_id)
        if blood_group:
            stmt = stmt.where(BloodUnit.blood_group == blood_group)
        if status:
            stmt = stmt.where(BloodUnit.status == status)
        stmt = stmt.order_by(BloodUnit.expiry_date.asc()).limit(limit)
        return list(self.db.scalars(stmt).all())

    def get_separation_by_id(self, sep_id: UUID) -> BloodComponentSeparation | None:
        stmt = select(BloodComponentSeparation).where(
            BloodComponentSeparation.id == sep_id,
            BloodComponentSeparation.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    # -----------------------------------------------------------------------
    # Feature 35: Cross-Matches
    # -----------------------------------------------------------------------

    def get_cross_match_by_id(self, xm_id: UUID) -> BloodCrossMatch | None:
        stmt = select(BloodCrossMatch).where(
            BloodCrossMatch.id == xm_id,
            BloodCrossMatch.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_cross_matches_for_patient(self, patient_id: UUID) -> list[BloodCrossMatch]:
        stmt = (
            select(BloodCrossMatch)
            .where(
                BloodCrossMatch.hospital_id == self.hospital_id,
                BloodCrossMatch.patient_id == patient_id,
            )
            .order_by(desc(BloodCrossMatch.tested_at))
        )
        return list(self.db.scalars(stmt).all())

    def list_cross_matches(self, limit: int = 100) -> list[BloodCrossMatch]:
        stmt = select(BloodCrossMatch).where(
            BloodCrossMatch.hospital_id == self.hospital_id
        ).order_by(desc(BloodCrossMatch.tested_at)).limit(limit)
        return list(self.db.scalars(stmt).all())

    # -----------------------------------------------------------------------
    # Feature 36: Issues & Returns
    # -----------------------------------------------------------------------

    def list_issues(self, limit: int = 100) -> list[BloodIssue]:
        stmt = select(BloodIssue).where(
            BloodIssue.hospital_id == self.hospital_id
        ).order_by(desc(BloodIssue.issued_at)).limit(limit)
        return list(self.db.scalars(stmt).all())

    def get_issue_by_id(self, issue_id: UUID) -> BloodIssue | None:
        stmt = select(BloodIssue).where(
            BloodIssue.id == issue_id,
            BloodIssue.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_return_by_id(self, return_id: UUID) -> BloodReturn | None:
        stmt = select(BloodReturn).where(
            BloodReturn.id == return_id,
            BloodReturn.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_return_by_issue_id(self, issue_id: UUID) -> BloodReturn | None:
        stmt = select(BloodReturn).where(
            BloodReturn.issue_id == issue_id,
            BloodReturn.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_separations_for_parent_unit(self, parent_unit_id: UUID) -> list[BloodComponentSeparation]:
        stmt = select(BloodComponentSeparation).where(
            BloodComponentSeparation.hospital_id == self.hospital_id,
            BloodComponentSeparation.parent_unit_id == parent_unit_id,
        )
        return list(self.db.scalars(stmt).all())

    def get_cross_matches_for_unit(self, unit_id: UUID) -> list[BloodCrossMatch]:
        stmt = (
            select(BloodCrossMatch)
            .where(
                BloodCrossMatch.hospital_id == self.hospital_id,
                BloodCrossMatch.blood_unit_id == unit_id,
            )
            .order_by(desc(BloodCrossMatch.tested_at))
        )
        return list(self.db.scalars(stmt).all())

    def get_issue_for_unit(self, unit_id: UUID) -> BloodIssue | None:
        stmt = (
            select(BloodIssue)
            .where(
                BloodIssue.hospital_id == self.hospital_id,
                BloodIssue.blood_unit_id == unit_id,
            )
            .order_by(desc(BloodIssue.issued_at))
        )
        return self.db.scalars(stmt).first()

    def get_child_units(self, parent_unit_id: UUID) -> list[BloodUnit]:
        stmt = select(BloodUnit).where(
            BloodUnit.hospital_id == self.hospital_id,
            BloodUnit.parent_unit_id == parent_unit_id,
        )
        return list(self.db.scalars(stmt).all())

    def get_units_for_donation(self, donation_id: UUID) -> list[BloodUnit]:
        stmt = select(BloodUnit).where(
            BloodUnit.hospital_id == self.hospital_id,
            BloodUnit.donation_id == donation_id,
        )
        return list(self.db.scalars(stmt).all())

    # -----------------------------------------------------------------------
    # Feature 37: Transfusions
    # -----------------------------------------------------------------------

    def get_transfusion_by_id(self, transfusion_id: UUID) -> BloodTransfusion | None:
        stmt = select(BloodTransfusion).where(
            BloodTransfusion.id == transfusion_id,
            BloodTransfusion.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_transfusion_by_issue_id(self, issue_id: UUID) -> BloodTransfusion | None:
        stmt = select(BloodTransfusion).where(
            BloodTransfusion.issue_id == issue_id,
            BloodTransfusion.hospital_id == self.hospital_id,
        )
        return self.db.scalars(stmt).first()

    def get_transfusion_for_unit(self, unit_id: UUID) -> BloodTransfusion | None:
        stmt = (
            select(BloodTransfusion)
            .where(
                BloodTransfusion.hospital_id == self.hospital_id,
                BloodTransfusion.blood_unit_id == unit_id,
            )
            .order_by(desc(BloodTransfusion.start_time))
        )
        return self.db.scalars(stmt).first()
