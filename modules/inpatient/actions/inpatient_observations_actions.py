"""
Actions for Inpatient Structured Vitals and Ward Intake/Output Observations.

Conforms to UltrionTech-Backend-Template modules/inpatient/actions/ specification.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from shared.exceptions.base import NotFoundError
from sqlalchemy.orm import Session

from modules.inpatient.contracts.nursing_contracts import (
    IpdIntakeOutputCreate,
    IpdIntakeOutputResponse,
    IpdIntakeOutputSummary,
    IpdVitalSignCreate,
    IpdVitalSignResponse,
)
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.db.inpatient_vitals_repository import InpatientVitalsRepository
from modules.inpatient.entities.nursing_entities import (
    IpdIntakeOutput,
    IpdVitalSign,
)
from modules.inpatient.services.admission_lifecycle_policy import (
    assert_can_document_clinical_record,
)
from shared.audit.service import write_audit_log


class RecordIpdVitalsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.adm_repo = AdmissionsRepository(db)
        self.repo = InpatientVitalsRepository(db, hospital_id)

    def execute(
        self,
        admission_id: UUID,
        payload: IpdVitalSignCreate,
        actor: dict[str, Any],
    ) -> IpdVitalSignResponse:
        adm = self.adm_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not adm:
            raise NotFoundError("Admission not found")
        assert_can_document_clinical_record(adm, "record vitals")

        recorder_name = str(actor.get("name") or "Clinical Staff")
        recorder_id_raw = actor.get("user_id")
        recorder_id = UUID(str(recorder_id_raw)) if recorder_id_raw else None
        recorded_at = payload.recorded_at or datetime.now(timezone.utc)

        vital = IpdVitalSign(
            hospital_id=self.hospital_id,
            admission_id=adm.id,
            patient_id=adm.patient_id,
            temperature_c=payload.temperature_c,
            pulse_rate_bpm=payload.pulse_rate_bpm,
            respiratory_rate_bpm=payload.respiratory_rate_bpm,
            systolic_bp=payload.systolic_bp,
            diastolic_bp=payload.diastolic_bp,
            spo2_percent=payload.spo2_percent,
            pain_score=payload.pain_score,
            consciousness=payload.consciousness,
            weight_kg=payload.weight_kg,
            notes=payload.notes.strip() if payload.notes else None,
            recorded_by_id=recorder_id,
            recorded_by_name=recorder_name,
            recorded_at=recorded_at,
        )
        self.repo.add_vital(vital)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="RECORD_IPD_VITALS",
            entity_type="ipd_vitals",
            summary=f"Recorded vital signs for admission {admission_id}",
            entity_id=vital.id,
        )
        return IpdVitalSignResponse.model_validate(vital)


class ListIpdVitalsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = InpatientVitalsRepository(db, hospital_id)

    def execute(self, admission_id: UUID, limit: int = 100) -> list[IpdVitalSignResponse]:
        vitals = self.repo.list_vitals(admission_id, limit=limit)
        return [IpdVitalSignResponse.model_validate(v) for v in vitals]


class RecordIntakeOutputAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.adm_repo = AdmissionsRepository(db)
        self.repo = InpatientVitalsRepository(db, hospital_id)

    def execute(
        self,
        admission_id: UUID,
        payload: IpdIntakeOutputCreate,
        actor: dict[str, Any],
    ) -> IpdIntakeOutputResponse:
        adm = self.adm_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not adm:
            raise NotFoundError("Admission not found")
        assert_can_document_clinical_record(adm, "record intake and output")

        recorder_name = str(actor.get("name") or "Clinical Staff")
        recorder_id_raw = actor.get("user_id")
        recorder_id = UUID(str(recorder_id_raw)) if recorder_id_raw else None
        recorded_at = payload.recorded_at or datetime.now(timezone.utc)

        entry = IpdIntakeOutput(
            hospital_id=self.hospital_id,
            admission_id=adm.id,
            patient_id=adm.patient_id,
            entry_type=payload.entry_type,
            category=payload.category.strip(),
            volume_ml=payload.volume_ml,
            unit=payload.unit.strip() or "mL",
            route_or_site=payload.route_or_site.strip() if payload.route_or_site else None,
            notes=payload.notes.strip() if payload.notes else None,
            recorded_by_id=recorder_id,
            recorded_by_name=recorder_name,
            recorded_at=recorded_at,
        )
        self.repo.add_intake_output(entry)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="RECORD_IPD_INTAKE_OUTPUT",
            entity_type="ipd_intake_outputs",
            summary=f"Recorded {entry.entry_type.value} of {entry.volume_ml} {entry.unit} ({entry.category}) for admission {admission_id}",
            entity_id=entry.id,
        )
        return IpdIntakeOutputResponse.model_validate(entry)


class ListIntakeOutputAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = InpatientVitalsRepository(db, hospital_id)

    def execute(self, admission_id: UUID, limit: int = 200) -> list[IpdIntakeOutputResponse]:
        records = self.repo.list_intake_output(admission_id, limit=limit)
        return [IpdIntakeOutputResponse.model_validate(r) for r in records]


class GetIntakeOutputSummaryAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = InpatientVitalsRepository(db, hospital_id)

    def execute(self, admission_id: UUID, hours: int = 24) -> IpdIntakeOutputSummary:
        return self.repo.get_intake_output_summary(admission_id, hours=hours)
