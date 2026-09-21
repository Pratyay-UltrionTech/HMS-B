"""
Target-native SQLAlchemy entities for Radiology domain.

Tables:
- radiology_scan_catalog
- radiology_orders
- rad_prescription_requests
- rad_prescription_request_items
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
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    case,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from infrastructure.postgres.base import Base

# Imported unconditionally (not just under TYPE_CHECKING): string-based
# relationship() targets are resolved from SQLAlchemy's mapper registry at
# first mapper configuration (same pattern as laboratory entities).
from modules.clinical_records.entities.clinical_record import Prescription

if TYPE_CHECKING:
    from modules.doctors.entities.doctor import HospitalUser
    from modules.patients.entities.patient import Patient


class RadiologyOrderStatus(str, enum.Enum):
    ordered = "ordered"
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class RadPrescriptionRequestStatus(str, enum.Enum):
    """Lifecycle of a doctor-prescribed radiology investigation request."""

    pending = "pending"
    partially_processed = "partially_processed"
    completed = "completed"
    cancelled = "cancelled"


class RadRequestItemStatus(str, enum.Enum):
    """Per-scan fulfillment state within a radiology prescription request."""

    pending = "pending"
    ordered = "ordered"
    unavailable = "unavailable"
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
        Index("ix_rad_orders_hospital_ordered", "hospital_id", "ordered_at"),
        Index("ix_rad_orders_hospital_status", "hospital_id", "status"),
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
    prescription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    prescription_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rad_prescription_requests.id", ondelete="SET NULL"), nullable=True, index=True
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
    report_file_data: Mapped[str | None] = mapped_column(Text, nullable=True, deferred=True)
    image_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_file_data: Mapped[str | None] = mapped_column(Text, nullable=True, deferred=True)
    has_report_file: Mapped[bool] = column_property(
        case(
            (report_file_data.isnot(None) & (report_file_data != ""), True),
            else_=False,
        )
    )
    has_image_file: Mapped[bool] = column_property(
        case(
            (image_file_data.isnot(None) & (image_file_data != ""), True),
            else_=False,
        )
    )
    report_uploaded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_amended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    amendment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ordered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    doctor: Mapped["HospitalUser | None"] = relationship("HospitalUser", foreign_keys=[doctor_id])
    scan: Mapped["RadiologyScanCatalog | None"] = relationship("RadiologyScanCatalog", foreign_keys=[scan_id])


class RadPrescriptionRequest(Base):
    """Doctor-prescribed radiology investigation awaiting fulfillment."""

    __tablename__ = "rad_prescription_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prescription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[RadPrescriptionRequestStatus] = mapped_column(
        Enum(RadPrescriptionRequestStatus, name="rad_prescription_request_status"),
        nullable=False,
        default=RadPrescriptionRequestStatus.pending,
    )
    prescribed_scan_ids: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=list
    )
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    doctor: Mapped["HospitalUser"] = relationship("HospitalUser", foreign_keys=[doctor_id])
    prescription: Mapped["Prescription"] = relationship("Prescription", foreign_keys=[prescription_id])
    items: Mapped[list[RadPrescriptionRequestItem]] = relationship(
        "RadPrescriptionRequestItem",
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="RadPrescriptionRequestItem.sort_order",
        foreign_keys="RadPrescriptionRequestItem.request_id",
    )


class RadPrescriptionRequestItem(Base):
    """Line item in a doctor-prescribed radiology investigation request."""

    __tablename__ = "rad_prescription_request_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rad_prescription_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radiology_scan_catalog.id", ondelete="SET NULL"), nullable=True
    )
    scan_code: Mapped[str] = mapped_column(String(32), nullable=False)
    scan_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[RadRequestItemStatus] = mapped_column(
        Enum(RadRequestItemStatus, name="rad_request_item_status"),
        nullable=False,
        default=RadRequestItemStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    request: Mapped[RadPrescriptionRequest] = relationship(
        "RadPrescriptionRequest", back_populates="items", foreign_keys=[request_id]
    )
