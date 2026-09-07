"""
Target-native SQLAlchemy entities for Radiology domain.

Tables:
- radiology_scan_catalog
- radiology_orders
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

if TYPE_CHECKING:
    from hms_migration.modules.doctors.entities.doctor import HospitalUser
    from hms_migration.modules.patients.entities.patient import Patient


class RadiologyOrderStatus(str, enum.Enum):
    ordered = "ordered"
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class RadiologyScanCatalog(Base):
    """Catalog of radiology and diagnostic imaging scans."""

    __tablename__ = "radiology_scan_catalog"
    __table_args__ = (
        UniqueConstraint("hospital_id", "scan_code", name="uq_rad_scan_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scan_code: Mapped[str] = mapped_column(String(32), nullable=False)
    scan_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="General")
    department: Mapped[str] = mapped_column(String(128), nullable=False, default="Radiology")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RadiologyOrder(Base):
    """Order for a radiology/imaging scan with scheduling, execution, and reporting."""

    __tablename__ = "radiology_orders"
    __table_args__ = (
        UniqueConstraint("hospital_id", "order_no", name="uq_rad_order_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radiology_scan_catalog.id", ondelete="SET NULL"), nullable=True
    )
    scan_code: Mapped[str] = mapped_column(String(32), nullable=False)
    scan_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ordered_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    ordered_by_role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[RadiologyOrderStatus] = mapped_column(
        Enum(RadiologyOrderStatus, name="radiology_order_status"),
        nullable=False,
        default=RadiologyOrderStatus.ordered,
    )
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    machine: Mapped[str | None] = mapped_column(String(128), nullable=True)
    technician_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    findings: Mapped[str | None] = mapped_column(Text, nullable=True)
    impression: Mapped[str | None] = mapped_column(Text, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    report_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_uploaded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ordered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    doctor: Mapped["HospitalUser | None"] = relationship("HospitalUser", foreign_keys=[doctor_id])
    scan: Mapped["RadiologyScanCatalog | None"] = relationship("RadiologyScanCatalog", foreign_keys=[scan_id])
