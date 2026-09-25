"""Care Team entity model for inpatient admissions."""

from __future__ import annotations

from datetime import datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.postgres.base import Base
from modules.doctors.entities.doctor import HospitalUser


class AdmissionCareTeamRole(str, enum.Enum):
    """Clinical roles within an admission care team."""

    primary_consultant = "primary_consultant"
    admitting_doctor = "admitting_doctor"
    consulting_doctor = "consulting_doctor"
    resident = "resident"


class AdmissionCareTeamMember(Base):
    """
    Care team member for an inpatient admission.
    Enforces role separation, active status, attribution, and single active primary consultant.
    """

    __tablename__ = "admission_care_team_members"
    __table_args__ = (
        Index("ix_care_team_hospital_id", "hospital_id"),
        Index("ix_care_team_admission_active", "hospital_id", "admission_id", "is_active"),
        Index("ix_care_team_doctor", "hospital_id", "doctor_id"),
        Index(
            "uq_admission_care_team_active_primary",
            "hospital_id",
            "admission_id",
            unique=True,
            postgresql_where=text("is_active IS TRUE AND role = 'primary_consultant'"),
            sqlite_where=text("is_active = 1 AND role = 'primary_consultant'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[AdmissionCareTeamRole] = mapped_column(
        Enum(AdmissionCareTeamRole, name="admission_care_team_role"),
        nullable=False,
        default=AdmissionCareTeamRole.consulting_doctor,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    doctor: Mapped[HospitalUser] = relationship("HospitalUser", foreign_keys=[doctor_id])
    admission: Mapped["Admission"] = relationship("Admission", foreign_keys=[admission_id], back_populates="care_team")
