"""Care Team repository for managing admission clinical ownership and care teams."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from modules.inpatient.entities.admission import Admission
from modules.inpatient.entities.care_team import AdmissionCareTeamMember, AdmissionCareTeamRole
from modules.doctors.entities.doctor import HospitalUser
from shared.exceptions.base import ValidationError


class CareTeamRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def list_care_team(
        self, admission_id: UUID, active_only: bool = False
    ) -> list[AdmissionCareTeamMember]:
        q = (
            self.db.query(AdmissionCareTeamMember)
            .options(joinedload(AdmissionCareTeamMember.doctor))
            .filter(
                AdmissionCareTeamMember.hospital_id == self.hospital_id,
                AdmissionCareTeamMember.admission_id == admission_id,
            )
        )
        if active_only:
            q = q.filter(AdmissionCareTeamMember.is_active == True)
        return q.order_by(AdmissionCareTeamMember.assigned_at.desc()).all()

    def get_member_by_id(self, member_id: UUID) -> AdmissionCareTeamMember | None:
        return (
            self.db.query(AdmissionCareTeamMember)
            .options(joinedload(AdmissionCareTeamMember.doctor))
            .filter(
                AdmissionCareTeamMember.id == member_id,
                AdmissionCareTeamMember.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_active_primary_consultant(
        self, admission_id: UUID
    ) -> AdmissionCareTeamMember | None:
        return (
            self.db.query(AdmissionCareTeamMember)
            .options(joinedload(AdmissionCareTeamMember.doctor))
            .filter(
                AdmissionCareTeamMember.hospital_id == self.hospital_id,
                AdmissionCareTeamMember.admission_id == admission_id,
                AdmissionCareTeamMember.role == AdmissionCareTeamRole.primary_consultant,
                AdmissionCareTeamMember.is_active == True,
            )
            .first()
        )

    def add_member(
        self,
        *,
        admission_id: UUID,
        doctor_id: UUID,
        role: AdmissionCareTeamRole,
        assigned_by_id: UUID | None = None,
        assigned_by_name: str | None = None,
        notes: str | None = None,
    ) -> AdmissionCareTeamMember:
        now = datetime.now(timezone.utc)
        admission = (
            self.db.query(Admission)
            .filter(
                Admission.id == admission_id,
                Admission.hospital_id == self.hospital_id,
            )
            .with_for_update()
            .first()
        )
        if not admission:
            raise ValidationError("Admission not found in this hospital")
        doctor = (
            self.db.query(HospitalUser)
            .filter(
                HospitalUser.id == doctor_id,
                HospitalUser.hospital_id == self.hospital_id,
            )
            .first()
        )
        if not doctor:
            raise ValidationError("Care team doctor must belong to this hospital")
        if getattr(doctor, "is_active", True) is False:
            raise ValidationError("Inactive or deactivated practitioner cannot be assigned to care team")

        # If adding a primary consultant, deactivate any existing active primary consultant
        # and record replacement audit trail (Section 15 & 17)
        if role == AdmissionCareTeamRole.primary_consultant:
            existing = self.get_active_primary_consultant(admission_id)
            if existing and existing.doctor_id != doctor_id:
                existing.is_active = False
                existing.ended_at = now
                old_name = getattr(existing.doctor, "name", None) or getattr(existing.doctor, "full_name", None) or str(existing.doctor_id)
                new_name = getattr(doctor, "name", None) or getattr(doctor, "full_name", None) or str(doctor_id)
                from shared.audit.service import write_audit_log
                write_audit_log(
                    self.db,
                    hospital_id=self.hospital_id,
                    actor={"id": str(assigned_by_id) if assigned_by_id else None, "name": assigned_by_name or "System"},
                    action="update",
                    entity_type="admission_care_team_member",
                    entity_id=str(existing.id),
                    summary=f"Primary consultant {old_name} ended and replaced by {new_name}",
                )

        member = AdmissionCareTeamMember(
            hospital_id=self.hospital_id,
            admission_id=admission_id,
            doctor_id=doctor_id,
            role=role,
            is_active=True,
            assigned_at=now,
            assigned_by_id=assigned_by_id,
            assigned_by_name=assigned_by_name,
            notes=notes,
        )
        self.db.add(member)

        # Keep admission.doctor_id in sync for backward compatibility if role is primary_consultant (Section 14)
        if role == AdmissionCareTeamRole.primary_consultant:
            admission.doctor_id = doctor_id

        self.db.flush()
        return member

    def end_assignment(self, member_id: UUID) -> AdmissionCareTeamMember | None:
        member = self.get_member_by_id(member_id)
        if not member:
            return None
        member.is_active = False
        member.ended_at = datetime.now(timezone.utc)
        self.db.flush()
        return member
