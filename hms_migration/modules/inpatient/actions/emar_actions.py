"""
Target action implementation for Feature 15 (eMAR) and Feature 18 (Dual Sign-Off).

Conforms to UltrionTech-Backend-Template modules/inpatient/actions/ specification.
Follows Vertical Slice: API -> Actions -> Repository -> Entity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.inpatient.contracts.nursing_contracts import (
    MedicationAdminRecordExecution,
    MedicationAdminResponse,
    MedicationAdminScheduleCreate,
)
from hms_migration.modules.inpatient.db.admissions_repository import AdmissionsRepository
from hms_migration.modules.inpatient.db.nursing_repository import NursingRepository
from hms_migration.modules.inpatient.entities.nursing_entities import (
    MedicationAdminStatus,
    MedicationAdministrationRecord,
)
from hms_migration.shared.audit.service import write_audit_log


class ScheduleMedicationAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = NursingRepository(db, hospital_id)
        self.admission_repo = AdmissionsRepository(db)

    def execute(
        self,
        admission_id: UUID,
        payload: MedicationAdminScheduleCreate,
        actor: dict[str, Any],
    ) -> MedicationAdminResponse:
        admission = self.admission_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not admission:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")

        record = MedicationAdministrationRecord(
            hospital_id=self.hospital_id,
            admission_id=admission.id,
            patient_id=admission.patient_id,
            medicine_id=payload.medicine_id,
            medicine_name=payload.medicine_name,
            dose=payload.dose,
            route=payload.route,
            scheduled_time=payload.scheduled_time,
            status=MedicationAdminStatus.scheduled,
            is_high_alert=payload.is_high_alert,
        )
        self.repo.add(record)
        self.repo.commit()
        self.repo.refresh(record)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="SCHEDULE_MEDICATION_EMAR",
            entity_type="medication_administration_records",
            summary=f"Scheduled medication {record.medicine_name} ({record.dose}) for patient {record.patient_id}",
            entity_id=record.id,
        )
        return MedicationAdminResponse.model_validate(record)


class RecordMedicationExecutionAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = NursingRepository(db, hospital_id)

    def execute(
        self,
        record_id: UUID,
        payload: MedicationAdminRecordExecution,
        actor: dict[str, Any],
    ) -> MedicationAdminResponse:
        record = self.repo.get_emar_record_by_id(record_id)
        if not record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Medication administration record not found",
            )

        # Feature 18: High-alert dual sign-off validation
        if record.is_high_alert and payload.status == MedicationAdminStatus.administered:
            if not payload.witness_nurse_id and not payload.witness_nurse_name:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Dual sign-off (witness nurse) is mandatory when administering high-alert medication",
                )

        record.status = payload.status
        if payload.status == MedicationAdminStatus.administered:
            record.administered_at = datetime.now(timezone.utc)

        nurse_id_raw = actor.get("id") or actor.get("user_id")
        record.administering_nurse_id = UUID(str(nurse_id_raw)) if nurse_id_raw else None
        record.administering_nurse_name = actor.get("name") or actor.get("email") or "Nurse"
        record.witness_nurse_id = UUID(str(payload.witness_nurse_id)) if payload.witness_nurse_id else None
        record.witness_nurse_name = payload.witness_nurse_name
        record.vitals_before_admin = payload.vitals_before_admin
        record.notes_or_reason = payload.notes_or_reason

        self.repo.commit()
        self.repo.refresh(record)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="EXECUTE_MEDICATION_EMAR",
            entity_type="medication_administration_records",
            summary=f"Recorded medication status {record.status.value} for {record.medicine_name}",
            entity_id=record.id,
            details={
                "status": record.status.value,
                "is_high_alert": record.is_high_alert,
                "witness": record.witness_nurse_name,
            },
        )
        return MedicationAdminResponse.model_validate(record)


class ListMedicationAdminRecordsAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = NursingRepository(db, hospital_id)

    def execute(
        self,
        admission_id: UUID,
        status: MedicationAdminStatus | None = None,
    ) -> list[MedicationAdminResponse]:
        records = self.repo.list_emar_records(admission_id, status=status)
        return [MedicationAdminResponse.model_validate(r) for r in records]
