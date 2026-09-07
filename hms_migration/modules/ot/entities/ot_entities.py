"""
Target-native SQLAlchemy entities for Operation Theatre (OT) domain.

Tables:
- ot_rooms
- ot_surgeries
"""

from __future__ import annotations

from datetime import datetime
import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base

if TYPE_CHECKING:
    from hms_migration.modules.doctors.entities.doctor import HospitalUser
    from hms_migration.modules.masters.entities.organization_entities import Department, Wing
    from hms_migration.modules.patients.entities.patient import Patient


class OtPriority(str, enum.Enum):
    emergency = "emergency"
    urgent = "urgent"
    elective = "elective"


class OtSurgeryStatus(str, enum.Enum):
    scheduled = "scheduled"
    confirmed = "confirmed"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class OtRoom(Base):
    """Operation theatre rooms configured under Masters → Organization → OT."""

    __tablename__ = "ot_rooms"
    __table_args__ = (
        UniqueConstraint("hospital_id", "code", name="uq_ot_room_hospital_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    desk_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    base_ot_charge: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    wing: Mapped["Wing | None"] = relationship("Wing", foreign_keys=[wing_id])
    department: Mapped["Department"] = relationship("Department", foreign_keys=[department_id])


class OtSurgery(Base):
    """Operation Theatre surgery schedule, clinical notes, and post-op lifecycle."""

    __tablename__ = "ot_surgeries"
    __table_args__ = (
        UniqueConstraint("hospital_id", "surgery_no", name="uq_ot_surgery_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    surgery_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    surgeon_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assistant_surgeon: Mapped[str | None] = mapped_column(String(255), nullable=True)
    surgery_type: Mapped[str] = mapped_column(String(255), nullable=False)
    surgery_category: Mapped[str] = mapped_column(String(128), nullable=False, default="General")
    priority: Mapped[OtPriority] = mapped_column(
        Enum(OtPriority, name="ot_priority"),
        nullable=False,
        default=OtPriority.elective,
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ot_room_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ot_rooms.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ot_room: Mapped[str] = mapped_column(String(64), nullable=False, default="OT-1")
    ot_charge_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    anaesthetist: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    booked_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    booked_by_role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[OtSurgeryStatus] = mapped_column(
        Enum(OtSurgeryStatus, name="ot_surgery_status"),
        nullable=False,
        default=OtSurgeryStatus.scheduled,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shifted_to: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Operation notes
    pre_op_diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    procedure_performed: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings: Mapped[str | None] = mapped_column(Text, nullable=True)
    implants_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    complications: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_op_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    follow_up_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes_recorded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Attachments
    ot_report_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ot_report_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consent_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    surgeon: Mapped["HospitalUser | None"] = relationship("HospitalUser", foreign_keys=[surgeon_id])
    department: Mapped["Department | None"] = relationship("Department", foreign_keys=[department_id])
    ot_room_ref: Mapped["OtRoom | None"] = relationship("OtRoom", foreign_keys=[ot_room_id])
