"""
Actions for Feature 14: Nursing Shift Handover.

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
    NursingShiftHandoverAcknowledge,
    NursingShiftHandoverCreate,
    NursingShiftHandoverResponse,
)
from hms_migration.modules.inpatient.db.admissions_repository import AdmissionsRepository
from hms_migration.modules.inpatient.db.nursing_repository import NursingRepository
from hms_migration.modules.inpatient.entities.nursing_entities import NursingShiftHandover
from hms_migration.shared.audit.service import write_audit_log


class CreateNursingShiftHandoverAction:
    """Draft and record a structured SBAR shift handover."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.adm_repo = AdmissionsRepository(db)
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(
        self,
        admission_id: UUID,
        payload: NursingShiftHandoverCreate,
        actor: dict[str, Any],
    ) -> NursingShiftHandoverResponse:
        adm = self.adm_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not adm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        nurse_name = str(actor.get("name") or "Outgoing Nurse")
        nurse_id_raw = actor.get("user_id")
        nurse_id = UUID(str(nurse_id_raw)) if nurse_id_raw else None

        handover = NursingShiftHandover(
            hospital_id=self.hospital_id,
            admission_id=admission_id,
            patient_id=adm.patient_id,
            shift_date=payload.shift_date,
            shift_type=payload.shift_type,
            outgoing_nurse_id=nurse_id,
            outgoing_nurse_name=nurse_name,
            situation=payload.situation.strip(),
            background=payload.background.strip(),
            assessment_acuity=payload.assessment_acuity.strip(),
            pending_stat_orders=payload.pending_stat_orders or {},
            critical_lab_alerts=payload.critical_lab_alerts or {},
            high_risk_precautions=payload.high_risk_precautions or {},
            pending_tasks=payload.pending_tasks or {},
            is_acknowledged=False,
        )
        self.nursing_repo.add(handover)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="create_shift_handover",
            entity_type="nursing_shift_handover",
            entity_id=handover.id,
            summary=f"Created {payload.shift_type.value} shift handover for admission {admission_id}",
        )
        self.nursing_repo.commit()
        self.nursing_repo.refresh(handover)
        return NursingShiftHandoverResponse.model_validate(handover)


class AcknowledgeNursingShiftHandoverAction:
    """Incoming nurse electronic sign-off and acknowledgment of shift handover."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(
        self,
        handover_id: UUID,
        payload: NursingShiftHandoverAcknowledge,
        actor: dict[str, Any],
    ) -> NursingShiftHandoverResponse:
        handover = self.nursing_repo.get_handover_by_id(handover_id)
        if not handover:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Handover record not found")

        nurse_name = str(actor.get("name") or "Incoming Nurse")
        nurse_id_raw = actor.get("user_id")
        nurse_id = UUID(str(nurse_id_raw)) if nurse_id_raw else None

        handover.is_acknowledged = True
        handover.incoming_nurse_id = nurse_id
        handover.incoming_nurse_name = nurse_name
        handover.acknowledged_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="acknowledge_shift_handover",
            entity_type="nursing_shift_handover",
            entity_id=handover.id,
            summary=f"Acknowledged shift handover {handover_id} by incoming nurse {nurse_name}",
        )
        self.nursing_repo.commit()
        self.nursing_repo.refresh(handover)
        return NursingShiftHandoverResponse.model_validate(handover)


class GetNursingShiftHandoverAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(self, handover_id: UUID) -> NursingShiftHandoverResponse:
        handover = self.nursing_repo.get_handover_by_id(handover_id)
        if not handover:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Handover record not found")
        return NursingShiftHandoverResponse.model_validate(handover)


class ListNursingShiftHandoversAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.nursing_repo = NursingRepository(db, hospital_id)

    def execute(self, admission_id: UUID) -> list[NursingShiftHandoverResponse]:
        handovers = self.nursing_repo.list_handovers(admission_id)
        return [NursingShiftHandoverResponse.model_validate(h) for h in handovers]


CreateShiftHandoverAction = CreateNursingShiftHandoverAction
AcknowledgeShiftHandoverAction = AcknowledgeNursingShiftHandoverAction
GetShiftHandoverAction = GetNursingShiftHandoverAction
ListShiftHandoversAction = ListNursingShiftHandoversAction
