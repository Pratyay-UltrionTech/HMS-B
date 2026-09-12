"""
Actions for Feature 9: Code Blue Resuscitation Incident Management.

Manages activation, arrival timestamps, CPR cycle / defibrillation event logging, and ROSC outcome.
Conforms to UltrionTech-Backend-Template modules/critical_care/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.critical_care.contracts.critical_care_contracts import (
    CodeBlueConcludeRequest,
    CodeBlueEventCreate,
    CodeBlueEventResponse,
    CodeBlueIncidentCreate,
    CodeBlueIncidentResponse,
)
from hms_migration.modules.critical_care.db.critical_care_repository import CriticalCareRepository
from hms_migration.modules.critical_care.entities.critical_care_entities import (
    CodeBlueEvent,
    CodeBlueIncident,
    CodeBlueStatus,
)
from hms_migration.shared.audit.service import write_audit_log


class ActivateCodeBlueAction:
    """Action to initiate an in-hospital Code Blue emergency resuscitation event."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, payload: CodeBlueIncidentCreate, actor: dict[str, Any]) -> CodeBlueIncidentResponse:
        code = self.repo.generate_next_code_blue_code()
        activator_name = str(actor.get("name") or "Emergency Staff")

        incident = CodeBlueIncident(
            hospital_id=self.hospital_id,
            incident_code=code,
            patient_id=payload.patient_id,
            location_description=payload.location_description.strip(),
            ward_id=payload.ward_id,
            bed_id=payload.bed_id,
            status=CodeBlueStatus.activated,
            activated_at=datetime.now(timezone.utc),
            activated_by_name=activator_name,
            team_members=payload.team_members or {},
        )
        self.repo.add(incident)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="activate_code_blue",
            entity_type="code_blue",
            entity_id=incident.id,
            summary=f"Activated Code Blue {code} at {payload.location_description}",
            details={"code": code, "location": payload.location_description},
        )
        self.repo.commit()
        self.repo.refresh(incident)
        return CodeBlueIncidentResponse.model_validate(incident)


class MarkCodeBlueTeamArrivedAction:
    """Record resuscitation team arrival timestamp."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, incident_id: UUID, actor: dict[str, Any]) -> CodeBlueIncidentResponse:
        incident = self.repo.get_code_blue_by_id(incident_id)
        if not incident:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code Blue incident not found")

        incident.team_arrived_at = datetime.now(timezone.utc)
        incident.status = CodeBlueStatus.team_arrived

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="code_blue_team_arrived",
            entity_type="code_blue",
            entity_id=incident.id,
            summary=f"Resuscitation team arrived for Code Blue {incident.incident_code}",
        )
        self.repo.commit()
        self.repo.refresh(incident)
        return CodeBlueIncidentResponse.model_validate(incident)


class LogCodeBlueEventAction:
    """Log CPR cycle, defibrillation shock, or resuscitation medication event in timeline."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, incident_id: UUID, payload: CodeBlueEventCreate, actor: dict[str, Any]) -> CodeBlueEventResponse:
        incident = self.repo.get_code_blue_by_id(incident_id)
        if not incident:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code Blue incident not found")

        if incident.status == CodeBlueStatus.concluded:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot log events to concluded Code Blue")

        incident.status = CodeBlueStatus.in_progress
        recorder_name = str(actor.get("name") or "Recorder Nurse")

        event = CodeBlueEvent(
            hospital_id=self.hospital_id,
            incident_id=incident_id,
            event_time=datetime.now(timezone.utc),
            event_type=payload.event_type.strip(),
            details=payload.details.strip(),
            recorded_by_name=recorder_name,
        )
        self.repo.add(event)
        self.repo.commit()
        self.repo.refresh(event)
        return CodeBlueEventResponse.model_validate(event)


class ConcludeCodeBlueAction:
    """Conclude resuscitation event and document final clinical outcome (ROSC, ICU, deceased)."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CriticalCareRepository(db, hospital_id)

    def execute(self, incident_id: UUID, payload: CodeBlueConcludeRequest, actor: dict[str, Any]) -> CodeBlueIncidentResponse:
        incident = self.repo.get_code_blue_by_id(incident_id)
        if not incident:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code Blue incident not found")

        incident.status = CodeBlueStatus.concluded
        incident.concluded_at = datetime.now(timezone.utc)
        incident.outcome = payload.outcome
        incident.summary_notes = payload.summary_notes.strip()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="conclude_code_blue",
            entity_type="code_blue",
            entity_id=incident.id,
            summary=f"Concluded Code Blue {incident.incident_code} with outcome {payload.outcome.value}",
            details={"outcome": payload.outcome.value},
        )
        self.repo.commit()
        self.repo.refresh(incident)
        return CodeBlueIncidentResponse.model_validate(incident)
