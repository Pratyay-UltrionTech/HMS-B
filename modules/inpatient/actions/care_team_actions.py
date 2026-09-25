"""Actions for Inpatient Care Team management."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from modules.inpatient.contracts.inpatient_contracts import (
    AdmissionCareTeamCreate,
    AdmissionCareTeamResponse,
)
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.db.care_team_repository import CareTeamRepository
from modules.inpatient.entities.care_team import AdmissionCareTeamMember, AdmissionCareTeamRole
from shared.audit.service import write_audit_log
from shared.exceptions.base import NotFoundError, ValidationError


def _to_care_team_response(m: AdmissionCareTeamMember) -> AdmissionCareTeamResponse:
    doc = getattr(m, "doctor", None)
    return AdmissionCareTeamResponse(
        id=m.id,
        hospital_id=m.hospital_id,
        admission_id=m.admission_id,
        doctor_id=m.doctor_id,
        doctor_name=getattr(doc, "name", None) or getattr(doc, "full_name", None),
        doctor_department=getattr(doc, "department", None),
        role=m.role,
        is_active=m.is_active,
        assigned_at=m.assigned_at,
        assigned_by_id=m.assigned_by_id,
        assigned_by_name=m.assigned_by_name,
        ended_at=m.ended_at,
        notes=m.notes,
    )


class ListCareTeamAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CareTeamRepository(db, hospital_id)

    def execute(self, admission_id: UUID, active_only: bool = False) -> list[AdmissionCareTeamResponse]:
        members = self.repo.list_care_team(admission_id, active_only=active_only)
        return [_to_care_team_response(m) for m in members]


class AddCareTeamMemberAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CareTeamRepository(db, hospital_id)
        self.admissions_repo = AdmissionsRepository(db)

    def execute(
        self,
        admission_id: UUID,
        payload: AdmissionCareTeamCreate,
        actor: dict[str, Any],
    ) -> AdmissionCareTeamResponse:
        admission = self.admissions_repo.get_admission_by_id(self.hospital_id, admission_id)
        if not admission:
            raise NotFoundError("Admission not found")

        actor_id_str = actor.get("id") or actor.get("sub")
        actor_id = None
        if actor_id_str:
            try:
                actor_id = UUID(str(actor_id_str))
            except (ValueError, TypeError):
                actor_id = None
        actor_name = str(actor.get("name") or "Staff")

        member = self.repo.add_member(
            admission_id=admission_id,
            doctor_id=payload.doctor_id,
            role=payload.role,
            assigned_by_id=actor_id,
            assigned_by_name=actor_name,
            notes=payload.notes,
        )
        self.db.commit()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="create",
            entity_type="admission_care_team_member",
            entity_id=str(member.id),
            summary=f"Added {payload.role.value} to admission {admission.ip_id or admission.id}",
        )
        self.db.commit()
        return _to_care_team_response(member)


class EndCareTeamAssignmentAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = CareTeamRepository(db, hospital_id)

    def execute(self, member_id: UUID, actor: dict[str, Any]) -> AdmissionCareTeamResponse:
        member = self.repo.end_assignment(member_id)
        if not member:
            raise NotFoundError("Care team assignment not found")
        self.db.commit()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="update",
            entity_type="admission_care_team_member",
            entity_id=str(member.id),
            summary=f"Ended {member.role.value} assignment for admission {member.admission_id}",
        )
        self.db.commit()
        return _to_care_team_response(member)
