"""
Target architecture entities for Beds, Wards, and Rooms.

Conforms to UltrionTech-Backend-Template modules/beds/entities/ specification.
Maps directly to beds, wards, and rooms tables on the target architecture Base metadata.
"""

from __future__ import annotations

from datetime import datetime
import enum
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


class WardType(str, enum.Enum):
    """Ward category enum."""

    icu = "icu"
    general = "general"
    private = "private"
    emergency = "emergency"


class Ward(Base):
    """Ward unit within a hospital facility."""

    __tablename__ = "wards"
    __table_args__ = (
        UniqueConstraint("hospital_id", "name", name="uq_ward_hospital_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    wing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ward_type: Mapped[WardType] = mapped_column(
        Enum(WardType, name="ward_type"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    desk_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    admission_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bed_charge_per_day: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    rooms: Mapped[list[Room]] = relationship(
        "Room", back_populates="ward", cascade="all, delete-orphan"
    )


class Room(Base):
    """Room within a ward."""

    __tablename__ = "rooms"
    __table_args__ = (
        UniqueConstraint("hospital_id", "room_code", name="uq_room_hospital_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    ward_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ward: Mapped[Ward | None] = relationship("Ward", back_populates="rooms")
    beds: Mapped[list[Bed]] = relationship("Bed", back_populates="room")


class Bed(Base):
    """Inpatient bed unit."""

    __tablename__ = "beds"
    __table_args__ = (
        UniqueConstraint("hospital_id", "room_id", "bed_code", name="uq_bed_hospital_room_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    ward_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bed_code: Mapped[str] = mapped_column(String(64), nullable=False)
    is_occupied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ward: Mapped[Ward | None] = relationship("Ward", foreign_keys=[ward_id])
    room: Mapped[Room | None] = relationship("Room", back_populates="beds", foreign_keys=[room_id])
