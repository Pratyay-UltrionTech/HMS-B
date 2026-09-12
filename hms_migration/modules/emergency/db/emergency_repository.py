"""
Database repository for Emergency Department domain.

Conforms to UltrionTech-Backend-Template modules/emergency/db/ specification.
All operations are tenant-isolated via hospital_id.
Strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Sequence
from uuid import UUID

from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.emergency.entities.emergency_entities import (
    EmergencyDisposition,
    EmergencyEncounter,
    EmergencyOrderStatus,
    EmergencyStatus,
    EmergencyTreatmentOrder,
    EmergencyTriageAssessment,
)
from hms_migration.modules.patients.entities.patient import Patient


class EmergencyRepository:
    """Encapsulated data access for Emergency domain."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def generate_next_er_id(self) -> str:
        """Generate human-readable ER encounter ID: ER-YYYY-NNNNN."""
        year = date.today().year
        prefix = f"ER-{year}-"
        latest = (
            self.db.query(func.max(EmergencyEncounter.er_id))
            .filter(
                EmergencyEncounter.hospital_id == self.hospital_id,
                EmergencyEncounter.er_id.like(f"{prefix}%"),
            )
            .scalar()
        )
        seq = 0
        if latest:
            try:
                seq = int(str(latest).rsplit("-", 1)[-1])
            except ValueError:
                seq = 0
        return f"{prefix}{seq + 1:05d}"

    def get_encounter_by_id(self, encounter_id: UUID) -> EmergencyEncounter | None:
        """Fetch emergency encounter by primary key within hospital context."""
        return (
            self.db.query(EmergencyEncounter)
            .options(
                joinedload(EmergencyEncounter.patient),
                joinedload(EmergencyEncounter.attending_doctor),
                joinedload(EmergencyEncounter.triage_assessment),
                joinedload(EmergencyEncounter.orders),
                joinedload(EmergencyEncounter.disposition),
            )
            .filter(
                EmergencyEncounter.id == encounter_id,
                EmergencyEncounter.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_active_patient_encounter(self, patient_id: UUID) -> EmergencyEncounter | None:
        """Fetch any active (non-completed/non-cancelled) encounter for a patient."""
        return (
            self.db.query(EmergencyEncounter)
            .filter(
                EmergencyEncounter.patient_id == patient_id,
                EmergencyEncounter.hospital_id == self.hospital_id,
                EmergencyEncounter.status.in_([
                    EmergencyStatus.registered,
                    EmergencyStatus.triaged,
                    EmergencyStatus.in_treatment,
                    EmergencyStatus.disposition_planned,
                ]),
            )
            .first()
        )

    def list_encounters(
        self,
        statuses: Sequence[EmergencyStatus] | None = None,
        search: str | None = None,
    ) -> list[EmergencyEncounter]:
        """List encounters matching filters."""
        q = (
            self.db.query(EmergencyEncounter)
            .options(
                joinedload(EmergencyEncounter.patient),
                joinedload(EmergencyEncounter.attending_doctor),
            )
            .filter(EmergencyEncounter.hospital_id == self.hospital_id)
        )
        if statuses:
            q = q.filter(EmergencyEncounter.status.in_(statuses))
        if search:
            term = f"%{search.strip()}%"
            q = q.join(EmergencyEncounter.patient).filter(
                or_(
                    EmergencyEncounter.er_id.ilike(term),
                    Patient.name.ilike(term),
                    Patient.uhid.ilike(term),
                    Patient.mobile.ilike(term),
                )
            )
        return q.order_by(EmergencyEncounter.arrival_time.desc()).all()

    def get_triage_queue(self) -> list[EmergencyEncounter]:
        """
        Maintains live priority-based emergency queue.
        Sorted by:
        1. is_escalated DESC (deteriorating patients at top)
        2. triage_level ASC NULLS LAST (Level 1 Resuscitation first)
        3. arrival_time ASC (longest waiting first)
        """
        active_statuses = [
            EmergencyStatus.registered,
            EmergencyStatus.triaged,
            EmergencyStatus.in_treatment,
            EmergencyStatus.disposition_planned,
        ]
        return (
            self.db.query(EmergencyEncounter)
            .options(
                joinedload(EmergencyEncounter.patient),
                joinedload(EmergencyEncounter.attending_doctor),
            )
            .filter(
                EmergencyEncounter.hospital_id == self.hospital_id,
                EmergencyEncounter.status.in_(active_statuses),
            )
            .order_by(
                desc(EmergencyEncounter.is_escalated),
                func.coalesce(EmergencyEncounter.triage_level, 99).asc(),
                EmergencyEncounter.arrival_time.asc(),
            )
            .all()
        )

    def add(self, entity: object) -> None:
        self.db.add(entity)

    def commit(self) -> None:
        self.db.commit()

    def flush(self) -> None:
        self.db.flush()

    def refresh(self, entity: object) -> None:
        self.db.refresh(entity)
