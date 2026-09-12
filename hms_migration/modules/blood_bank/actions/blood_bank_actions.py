"""
Business logic actions for Blood Bank Foundation.

Features covered:
- Feature 32: Donor Registry & Donation Collection
- Feature 33: Blood Unit Inventory & Serology Clearance (Correction #7 & #8)
- Feature 34: Component Management & Lineage (Correction #9)
- Feature 35: Cross-Match Management (Correction #10)
- Feature 36: Blood Issue & Return Lifecycle (Correction #11)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
import uuid
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.blood_bank.contracts.blood_bank_contracts import (
    BloodDonationCreate,
    BloodDonationResponse,
    BloodDonorCreate,
    BloodDonorResponse,
    BloodIssueCreate,
    BloodIssueResponse,
    BloodReturnCreate,
    BloodReturnResponse,
    BloodTransfusionResponse,
    BloodUnitCreate,
    BloodUnitResponse,
    BloodUnitSerologyClear,
    ComponentSeparationRequest,
    ComponentSeparationResponse,
    CrossMatchCreate,
    CrossMatchResponse,
    TransfusionAbort,
    TransfusionComplete,
    TransfusionReactionReport,
    TransfusionStart,
)
from hms_migration.modules.blood_bank.db.blood_bank_repository import BloodBankRepository
from hms_migration.modules.blood_bank.entities.blood_bank_entities import (
    BloodComponentSeparation,
    BloodComponentType,
    BloodCrossMatch,
    BloodDonation,
    BloodDonor,
    BloodIssue,
    BloodIssueStatus,
    BloodReturn,
    BloodReturnDisposition,
    BloodTransfusion,
    BloodTransfusionStatus,
    BloodUnit,
    BloodUnitStatus,
)
from hms_migration.modules.inpatient.entities.admission import Admission
from hms_migration.modules.patients.entities.patient import Patient


class BloodBankActions:
    def __init__(self, db: Session, hospital_id: UUID, actor: dict[str, Any] | None = None) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.actor = actor or {}
        self.repo = BloodBankRepository(db, hospital_id)

    # -----------------------------------------------------------------------
    # Feature 32: Donor Registry & Donation Collection
    # -----------------------------------------------------------------------

    def register_donor(self, payload: BloodDonorCreate) -> BloodDonorResponse:
        donor_num = payload.donor_number or f"DNR-{uuid.uuid4().hex[:8].upper()}"

        donor = BloodDonor(
            hospital_id=self.hospital_id,
            donor_number=donor_num,
            name=payload.name,
            gender=payload.gender,
            date_of_birth=payload.date_of_birth,
            blood_group=payload.blood_group,
            phone=payload.phone,
            email=payload.email,
            address=payload.address,
            is_eligible=payload.is_eligible,
            deferral_reason=payload.deferral_reason,
            deferral_until=payload.deferral_until,
        )
        self.db.add(donor)
        self.db.commit()
        self.db.refresh(donor)
        return BloodDonorResponse.model_validate(donor)

    def record_donation(self, payload: BloodDonationCreate) -> BloodDonationResponse:
        donor = self.repo.get_donor_by_id(payload.donor_id)
        if not donor:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood donor not found")

        if not donor.is_eligible:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Donor {donor.donor_number} is currently deferred: {donor.deferral_reason or 'Not eligible'}",
            )

        now = datetime.now(timezone.utc)
        actor_name = str(self.actor.get("email") or self.actor.get("username") or "Phlebotomist")
        donation_num = f"DON-{uuid.uuid4().hex[:8].upper()}"

        donation = BloodDonation(
            hospital_id=self.hospital_id,
            donation_number=donation_num,
            donor_id=donor.id,
            donation_date=now,
            donation_type=payload.donation_type,
            bag_type=payload.bag_type,
            volume_ml=payload.volume_ml,
            hemoglobin_g_dl=payload.hemoglobin_g_dl,
            blood_pressure=payload.blood_pressure,
            pulse=payload.pulse,
            collected_by=actor_name,
            notes=payload.notes,
        )
        self.db.add(donation)
        self.db.flush()

        # Update donor's last donation date
        donor.last_donation_date = now.date()

        # Automatically spawn initial Whole Blood Unit in quarantine (Correction #7)
        unit_num = f"BU-{uuid.uuid4().hex[:8].upper()}"
        expiry = now + timedelta(days=35)  # Standard whole blood collection shelf-life

        initial_unit = BloodUnit(
            hospital_id=self.hospital_id,
            unit_number=unit_num,
            donation_id=donation.id,
            parent_unit_id=None,
            component_type=BloodComponentType.whole_blood,
            blood_group=donor.blood_group,
            volume_ml=payload.volume_ml,
            collection_date=now,
            expiry_date=expiry,
            storage_location="Blood Bank Refrigerator (Quarantine)",
            serology_tested=False,
            serology_cleared=False,
            status=BloodUnitStatus.quarantine,
        )
        self.db.add(initial_unit)
        self.db.commit()
        self.db.refresh(donation)
        return BloodDonationResponse.model_validate(donation)

    # -----------------------------------------------------------------------
    # Feature 33: Blood Unit Inventory & Serology Clearance
    # -----------------------------------------------------------------------

    def create_unit_manual(self, payload: BloodUnitCreate) -> BloodUnitResponse:
        """Manual intake of blood unit (e.g. from external blood bank transfer)."""
        unit_num = f"BU-{uuid.uuid4().hex[:8].upper()}"
        unit = BloodUnit(
            hospital_id=self.hospital_id,
            unit_number=unit_num,
            donation_id=payload.donation_id,
            parent_unit_id=None,
            component_type=payload.component_type,
            blood_group=payload.blood_group,
            volume_ml=payload.volume_ml,
            collection_date=payload.collection_date,
            expiry_date=payload.expiry_date,
            storage_location=payload.storage_location,
            serology_tested=False,
            serology_cleared=False,
            status=BloodUnitStatus.quarantine,
        )
        self.db.add(unit)
        self.db.commit()
        self.db.refresh(unit)
        return BloodUnitResponse.model_validate(unit)

    def clear_serology(self, unit_id: UUID, payload: BloodUnitSerologyClear) -> BloodUnitResponse:
        unit = self.repo.get_unit_by_id(unit_id)
        if not unit:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood unit not found")

        unit.serology_tested = True
        unit.serology_cleared = payload.serology_cleared

        if payload.serology_cleared:
            unit.status = BloodUnitStatus.available
            if payload.storage_location:
                unit.storage_location = payload.storage_location
            else:
                unit.storage_location = "Blood Bank Main Refrigerator"
        else:
            unit.status = BloodUnitStatus.discarded
            unit.storage_location = "Biohazard Quarantine / Discard"

        self.db.commit()
        self.db.refresh(unit)
        return BloodUnitResponse.model_validate(unit)

    # -----------------------------------------------------------------------
    # Feature 34: Component Management & Separation Lineage (Correction #9)
    # -----------------------------------------------------------------------

    def separate_components(self, parent_unit_id: UUID, payload: ComponentSeparationRequest) -> ComponentSeparationResponse:
        parent_unit = self.repo.get_unit_by_id(parent_unit_id)
        if not parent_unit:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent blood unit not found")

        if parent_unit.component_type != BloodComponentType.whole_blood:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot separate unit of type '{parent_unit.component_type}'. Only whole_blood can be separated.",
            )

        if parent_unit.status not in (BloodUnitStatus.available, BloodUnitStatus.quarantine):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot separate unit in status '{parent_unit.status}'. Must be available or quarantine.",
            )

        now = datetime.now(timezone.utc)
        actor_name = str(self.actor.get("email") or self.actor.get("username") or "Lab Specialist")
        sep_num = f"SEP-{uuid.uuid4().hex[:8].upper()}"

        separation = BloodComponentSeparation(
            hospital_id=self.hospital_id,
            separation_number=sep_num,
            parent_unit_id=parent_unit.id,
            separated_at=now,
            separated_by=actor_name,
            method=payload.method,
            notes=payload.notes,
        )
        self.db.add(separation)

        # Transition parent unit status to separated
        parent_unit.status = BloodUnitStatus.separated

        child_units: list[BloodUnit] = []
        for comp in payload.components:
            child_unit_num = f"{parent_unit.unit_number}-{comp.component_type.value.upper()[:4]}"
            child_expiry = parent_unit.collection_date + timedelta(days=comp.shelf_life_days)

            c_unit = BloodUnit(
                hospital_id=self.hospital_id,
                unit_number=child_unit_num,
                donation_id=parent_unit.donation_id,
                parent_unit_id=parent_unit.id,
                component_type=comp.component_type,
                blood_group=parent_unit.blood_group,
                volume_ml=comp.volume_ml,
                collection_date=parent_unit.collection_date,
                expiry_date=child_expiry,
                storage_location=comp.storage_location,
                serology_tested=parent_unit.serology_tested,
                serology_cleared=parent_unit.serology_cleared,
                status=parent_unit.status if parent_unit.status != BloodUnitStatus.separated else BloodUnitStatus.available,
            )
            self.db.add(c_unit)
            child_units.append(c_unit)

        self.db.commit()
        self.db.refresh(separation)
        for u in child_units:
            self.db.refresh(u)

        return ComponentSeparationResponse(
            id=separation.id,
            hospital_id=separation.hospital_id,
            separation_number=separation.separation_number,
            parent_unit_id=separation.parent_unit_id,
            separated_at=separation.separated_at,
            separated_by=separation.separated_by,
            method=separation.method,
            notes=separation.notes,
            created_components=[BloodUnitResponse.model_validate(cu) for cu in child_units],
        )

    # -----------------------------------------------------------------------
    # Feature 35: Cross-Match Management (Correction #10)
    # -----------------------------------------------------------------------

    def record_cross_match(self, payload: CrossMatchCreate) -> CrossMatchResponse:
        unit = self.repo.get_unit_by_id(payload.blood_unit_id)
        if not unit:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood unit not found")

        # Must be available or reserved for this exact patient
        if unit.status == BloodUnitStatus.reserved and unit.reserved_for_patient_id != payload.patient_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Blood unit {unit.unit_number} is already reserved for another patient.",
            )
        elif unit.status not in (BloodUnitStatus.available, BloodUnitStatus.reserved):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Blood unit {unit.unit_number} cannot be cross-matched in status '{unit.status}'. Must be available.",
            )

        patient = self.db.query(Patient).filter(
            Patient.id == payload.patient_id, Patient.hospital_id == self.hospital_id
        ).first()
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        if payload.admission_id:
            adm = self.db.query(Admission).filter(
                Admission.id == payload.admission_id, Admission.hospital_id == self.hospital_id
            ).first()
            if not adm:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        now = datetime.now(timezone.utc)
        actor_name = str(self.actor.get("email") or self.actor.get("username") or "Lab Technician")
        xm_num = f"XM-{uuid.uuid4().hex[:8].upper()}"

        xm = BloodCrossMatch(
            hospital_id=self.hospital_id,
            cross_match_number=xm_num,
            blood_unit_id=unit.id,
            patient_id=payload.patient_id,
            admission_id=payload.admission_id,
            patient_blood_group=payload.patient_blood_group,
            donor_blood_group=unit.blood_group,
            major_crossmatch_result=payload.major_crossmatch_result,
            minor_crossmatch_result=payload.minor_crossmatch_result,
            coombs_test_result=payload.coombs_test_result,
            antibody_screen_result=payload.antibody_screen_result,
            is_compatible=payload.is_compatible,
            tested_by=actor_name,
            authorized_by=payload.authorized_by,
            tested_at=now,
            notes=payload.notes,
        )
        self.db.add(xm)

        # Enforce inventory reservation only when authorized test result is compatible (Correction #10)
        if payload.is_compatible:
            unit.status = BloodUnitStatus.reserved
            unit.reserved_for_patient_id = payload.patient_id
            unit.reserved_for_admission_id = payload.admission_id
            unit.reserved_at = now

        self.db.commit()
        self.db.refresh(xm)
        return CrossMatchResponse.model_validate(xm)

    # -----------------------------------------------------------------------
    # Feature 36: Blood Issue & Return (Correction #11)
    # -----------------------------------------------------------------------

    def issue_blood_unit(self, payload: BloodIssueCreate) -> BloodIssueResponse:
        unit = self.repo.get_unit_by_id(payload.blood_unit_id)
        if not unit:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood unit not found")

        # Validate eligible status
        if unit.status not in (BloodUnitStatus.reserved, BloodUnitStatus.available):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Blood unit {unit.unit_number} cannot be issued from status '{unit.status}'. Must be reserved or available.",
            )

        if unit.status == BloodUnitStatus.reserved and unit.reserved_for_patient_id != payload.patient_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Blood unit {unit.unit_number} is reserved for a different patient.",
            )

        now = datetime.now(timezone.utc)
        actor_name = str(self.actor.get("email") or self.actor.get("username") or "Blood Bank Officer")
        iss_num = f"ISS-{uuid.uuid4().hex[:8].upper()}"

        issue = BloodIssue(
            hospital_id=self.hospital_id,
            issue_number=iss_num,
            blood_unit_id=unit.id,
            patient_id=payload.patient_id,
            admission_id=payload.admission_id,
            requisition_ref=payload.requisition_ref,
            issued_to=payload.issued_to,
            issued_by=actor_name,
            issued_at=now,
            transport_box_temp_c=payload.transport_box_temp_c,
            status=BloodIssueStatus.issued,
            notes=payload.notes,
        )
        self.db.add(issue)

        # Enforce inventory transition
        unit.status = BloodUnitStatus.issued
        self.db.commit()
        self.db.refresh(issue)
        return BloodIssueResponse.model_validate(issue)

    def return_blood_unit(self, issue_id: UUID, payload: BloodReturnCreate) -> BloodReturnResponse:
        issue = self.repo.get_issue_by_id(issue_id)
        if not issue:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood issue record not found")

        if issue.status != BloodIssueStatus.issued:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot return blood issue in status '{issue.status}'.",
            )

        unit = self.repo.get_unit_by_id(issue.blood_unit_id)
        if not unit:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked blood unit not found")

        now = datetime.now(timezone.utc)
        actor_name = str(self.actor.get("email") or self.actor.get("username") or "Blood Bank Officer")
        ret_num = f"RET-{uuid.uuid4().hex[:8].upper()}"

        ret = BloodReturn(
            hospital_id=self.hospital_id,
            return_number=ret_num,
            issue_id=issue.id,
            blood_unit_id=unit.id,
            returned_by=actor_name,
            received_by=actor_name,
            returned_at=now,
            return_reason=payload.return_reason,
            cold_chain_maintained=payload.cold_chain_maintained,
            bag_intact=payload.bag_intact,
            disposition=payload.disposition,
            disposition_notes=payload.disposition_notes,
        )
        self.db.add(ret)
        issue.status = BloodIssueStatus.returned

        # Enforce valid inventory transition according to disposition (Correction #11)
        if payload.disposition == BloodReturnDisposition.restocked:
            unit.status = BloodUnitStatus.available
            unit.reserved_for_patient_id = None
            unit.reserved_for_admission_id = None
            unit.reserved_at = None
        else:
            unit.status = BloodUnitStatus.discarded
            unit.storage_location = "Biohazard Discard (Returned compromised)"

        self.db.commit()
        self.db.refresh(ret)
        return BloodReturnResponse.model_validate(ret)

    # -----------------------------------------------------------------------
    # Feature 37: Transfusion Record
    # -----------------------------------------------------------------------

    def start_transfusion(self, payload: TransfusionStart) -> BloodTransfusionResponse:
        issue = self.repo.get_issue_by_id(payload.issue_id)
        if not issue:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood issue record not found")

        # Cannot create a transfusion for an issue/unit not in "issued" status
        if issue.status != BloodIssueStatus.issued:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot start transfusion for issue in status '{issue.status}'. Must be issued.",
            )

        unit = self.repo.get_unit_by_id(issue.blood_unit_id)
        if not unit:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked blood unit not found")

        if unit.status != BloodUnitStatus.issued:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot start transfusion; blood unit {unit.unit_number} is not in 'issued' status.",
            )

        existing = self.repo.get_transfusion_by_issue_id(issue.id)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A transfusion record already exists for this issue.",
            )

        now = datetime.now(timezone.utc)
        tx_num = f"TRX-{uuid.uuid4().hex[:8].upper()}"

        transfusion = BloodTransfusion(
            hospital_id=self.hospital_id,
            transfusion_number=tx_num,
            issue_id=issue.id,
            blood_unit_id=unit.id,
            patient_id=issue.patient_id,
            admission_id=issue.admission_id,
            start_time=now,
            verified_by_staff_id=payload.verified_by_staff_id,
            pre_transfusion_vitals=payload.pre_transfusion_vitals,
            observations=payload.observations,
            status=BloodTransfusionStatus.in_progress,
        )
        self.db.add(transfusion)
        self.db.commit()
        self.db.refresh(transfusion)
        return BloodTransfusionResponse.model_validate(transfusion)

    def _get_in_progress_transfusion(self, transfusion_id: UUID) -> BloodTransfusion:
        transfusion = self.repo.get_transfusion_by_id(transfusion_id)
        if not transfusion:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfusion record not found")
        if transfusion.status != BloodTransfusionStatus.in_progress:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot transition transfusion in status '{transfusion.status}'. Must be in_progress.",
            )
        return transfusion

    def complete_transfusion(self, transfusion_id: UUID, payload: TransfusionComplete) -> BloodTransfusionResponse:
        transfusion = self._get_in_progress_transfusion(transfusion_id)
        unit = self.repo.get_unit_by_id(transfusion.blood_unit_id)
        if not unit:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked blood unit not found")

        now = datetime.now(timezone.utc)
        transfusion.status = BloodTransfusionStatus.completed
        transfusion.end_time = now
        transfusion.completed_by_staff_id = payload.completed_by_staff_id
        if payload.observations:
            transfusion.observations = payload.observations

        # On completion, update the linked BloodUnit status to "transfused"
        unit.status = BloodUnitStatus.transfused

        # Mirror completion onto the issue record status
        issue = self.repo.get_issue_by_id(transfusion.issue_id)
        if issue:
            issue.status = BloodIssueStatus.transfused

        self.db.commit()
        self.db.refresh(transfusion)
        return BloodTransfusionResponse.model_validate(transfusion)

    def abort_transfusion(self, transfusion_id: UUID, payload: TransfusionAbort) -> BloodTransfusionResponse:
        transfusion = self._get_in_progress_transfusion(transfusion_id)

        now = datetime.now(timezone.utc)
        transfusion.status = BloodTransfusionStatus.aborted
        transfusion.end_time = now
        transfusion.completed_by_staff_id = payload.completed_by_staff_id
        if payload.observations:
            transfusion.observations = payload.observations

        self.db.commit()
        self.db.refresh(transfusion)
        return BloodTransfusionResponse.model_validate(transfusion)

    def report_transfusion_reaction(
        self, transfusion_id: UUID, payload: TransfusionReactionReport
    ) -> BloodTransfusionResponse:
        transfusion = self._get_in_progress_transfusion(transfusion_id)

        now = datetime.now(timezone.utc)
        transfusion.status = BloodTransfusionStatus.reaction
        transfusion.end_time = now
        transfusion.completed_by_staff_id = payload.completed_by_staff_id
        transfusion.adverse_reaction_occurred = True
        transfusion.adverse_reaction_type = payload.adverse_reaction_type
        transfusion.adverse_reaction_details = payload.adverse_reaction_details
        if payload.observations:
            transfusion.observations = payload.observations

        self.db.commit()
        self.db.refresh(transfusion)
        return BloodTransfusionResponse.model_validate(transfusion)

    # -----------------------------------------------------------------------
    # Query / Read Actions (Finding F-03: Architectural consistency API -> Action -> DB)
    # -----------------------------------------------------------------------

    def get_donors(self, limit: int = 100) -> list[BloodDonorResponse]:
        donors = self.repo.get_donors(limit=limit)
        return [BloodDonorResponse.model_validate(d) for d in donors]

    def get_donor(self, donor_id: UUID) -> BloodDonorResponse:
        donor = self.repo.get_donor_by_id(donor_id)
        if not donor:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood donor not found")
        return BloodDonorResponse.model_validate(donor)

    def get_units(
        self, blood_group: BloodGroup | None = None, status_filter: BloodUnitStatus | None = None
    ) -> list[BloodUnitResponse]:
        units = self.repo.get_units(blood_group=blood_group, status=status_filter)
        return [BloodUnitResponse.model_validate(u) for u in units]

    def get_unit(self, unit_id: UUID) -> BloodUnitResponse:
        unit = self.repo.get_unit_by_id(unit_id)
        if not unit:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blood unit not found")
        return BloodUnitResponse.model_validate(unit)

    def get_issues(self, limit: int = 100) -> list[BloodIssueResponse]:
        issues = self.repo.get_issues_for_hospital(limit=limit)
        return [BloodIssueResponse.model_validate(i) for i in issues]

