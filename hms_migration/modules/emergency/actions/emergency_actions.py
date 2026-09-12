"""
Domain actions for Emergency Case Registration, Triage Assessment, Queue, and Disposition.

Conforms to UltrionTech-Backend-Template modules/emergency/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.emergency.contracts.emergency_contracts import (
    EmergencyDispositionCreate,
    EmergencyDispositionResponse,
    EmergencyEncounterCreate,
    EmergencyEncounterResponse,
    EmergencyQueueItemResponse,
    EmergencyTriageCreate,
    EmergencyTriageResponse,
)
from hms_migration.modules.emergency.db.emergency_repository import EmergencyRepository
from hms_migration.modules.emergency.entities.emergency_entities import (
    EmergencyDisposition,
    EmergencyDispositionType,
    EmergencyEncounter,
    EmergencyStatus,
    EmergencyTriageAssessment,
)
from hms_migration.modules.emergency.validators.emergency_validators import (
    assert_can_set_disposition,
    assert_can_triage,
    validate_disposition_payload,
)
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.shared.audit.service import write_audit_log


class RegisterEmergencyEncounterAction:
    """Action for Feature 1: Emergency Case Registration."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = EmergencyRepository(db, hospital_id)

    def execute(self, payload: EmergencyEncounterCreate, actor: dict[str, Any]) -> EmergencyEncounterResponse:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == self.hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        # Check for duplicate active encounter to maintain patient clinical safety
        active = self.repo.get_active_patient_encounter(payload.patient_id)
        if active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Patient already has an active emergency encounter: {active.er_id}",
            )

        er_id = self.repo.generate_next_er_id()
        encounter = EmergencyEncounter(
            hospital_id=self.hospital_id,
            patient_id=payload.patient_id,
            er_id=er_id,
            arrival_time=datetime.now(timezone.utc),
            arrival_source=payload.arrival_source,
            presenting_complaints=payload.presenting_complaints.strip(),
            attending_doctor_id=payload.attending_doctor_id,
            status=EmergencyStatus.registered,
        )
        self.repo.add(encounter)
        self.repo.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="create",
            entity_type="emergency_encounter",
            entity_id=encounter.id,
            summary=f"Registered emergency encounter {encounter.er_id}",
            details={"er_id": encounter.er_id, "patient_id": str(payload.patient_id)},
        )
        self.repo.commit()
        self.repo.refresh(encounter)
        return EmergencyEncounterResponse.model_validate(encounter)


class PerformEmergencyTriageAction:
    """Action for Feature 2: Emergency Triage Assessment."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = EmergencyRepository(db, hospital_id)

    def execute(
        self,
        encounter_id: UUID,
        payload: EmergencyTriageCreate,
        actor: dict[str, Any],
    ) -> EmergencyTriageResponse:
        encounter = self.repo.get_encounter_by_id(encounter_id)
        if not encounter:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Emergency encounter not found")

        assert_can_triage(encounter)

        # Check if triage assessment already exists (idempotent / update or reject duplicate)
        existing = (
            self.db.query(EmergencyTriageAssessment)
            .filter(
                EmergencyTriageAssessment.encounter_id == encounter_id,
                EmergencyTriageAssessment.hospital_id == self.hospital_id,
            )
            .first()
        )
        nurse_name = str(actor.get("name") or "Emergency Nurse")
        nurse_id_raw = actor.get("user_id")
        nurse_id = UUID(str(nurse_id_raw)) if nurse_id_raw else None

        if existing:
            existing.acuity_level = payload.acuity_level
            existing.avpu = payload.avpu
            existing.pain_score = payload.pain_score
            existing.respiratory_distress = payload.respiratory_distress
            existing.systolic_bp = payload.systolic_bp
            existing.diastolic_bp = payload.diastolic_bp
            existing.heart_rate = payload.heart_rate
            existing.respiratory_rate = payload.respiratory_rate
            existing.temperature = payload.temperature
            existing.spo2 = payload.spo2
            existing.red_flags = payload.red_flags
            existing.triage_notes = payload.triage_notes
            existing.triaged_by_id = nurse_id
            existing.triaged_by_name = nurse_name
            triage = existing
        else:
            triage = EmergencyTriageAssessment(
                hospital_id=self.hospital_id,
                encounter_id=encounter_id,
                acuity_level=payload.acuity_level,
                avpu=payload.avpu,
                pain_score=payload.pain_score,
                respiratory_distress=payload.respiratory_distress,
                systolic_bp=payload.systolic_bp,
                diastolic_bp=payload.diastolic_bp,
                heart_rate=payload.heart_rate,
                respiratory_rate=payload.respiratory_rate,
                temperature=payload.temperature,
                spo2=payload.spo2,
                red_flags=payload.red_flags,
                triage_notes=payload.triage_notes,
                triaged_by_id=nurse_id,
                triaged_by_name=nurse_name,
            )
            self.repo.add(triage)

        # Update encounter acuity and status
        encounter.triage_level = payload.acuity_level
        # Acuity 1 (Resuscitation) or 2 (Emergent) moves directly to in_treatment
        if payload.acuity_level in (1, 2):
            encounter.status = EmergencyStatus.in_treatment
        elif encounter.status == EmergencyStatus.registered:
            encounter.status = EmergencyStatus.triaged

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="triage",
            entity_type="emergency_encounter",
            entity_id=encounter.id,
            summary=f"Triaged emergency encounter {encounter.er_id} at level {payload.acuity_level}",
            details={"acuity_level": payload.acuity_level, "er_id": encounter.er_id},
        )
        self.repo.commit()
        self.repo.refresh(triage)
        return EmergencyTriageResponse.model_validate(triage)


class GetEmergencyQueueAction:
    """Action for Feature 3: Live Priority-Based Emergency Triage Queue."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = EmergencyRepository(db, hospital_id)

    def execute(self) -> list[EmergencyQueueItemResponse]:
        encounters = self.repo.get_triage_queue()
        now = datetime.now(timezone.utc)
        items: list[EmergencyQueueItemResponse] = []
        for e in encounters:
            patient = e.patient
            # Calculate wait time in minutes
            wait_minutes = 0
            if e.arrival_time:
                wait_sec = (now - (e.arrival_time if e.arrival_time.tzinfo else e.arrival_time.replace(tzinfo=timezone.utc))).total_seconds()
                wait_minutes = max(0, int(wait_sec // 60))

            items.append(
                EmergencyQueueItemResponse(
                    encounter_id=e.id,
                    er_id=e.er_id,
                    patient_id=e.patient_id,
                    patient_name=patient.name if patient else "Unknown",
                    patient_uhid=patient.uhid if patient else "",
                    patient_age=patient.age if patient else None,
                    patient_gender=patient.gender if patient else None,
                    arrival_time=e.arrival_time,
                    arrival_source=e.arrival_source,
                    triage_level=e.triage_level,
                    status=e.status,
                    wait_time_minutes=wait_minutes,
                    is_escalated=e.is_escalated,
                    escalation_reason=e.escalation_reason,
                    attending_doctor_name=e.attending_doctor.name if e.attending_doctor else None,
                )
            )
        return items


class EscalateEmergencyEncounterAction:
    """Action for Feature 3: Automated/Manual Escalation for Deteriorating Patients."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = EmergencyRepository(db, hospital_id)

    def execute(self, encounter_id: UUID, reason: str, actor: dict[str, Any]) -> EmergencyEncounterResponse:
        encounter = self.repo.get_encounter_by_id(encounter_id)
        if not encounter:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Emergency encounter not found")

        encounter.is_escalated = True
        encounter.escalation_reason = reason.strip()
        self.repo.commit()
        self.repo.refresh(encounter)
        return EmergencyEncounterResponse.model_validate(encounter)


class RecordEmergencyDispositionAction:
    """Action for Feature 5: Emergency Disposition Tracking."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = EmergencyRepository(db, hospital_id)

    def execute(
        self,
        encounter_id: UUID,
        payload: EmergencyDispositionCreate,
        actor: dict[str, Any],
    ) -> EmergencyDispositionResponse:
        encounter = self.repo.get_encounter_by_id(encounter_id)
        if not encounter:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Emergency encounter not found")

        assert_can_set_disposition(encounter)
        validate_disposition_payload(
            payload.disposition_type,
            payload.destination_ward_id,
            payload.destination_bed_id,
            payload.transfer_facility,
        )

        decider_name = str(actor.get("name") or "Emergency Physician")
        decider_id_raw = actor.get("user_id")
        decider_id = UUID(str(decider_id_raw)) if decider_id_raw else None
        # Handle inpatient admission creation if destination ward and bed are provided
        created_admission_id: UUID | None = None
        if payload.disposition_type in (
            EmergencyDispositionType.admit_ipd,
            EmergencyDispositionType.admit_icu,
            EmergencyDispositionType.observation_ward,
        ) and payload.destination_bed_id and payload.destination_ward_id:
            from hms_migration.modules.beds.entities.bed import Bed
            from hms_migration.modules.inpatient.db.admissions_repository import AdmissionsRepository
            from hms_migration.modules.inpatient.services.inpatient_billing_service import (
                InpatientBillingService,
                next_ip_encounter_id,
            )

            bed = (
                self.db.query(Bed)
                .filter(Bed.id == payload.destination_bed_id, Bed.hospital_id == self.hospital_id)
                .first()
            )
            if bed and not bed.is_occupied:
                admissions_repo = AdmissionsRepository(self.db)
                billing_svc = InpatientBillingService(self.db)
                patient = encounter.patient or self.db.query(Patient).filter(Patient.id == encounter.patient_id).first()
                if patient:
                    ip_id = next_ip_encounter_id(self.db, self.hospital_id)
                    admission = admissions_repo.create_admission(
                        hospital_id=self.hospital_id,
                        patient=patient,
                        bed=bed,
                        ward_id=payload.destination_ward_id,
                        room_id=bed.room_id,
                        doctor_id=encounter.attending_doctor_id,
                        ip_id=ip_id,
                        notes=f"Admitted via Emergency ({encounter.er_id}). {payload.disposition_notes or ''}".strip(),
                        admitted_at=datetime.now(timezone.utc),
                    )
                    admission.er_id = encounter.er_id
                    created_admission_id = admission.id
                    from hms_migration.modules.beds.entities.bed import Ward
                    ward_obj = self.db.query(Ward).filter(Ward.id == payload.destination_ward_id).first()
                    billing_svc.ensure_admission_charge(
                        hospital_id=self.hospital_id,
                        patient_id=patient.id,
                        admission_id=admission.id,
                        ward_name=ward_obj.name if ward_obj else None,
                        admission_fee=float(getattr(ward_obj, "admission_fee", 0.0) or 0.0),
                        created_by_name=decider_name,
                    )

        disposition = EmergencyDisposition(
            hospital_id=self.hospital_id,
            encounter_id=encounter_id,
            disposition_type=payload.disposition_type,
            destination_ward_id=payload.destination_ward_id,
            destination_bed_id=payload.destination_bed_id,
            admission_id=created_admission_id,
            transfer_facility=payload.transfer_facility.strip() if payload.transfer_facility else None,
            disposition_notes=payload.disposition_notes.strip() if payload.disposition_notes else None,
            decided_by_id=decider_id,
            decided_by_name=decider_name,
            decided_at=datetime.now(timezone.utc),
        )
        self.repo.add(disposition)

        # Update encounter status based on disposition
        if payload.disposition_type in (
            EmergencyDispositionType.discharge_home,
            EmergencyDispositionType.transfer_tertiary,
            EmergencyDispositionType.lama,
            EmergencyDispositionType.deceased,
        ):
            encounter.status = EmergencyStatus.completed
        elif created_admission_id:
            encounter.status = EmergencyStatus.completed
        else:
            encounter.status = EmergencyStatus.disposition_planned

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="disposition",
            entity_type="emergency_encounter",
            entity_id=encounter.id,
            summary=f"Recorded disposition {payload.disposition_type.value} for {encounter.er_id}",
            details={
                "disposition_type": payload.disposition_type.value,
                "er_id": encounter.er_id,
                "admission_id": str(created_admission_id) if created_admission_id else None,
            },
        )
        self.repo.commit()
        self.repo.refresh(disposition)
        return EmergencyDispositionResponse.model_validate(disposition)
