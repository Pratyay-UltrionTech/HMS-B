"""
Target action implementation for Patient Allergies & Medication Safety Interception (Feature 16).

Conforms to UltrionTech-Backend-Template modules/patients/actions/ specification.
Follows Vertical Slice: API -> Actions -> Repository -> Entity.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.patients.contracts.allergy_contracts import (
    AllergyAlertWarning,
    CheckAllergyAlertRequest,
    PatientAllergyCreate,
    PatientAllergyResponse,
)
from hms_migration.modules.patients.db.allergy_repository import AllergyRepository
from hms_migration.modules.patients.db.patients_repository import PatientRepository
from hms_migration.modules.patients.entities.allergy import PatientAllergy
from hms_migration.shared.audit.service import write_audit_log


class AddPatientAllergyAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = AllergyRepository(db, hospital_id)
        self.patient_repo = PatientRepository(db, hospital_id)

    def execute(
        self,
        patient_id: UUID,
        payload: PatientAllergyCreate,
        actor: dict[str, Any],
    ) -> PatientAllergyResponse:
        patient = self.patient_repo.get_by_id(patient_id)
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        recorder_name = str(actor.get("name") or actor.get("email") or "Clinical Staff")

        allergy = PatientAllergy(
            hospital_id=self.hospital_id,
            patient_id=patient.id,
            allergen=payload.allergen.strip(),
            allergen_type=payload.allergen_type,
            severity=payload.severity,
            reaction=payload.reaction.strip() if payload.reaction else None,
            diagnosed_date=payload.diagnosed_date,
            is_active=True,
            recorded_by_name=recorder_name,
            notes=payload.notes.strip() if payload.notes else None,
        )
        self.repo.add(allergy)
        self.repo.commit()
        self.repo.refresh(allergy)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="ADD_PATIENT_ALLERGY",
            entity_type="patient_allergies",
            summary=f"Added allergy {allergy.allergen} ({allergy.severity.value}) for patient {patient.name}",
            entity_id=allergy.id,
        )
        return PatientAllergyResponse.model_validate(allergy)


class ListPatientAllergiesAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = AllergyRepository(db, hospital_id)

    def execute(self, patient_id: UUID, active_only: bool = True) -> list[PatientAllergyResponse]:
        if active_only:
            allergies = self.repo.list_active_allergies(patient_id)
        else:
            allergies = self.repo.list_all_allergies(patient_id)
        return [PatientAllergyResponse.model_validate(a) for a in allergies]


class DeactivatePatientAllergyAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = AllergyRepository(db, hospital_id)

    def execute(self, allergy_id: UUID, actor: dict[str, Any]) -> PatientAllergyResponse:
        allergy = self.repo.get_allergy_by_id(allergy_id)
        if not allergy:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Allergy record not found")

        allergy.is_active = False
        self.repo.commit()
        self.repo.refresh(allergy)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="DEACTIVATE_PATIENT_ALLERGY",
            entity_type="patient_allergies",
            summary=f"Deactivated allergy record for allergen {allergy.allergen}",
            entity_id=allergy.id,
        )
        return PatientAllergyResponse.model_validate(allergy)


class CheckPatientAllergyAlertAction:
    """
    Feature 16 Interception Engine:
    Evaluates proposed medication against the patient's active allergies.
    Detects cross-sensitivities and returns structured warning requiring clinical override.
    """

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = AllergyRepository(db, hospital_id)

    def execute(self, patient_id: UUID, payload: CheckAllergyAlertRequest) -> AllergyAlertWarning:
        active_allergies = self.repo.list_active_allergies(patient_id)
        if not active_allergies:
            return AllergyAlertWarning(has_conflict=False)

        med_name = payload.medicine_name.lower()
        gen_name = payload.generic_name.lower() if payload.generic_name else ""

        for a in active_allergies:
            allergen_clean = a.allergen.lower().strip()
            # Match allergen against medicine name or generic name
            if (
                allergen_clean in med_name
                or (gen_name and allergen_clean in gen_name)
                or med_name in allergen_clean
            ):
                msg = (
                    f"CRITICAL ALLERGY ALERT: Patient is allergic to '{a.allergen}' "
                    f"(Severity: {a.severity.value}). Reaction: {a.reaction or 'Unspecified'}."
                )
                return AllergyAlertWarning(
                    has_conflict=True,
                    severity=a.severity,
                    matched_allergen=a.allergen,
                    reaction=a.reaction,
                    message=msg,
                    requires_clinical_override=True,
                )

        return AllergyAlertWarning(has_conflict=False)
