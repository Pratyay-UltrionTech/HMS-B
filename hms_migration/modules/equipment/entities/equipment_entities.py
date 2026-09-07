"""
Target-native SQLAlchemy entities for Equipment domain.

Tables:
- equipment_categories
- equipment_items
- equipment_assignments
- equipment_maintenances
- equipment_service_logs
- equipment_requests
"""

from __future__ import annotations

from datetime import date, datetime
import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import (
    Boolean,
    Date,
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


class EquipmentStatus(str, enum.Enum):
    available = "available"
    in_use = "in_use"
    under_maintenance = "under_maintenance"
    out_of_service = "out_of_service"


class EquipmentAssignTarget(str, enum.Enum):
    department = "department"
    room = "room"
    doctor = "doctor"
    nurse = "nurse"
    patient = "patient"


class MaintenanceStatus(str, enum.Enum):
    scheduled = "scheduled"
    due = "due"
    ok = "ok"
    completed = "completed"
    overdue = "overdue"


class EquipmentRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    assigned = "assigned"


class EquipmentCategory(Base):
    __tablename__ = "equipment_categories"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_equip_cat_hospital_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    equipment: Mapped[list["EquipmentItem"]] = relationship(back_populates="category")


class EquipmentItem(Base):
    __tablename__ = "equipment_items"
    __table_args__ = (UniqueConstraint("hospital_id", "asset_id", name="uq_equip_asset_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    manufacturer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchase_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    department: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[EquipmentStatus] = mapped_column(
        Enum(EquipmentStatus, name="equipment_status"),
        nullable=False,
        default=EquipmentStatus.available,
    )
    # AMC / Warranty
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    warranty_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    warranty_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    amc_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    amc_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    vendor_contact: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    category: Mapped["EquipmentCategory | None"] = relationship(back_populates="equipment")
    assignments: Mapped[list["EquipmentAssignment"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )
    maintenances: Mapped[list["EquipmentMaintenance"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )
    service_logs: Mapped[list["EquipmentServiceLog"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )


class EquipmentAssignment(Base):
    __tablename__ = "equipment_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[EquipmentAssignTarget] = mapped_column(
        Enum(EquipmentAssignTarget, name="equipment_assign_target"),
        nullable=False,
        default=EquipmentAssignTarget.department,
    )
    target_name: Mapped[str] = mapped_column(String(255), nullable=False)
    assigned_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    equipment: Mapped["EquipmentItem"] = relationship(back_populates="assignments")


class EquipmentMaintenance(Base):
    __tablename__ = "equipment_maintenances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    last_service_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_service_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[MaintenanceStatus] = mapped_column(
        Enum(MaintenanceStatus, name="maintenance_status"),
        nullable=False,
        default=MaintenanceStatus.scheduled,
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    equipment: Mapped["EquipmentItem"] = relationship(back_populates="maintenances")


class EquipmentServiceLog(Base):
    __tablename__ = "equipment_service_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    work_done: Mapped[str] = mapped_column(Text, nullable=False)
    engineer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    equipment: Mapped["EquipmentItem"] = relationship(back_populates="service_logs")


class EquipmentRequest(Base):
    __tablename__ = "equipment_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    department: Mapped[str] = mapped_column(String(128), nullable=False)
    equipment_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[EquipmentRequestStatus] = mapped_column(
        Enum(EquipmentRequestStatus, name="equipment_request_status"),
        nullable=False,
        default=EquipmentRequestStatus.pending,
    )
    requested_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_equipment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
