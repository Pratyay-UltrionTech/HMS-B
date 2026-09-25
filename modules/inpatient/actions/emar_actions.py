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

from shared.exceptions.base import NotFoundError
from sqlalchemy.orm import Session

from modules.inpatient.contracts.nursing_contracts import (
    MedicationAdminRecordExecution,
    MedicationAdminResponse,
    MedicationAdminScheduleCreate,
)
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.db.nursing_repository import NursingRepository
from modules.inpatient.entities.nursing_entities import (
    MedicationAdminStatus,
    MedicationAdministrationRecord,
)
from modules.patients.actions.allergy_actions import CheckPatientAllergyAlertAction
from modules.patients.contracts.allergy_contracts import CheckAllergyAlertRequest
from modules.pharmacy.actions.drug_interaction_actions import CheckDrugInteractionsAction
from modules.pharmacy.contracts.drug_interaction_contracts import CheckDrugInteractionsRequest
from shared.audit.service import write_audit_log
from modules.inpatient.services.admission_lifecycle_policy import (
    assert_can_document_clinical_record,
)


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
            raise NotFoundError("Admission not found")
        assert_can_document_clinical_record(admission, "schedule medication")

        # Feature 16 & 17 Clinical Safety Validation: Patient Allergy & Drug Interaction Gate
        allergy_checker = CheckPatientAllergyAlertAction(self.db, self.hospital_id)
        warning = allergy_checker.execute(admission.patient_id, CheckAllergyAlertRequest(medicine_name=payload.medicine_name))
        if warning.has_conflict and warning.requires_clinical_override:
            if not payload.override_confirmed:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "type": "allergy_conflict",
                        "message": warning.message,
                        "matched_allergen": warning.matched_allergen,
                        "severity": warning.severity.value if warning.severity else "unknown",
                        "requires_clinical_override": True,
                    },
                )
            write_audit_log(
                self.db,
                hospital_id=self.hospital_id,
                actor=actor,
                action="CLINICAL_SAFETY_OVERRIDE_ALLERGY",
                entity_type="medication_administration_records",
                summary=f"eMAR allergy warning overridden for '{payload.medicine_name}' against allergen '{warning.matched_allergen}'. Reason: {payload.override_reason or 'Not specified'}",
            )

        # Evaluate against other actively scheduled medications for this admission
        active_scheduled = (
            self.db.query(MedicationAdministrationRecord)
            .filter(
                MedicationAdministrationRecord.hospital_id == self.hospital_id,
                MedicationAdministrationRecord.admission_id == admission.id,
                MedicationAdministrationRecord.status == MedicationAdminStatus.scheduled,
            )
            .all()
        )
        active_med_names = list({m.medicine_name.strip() for m in active_scheduled if m.medicine_name})
        if payload.medicine_name.strip() not in active_med_names:
            active_med_names.append(payload.medicine_name.strip())

        if len(active_med_names) >= 2:
            interaction_checker = CheckDrugInteractionsAction(self.db, self.hospital_id)
            report = interaction_checker.execute(CheckDrugInteractionsRequest(medicines=active_med_names))
            if report.requires_clinical_override:
                if not payload.override_confirmed:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "type": "drug_interaction_conflict",
                            "message": "Major drug-drug interaction detected between proposed medication and active inpatient schedule.",
                            "highest_severity": report.highest_severity.value if report.highest_severity else "major",
                            "alerts": [a.model_dump() for a in report.alerts],
                            "requires_clinical_override": True,
                        },
                    )
                write_audit_log(
                    self.db,
                    hospital_id=self.hospital_id,
                    actor=actor,
                    action="CLINICAL_SAFETY_OVERRIDE_DRUG_INTERACTION",
                    entity_type="medication_administration_records",
                    summary=f"eMAR drug interaction warning overridden for '{payload.medicine_name}'. Reason: {payload.override_reason or 'Not specified'}",
                )

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
        self.admission_repo = AdmissionsRepository(db)

    def execute(
        self,
        record_id: UUID,
        payload: MedicationAdminRecordExecution,
        actor: dict[str, Any],
    ) -> MedicationAdminResponse:
        record = self.repo.get_emar_record_by_id(record_id)
        if not record:
            raise NotFoundError("Medication administration record not found")

        admission = self.admission_repo.get_admission_by_id(self.hospital_id, record.admission_id)
        if admission:
            assert_can_document_clinical_record(admission, "record medication administration")

        # eMAR State Machine: Only scheduled records may be executed/transitioned
        if record.status != MedicationAdminStatus.scheduled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Cannot change medication record in '{record.status.value}' state: clinical record is already finalized",
            )

        if payload.status == MedicationAdminStatus.scheduled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Target status cannot be 'scheduled'",
            )

        # Requirement: Non-administration requires a clinical reason
        if payload.status in (MedicationAdminStatus.withheld, MedicationAdminStatus.refused, MedicationAdminStatus.missed):
            if not payload.notes_or_reason or not payload.notes_or_reason.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"A documented reason is mandatory when status is '{payload.status.value}'",
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
        else:
            record.administered_at = None

        nurse_id_raw = actor.get("id") or actor.get("user_id")
        record.administering_nurse_id = UUID(str(nurse_id_raw)) if nurse_id_raw else None
        record.administering_nurse_name = actor.get("name") or actor.get("email") or "Nurse"
        record.witness_nurse_id = UUID(str(payload.witness_nurse_id)) if payload.witness_nurse_id else None
        record.witness_nurse_name = payload.witness_nurse_name
        record.vitals_before_admin = payload.vitals_before_admin
        record.notes_or_reason = payload.notes_or_reason.strip() if payload.notes_or_reason else None

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
