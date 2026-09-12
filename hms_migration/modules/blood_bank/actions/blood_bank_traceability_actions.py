"""
Read-only traceability/biovigilance query actions for Blood Bank (Feature 38).

Does NOT introduce any new tables - joins the existing chain:
BloodDonor -> BloodDonation -> BloodUnit (parent/child lineage) ->
BloodComponentSeparation -> BloodCrossMatch -> BloodIssue -> BloodTransfusion -> BloodReturn
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.blood_bank.contracts.blood_bank_contracts import (
    BloodDonationResponse,
    BloodDonorResponse,
    BloodIssueResponse,
    BloodReturnResponse,
    BloodTransfusionResponse,
    BloodUnitResponse,
    ComponentSeparationResponse,
    CrossMatchResponse,
    DerivedUnitTrace,
    DonationTrace,
    DonorTraceabilityResponse,
    UnitTraceabilityResponse,
)
from hms_migration.modules.blood_bank.db.blood_bank_repository import BloodBankRepository


class BloodTraceabilityActions:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = BloodBankRepository(db, hospital_id)

    def get_unit_traceability(self, unit_id: UUID) -> UnitTraceabilityResponse:
        unit = self.repo.get_unit_by_id(unit_id)
        if not unit:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood unit not found")

        donation = None
        donor = None
        if unit.donation_id:
            donation = self.repo.get_donation_by_id(unit.donation_id)
            if donation:
                donor = self.repo.get_donor_by_id(donation.donor_id)

        parent_unit = None
        if unit.parent_unit_id:
            parent_unit = self.repo.get_unit_by_id(unit.parent_unit_id)

        child_units = self.repo.get_child_units(unit.id)

        separations = self.repo.get_separations_for_parent_unit(unit.id)
        separation_response = None
        if separations:
            sep = separations[0]
            separation_response = ComponentSeparationResponse(
                id=sep.id,
                hospital_id=sep.hospital_id,
                separation_number=sep.separation_number,
                parent_unit_id=sep.parent_unit_id,
                separated_at=sep.separated_at,
                separated_by=sep.separated_by,
                method=sep.method,
                notes=sep.notes,
                created_components=[BloodUnitResponse.model_validate(cu) for cu in child_units],
            )

        cross_matches = self.repo.get_cross_matches_for_unit(unit.id)
        issue = self.repo.get_issue_for_unit(unit.id)
        transfusion = self.repo.get_transfusion_for_unit(unit.id)
        return_record = None
        if issue:
            return_record = self.repo.get_return_by_issue_id(issue.id)

        return UnitTraceabilityResponse(
            unit=BloodUnitResponse.model_validate(unit),
            donor=BloodDonorResponse.model_validate(donor) if donor else None,
            donation=BloodDonationResponse.model_validate(donation) if donation else None,
            parent_unit=BloodUnitResponse.model_validate(parent_unit) if parent_unit else None,
            child_units=[BloodUnitResponse.model_validate(cu) for cu in child_units],
            separation=separation_response,
            cross_matches=[CrossMatchResponse.model_validate(xm) for xm in cross_matches],
            issue=BloodIssueResponse.model_validate(issue) if issue else None,
            transfusion=BloodTransfusionResponse.model_validate(transfusion) if transfusion else None,
            return_record=BloodReturnResponse.model_validate(return_record) if return_record else None,
        )

    def get_donor_traceability(self, donor_id: UUID) -> DonorTraceabilityResponse:
        donor = self.repo.get_donor_by_id(donor_id)
        if not donor:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood donor not found")

        donations = self.repo.get_donations_for_donor(donor.id)
        donation_traces: list[DonationTrace] = []

        for donation in donations:
            units = self.repo.get_units_for_donation(donation.id)
            # Include separated child components derived from any whole-blood unit of this donation
            all_units = list(units)
            seen_ids = {u.id for u in all_units}
            frontier = list(units)
            while frontier:
                next_frontier = []
                for u in frontier:
                    children = self.repo.get_child_units(u.id)
                    for c in children:
                        if c.id not in seen_ids:
                            all_units.append(c)
                            seen_ids.add(c.id)
                            next_frontier.append(c)
                frontier = next_frontier

            derived_traces: list[DerivedUnitTrace] = []
            for u in all_units:
                issue = self.repo.get_issue_for_unit(u.id)
                transfusion = self.repo.get_transfusion_for_unit(u.id)
                return_record = self.repo.get_return_by_issue_id(issue.id) if issue else None
                derived_traces.append(
                    DerivedUnitTrace(
                        unit=BloodUnitResponse.model_validate(u),
                        issue=BloodIssueResponse.model_validate(issue) if issue else None,
                        transfusion=BloodTransfusionResponse.model_validate(transfusion) if transfusion else None,
                        return_record=BloodReturnResponse.model_validate(return_record) if return_record else None,
                    )
                )

            donation_traces.append(
                DonationTrace(
                    donation=BloodDonationResponse.model_validate(donation),
                    units=derived_traces,
                )
            )

        return DonorTraceabilityResponse(
            donor=BloodDonorResponse.model_validate(donor),
            donations=donation_traces,
        )
