"""
Actions for Feature 12: Nursing Care Planning.

Conforms to UltrionTech-Backend-Template modules/inpatient/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.inpatient.contracts.nursing_contracts import (
    NursingCarePlanCreate,
    NursingCarePlanReassess,
    NursingCarePlanResponse,
)
from hms_migration.modules.inpatient.db.admissions_repository import AdmissionsRepository
from hms_migration.modules.inpatient.db.nursing_repository import NursingRepository
from hms_migration.modules.inpatient.entities.nursing_entities import NursingCarePlan
from hms_migration.shared.audit.service import write_audit_log


class CreateNursingCarePlanAction:
    """Action to create a structured nursing care plan for an admitted patient."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.adm_repo = AdmissionsRepository(db)
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(
        self,
        admission_id: UUID,
        payload: NursingCarePlanCreate,
        actor: dict[str, Any],
    ) -> NursingCarePlanResponse:
        adm = self.adm_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not adm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        nurse_name = str(actor.get("name") or "Primary Nurse")

        plan = NursingCarePlan(
            hospital_id=self.hospital_id,
            admission_id=admission_id,
            patient_id=adm.patient_id,
            diagnosis_code=payload.diagnosis_code.strip() if payload.diagnosis_code else None,
            nursing_diagnosis=payload.nursing_diagnosis.strip(),
            goals=payload.goals.strip(),
            interventions=payload.interventions or {},
            evaluation_frequency=payload.evaluation_frequency.strip(),
            created_by_nurse_name=nurse_name,
        )
        self.nursing_repo.add(plan)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="create_care_plan",
            entity_type="nursing_care_plan",
            entity_id=plan.id,
            summary=f"Created nursing care plan for admission {admission_id}: {plan.nursing_diagnosis}",
        )
        self.nursing_repo.commit()
        self.nursing_repo.refresh(plan)
        return NursingCarePlanResponse.model_validate(plan)


class ReassessNursingCarePlanAction:
    """Action to reassess goals and update status of a nursing care plan."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(
        self,
        plan_id: UUID,
        payload: NursingCarePlanReassess,
        actor: dict[str, Any],
    ) -> NursingCarePlanResponse:
        plan = self.nursing_repo.get_care_plan_by_id(plan_id)
        if not plan:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nursing care plan not found")

        plan.status = payload.status
        plan.reassessment_notes = payload.reassessment_notes.strip()
        plan.reassessed_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="reassess_care_plan",
            entity_type="nursing_care_plan",
            entity_id=plan.id,
            summary=f"Reassessed care plan {plan.id} to status {payload.status.value}",
        )
        self.nursing_repo.commit()
        self.nursing_repo.refresh(plan)
        return NursingCarePlanResponse.model_validate(plan)


class GetNursingCarePlanAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(self, plan_id: UUID) -> NursingCarePlanResponse:
        plan = self.nursing_repo.get_care_plan_by_id(plan_id)
        if not plan:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nursing care plan not found")
        return NursingCarePlanResponse.model_validate(plan)


class ListNursingCarePlansAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(self, admission_id: UUID) -> list[NursingCarePlanResponse]:
        plans = self.nursing_repo.list_care_plans(admission_id)
        return [NursingCarePlanResponse.model_validate(p) for p in plans]
