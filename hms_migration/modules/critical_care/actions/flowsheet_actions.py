"""
Actions for Feature 7: ICU Clinical Chart & Flowsheet.

Handles hourly physiological parameters, ABG tracking, and precise I/O fluid balancing.
Conforms to UltrionTech-Backend-Template modules/critical_care/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.critical_care.contracts.critical_care_contracts import (
    IcuFlowsheetCreate,
    IcuFlowsheetResponse,
)
from hms_migration.modules.critical_care.db.critical_care_repository import CriticalCareRepository
from hms_migration.modules.critical_care.entities.critical_care_entities import IcuFlowsheetEntry
from hms_migration.modules.inpatient.entities.admission import Admission
from hms_migration.shared.audit.service import write_audit_log


class RecordIcuFlowsheetAction:
    """Record an hourly intensive care flowsheet observation entry."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(
        self,
        admission_id: UUID,
        payload: IcuFlowsheetCreate,
        actor: dict[str, Any],
    ) -> IcuFlowsheetResponse:
        adm = (
            self.db.query(Admission)
            .filter(Admission.id == admission_id, Admission.hospital_id == self.hospital_id)
            .first()
        )
        if not adm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        # Compute Mean Arterial Pressure (MAP) if SBP and DBP provided: MAP = (2*DBP + SBP) / 3
        map_value: float | None = None
        if payload.systolic_bp is not None and payload.diastolic_bp is not None:
            map_value = round((2.0 * payload.diastolic_bp + payload.systolic_bp) / 3.0, 1)

        # Compute hourly fluid balance: Total Intake - Total Output
        total_intake = (payload.hourly_iv_intake_ml or 0.0) + (payload.hourly_enteral_intake_ml or 0.0)
        total_output = (payload.hourly_urine_output_ml or 0.0) + (payload.hourly_drain_output_ml or 0.0)
        hourly_balance = round(total_intake - total_output, 1)

        recorder_name = str(actor.get("name") or "ICU Nurse")
        recorder_id_raw = actor.get("user_id")
        recorder_id = UUID(str(recorder_id_raw)) if recorder_id_raw else None

        entry = IcuFlowsheetEntry(
            hospital_id=self.hospital_id,
            admission_id=admission_id,
            recorded_at=payload.recorded_at,
            recorded_by_id=recorder_id,
            recorded_by_name=recorder_name,
            heart_rate=payload.heart_rate,
            systolic_bp=payload.systolic_bp,
            diastolic_bp=payload.diastolic_bp,
            mean_arterial_pressure=map_value,
            spo2=payload.spo2,
            respiratory_rate=payload.respiratory_rate,
            temperature=payload.temperature,
            abg_ph=payload.abg_ph,
            abg_pco2=payload.abg_pco2,
            abg_po2=payload.abg_po2,
            abg_hco3=payload.abg_hco3,
            abg_lactate=payload.abg_lactate,
            hourly_iv_intake_ml=payload.hourly_iv_intake_ml,
            hourly_enteral_intake_ml=payload.hourly_enteral_intake_ml,
            hourly_urine_output_ml=payload.hourly_urine_output_ml,
            hourly_drain_output_ml=payload.hourly_drain_output_ml,
            hourly_balance_ml=hourly_balance,
            remarks=payload.remarks.strip() if payload.remarks else None,
        )
        self.repo.add(entry)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="record_flowsheet",
            entity_type="icu_flowsheet",
            entity_id=entry.id,
            summary=f"Recorded ICU flowsheet entry for admission {admission_id}",
            details={"balance_ml": hourly_balance, "map": map_value},
        )
        self.repo.commit()
        self.repo.refresh(entry)
        return IcuFlowsheetResponse.model_validate(entry)


class ListIcuFlowsheetAction:
    """Retrieve chronologically sorted hourly flowsheet entries for an admission."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, admission_id: UUID) -> list[IcuFlowsheetResponse]:
        entries = self.repo.list_flowsheet_entries(admission_id)
        return [IcuFlowsheetResponse.model_validate(e) for e in entries]
