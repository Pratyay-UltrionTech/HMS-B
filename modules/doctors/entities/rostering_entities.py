"""
Rostering entities for multi-segment daily schedules and recurring work patterns.

Conforms to UltrionTech-Backend-Template modules/doctors/entities/ specification.
Provides normalized entities for:
- StaffRosterDay (one employee + one date -> zero or many segments)
- StaffRosterSegment (independent duty period with real datetime boundaries, duty kind, presence type)
- StaffWorkPattern (default recurring weekly schedule container)
- StaffWorkPatternSegment (recurring weekday duty period template)
"""

from __future__ import annotations

from datetime import date, datetime, time
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.postgres.base import Base


class DutyKind(str, enum.Enum):
    OPD = "OPD"
    WARD = "WARD"
    ICU = "ICU"
    EMERGENCY = "EMERGENCY"
    OT = "OT"
    LAB = "LAB"
    RADIOLOGY = "RADIOLOGY"
    ON_CALL = "ON_CALL"
    ADMIN = "ADMIN"
    TRAINING = "TRAINING"
    OTHER = "OTHER"


class PresenceType(str, enum.Enum):
    ONSITE = "ONSITE"
    ON_CALL = "ON_CALL"
    REMOTE = "REMOTE"


class RosterDayStatus(str, enum.Enum):
    WORKING = "working"
    OFF = "off"
    LEAVE = "leave"


class StaffRosterDay(Base):
    """Parent daily roster container for one staff member on a specific date."""

    __tablename__ = "staff_roster_days"
    __table_args__ = (
        UniqueConstraint("hospital_id", "user_id", "roster_date", name="uq_staff_roster_day"),
        Index("ix_staff_roster_days_hosp_date", "hospital_id", "roster_date"),
        Index("ix_staff_roster_days_lookup", "hospital_id", "user_id", "roster_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    roster_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="working")  # working | off | leave
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["HospitalUser"] = relationship("HospitalUser", foreign_keys=[user_id])
    segments: Mapped[list["StaffRosterSegment"]] = relationship(
        "StaffRosterSegment",
        back_populates="roster_day",
        cascade="all, delete-orphan",
        order_by=lambda: StaffRosterSegment.start_at,
    )



class StaffRosterSegment(Base):
    """Independent duty period assignment within a staff roster day with real datetime boundaries."""

    __tablename__ = "staff_roster_segments"
    __table_args__ = (
        Index("ix_roster_seg_hosp_user_dates", "hospital_id", "user_id", "start_at", "end_at"),
        Index("ix_roster_seg_hosp_dept_dates", "hospital_id", "department_id", "start_at"),
        Index("ix_roster_seg_hosp_dates", "hospital_id", "start_at", "end_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    roster_day_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff_roster_days.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shift_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shift_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    duty_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="OTHER")
    presence_type: Mapped[str] = mapped_column(String(32), nullable=False, default="ONSITE")
    is_bookable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    roster_day: Mapped["StaffRosterDay"] = relationship("StaffRosterDay", back_populates="segments")
    user: Mapped["HospitalUser"] = relationship("HospitalUser", foreign_keys=[user_id])
    shift_type: Mapped["ShiftType | None"] = relationship("ShiftType", foreign_keys=[shift_type_id])
    department: Mapped["Department | None"] = relationship(
        "Department", foreign_keys=[department_id]
    )


class StaffWorkPattern(Base):
    """Recurring default work schedule container for an employee."""

    __tablename__ = "staff_work_patterns"
    __table_args__ = (
        UniqueConstraint("hospital_id", "user_id", name="uq_staff_work_pattern_user"),
        Index("ix_staff_work_patterns_lookup", "hospital_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="Default Recurring Schedule")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["HospitalUser"] = relationship("HospitalUser", foreign_keys=[user_id])
    segments: Mapped[list["StaffWorkPatternSegment"]] = relationship(
        "StaffWorkPatternSegment",
        back_populates="pattern",
        cascade="all, delete-orphan",
        order_by=lambda: (StaffWorkPatternSegment.day_of_week, StaffWorkPatternSegment.start_time),
    )



class StaffWorkPatternSegment(Base):
    """Normalized recurring duty segment template for a specific weekday."""

    __tablename__ = "staff_work_pattern_segments"
    __table_args__ = (
        Index("ix_work_pattern_seg_pattern_day", "pattern_id", "day_of_week"),
        Index("ix_work_pattern_seg_hosp_day", "hospital_id", "day_of_week"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pattern_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff_work_patterns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Monday .. 6=Sunday (ISO weekday)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    shift_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shift_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    duty_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="OTHER")
    presence_type: Mapped[str] = mapped_column(String(32), nullable=False, default="ONSITE")
    is_bookable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    pattern: Mapped["StaffWorkPattern"] = relationship("StaffWorkPattern", back_populates="segments")
    shift_type: Mapped["ShiftType | None"] = relationship("ShiftType", foreign_keys=[shift_type_id])
    department: Mapped["Department | None"] = relationship(
        "Department", foreign_keys=[department_id]
    )
