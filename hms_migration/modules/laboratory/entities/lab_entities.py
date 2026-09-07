"""
Laboratory domain entities.

Strictly adheres to UltrionTech-Backend-Template entity conventions:
- Pure SQLAlchemy declarative entities inheriting from target Base.
- Hospital multi-tenant isolation via hospital_id.
- Exact table names, constraints, cascades, indexes, and enums matching HMS-B.
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
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base

if TYPE_CHECKING:
    from hms_migration.modules.patients.entities.patient import Patient
    from hms_migration.modules.doctors.entities.doctor import HospitalUser
    from hms_migration.modules.clinical_records.entities.clinical_record import Prescription


class LabSampleType(str, enum.Enum):
    blood = "blood"
    urine = "urine"
    stool = "stool"
    swab = "swab"
    sputum = "sputum"
    other = "other"


class LabOrderStatus(str, enum.Enum):
    ordered = "ordered"
    sample_collected = "sample_collected"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class LabOrderSource(str, enum.Enum):
    doctor_prescribed = "doctor_prescribed"
    self_requested = "self_requested"


class LabPrescriptionRequestStatus(str, enum.Enum):
    pending = "pending"
    partially_processed = "partially_processed"
    completed = "completed"
    cancelled = "cancelled"


class LabRequestItemStatus(str, enum.Enum):
    pending = "pending"
    ordered = "ordered"
    unavailable = "unavailable"
    cancelled = "cancelled"


class LabItemStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"


class LabTestCatalog(Base):
    """Pathology test catalogue item."""

    __tablename__ = "lab_test_catalog"
    __table_args__ = (
        UniqueConstraint("hospital_id", "test_code", name="uq_lab_test_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    test_code: Mapped[str] = mapped_column(String(32), nullable=False)
    test_name: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(128), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sample_type: Mapped[LabSampleType] = mapped_column(
        Enum(LabSampleType, name="lab_sample_type"),
        nullable=False,
        default=LabSampleType.blood,
    )
    tat_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LabTestPanel(Base):
    """Grouped lab panel (e.g. Lipid Panel) containing multiple catalogue tests."""

    __tablename__ = "lab_test_panels"
    __table_args__ = (
        UniqueConstraint("hospital_id", "panel_code", name="uq_lab_panel_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    panel_code: Mapped[str] = mapped_column(String(32), nullable=False)
    panel_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tests: Mapped[list[LabPanelTest]] = relationship(
        "LabPanelTest", back_populates="panel", cascade="all, delete-orphan", order_by="LabPanelTest.sort_order"
    )


class LabPanelTest(Base):
    """Many-to-many junction: panel ↔ catalogue test."""

    __tablename__ = "lab_panel_tests"
    __table_args__ = (
        UniqueConstraint("panel_id", "test_id", name="uq_lab_panel_test"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    panel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_test_panels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    test_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_test_catalog.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    panel: Mapped[LabTestPanel] = relationship("LabTestPanel", back_populates="tests")
    test: Mapped[LabTestCatalog] = relationship("LabTestCatalog")


class LabOrder(Base):
    """Laboratory order containing tests, samples, and results."""

    __tablename__ = "lab_orders"
    __table_args__ = (
        UniqueConstraint("hospital_id", "order_no", name="uq_lab_order_no"),
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
    prescription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    prescription_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lab_prescription_requests.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    order_source: Mapped[LabOrderSource] = mapped_column(
        Enum(LabOrderSource, name="lab_order_source"),
        nullable=False,
        default=LabOrderSource.self_requested,
    )
    ordered_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    ordered_by_role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[LabOrderStatus] = mapped_column(
        Enum(LabOrderStatus, name="lab_order_status"),
        nullable=False,
        default=LabOrderStatus.ordered,
    )
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_type: Mapped[LabSampleType | None] = mapped_column(
        Enum(LabSampleType, name="lab_sample_type", create_type=False), nullable=True
    )
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    collection_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    ordered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    doctor: Mapped["HospitalUser | None"] = relationship("HospitalUser", foreign_keys=[doctor_id])
    prescription: Mapped["Prescription | None"] = relationship("Prescription", foreign_keys=[prescription_id])
    prescription_request: Mapped["LabPrescriptionRequest | None"] = relationship(
        "LabPrescriptionRequest",
        back_populates="lab_order",
        foreign_keys=[prescription_request_id],
    )
    items: Mapped[list[LabOrderItem]] = relationship(
        "LabOrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="LabOrderItem.created_at",
    )
    results: Mapped[list[LabResult]] = relationship(
        "LabResult",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="LabResult.sort_order",
    )


class LabOrderItem(Base):
    """Line item within a laboratory order."""

    __tablename__ = "lab_order_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    test_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_test_catalog.id", ondelete="SET NULL"), nullable=True
    )
    panel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_test_panels.id", ondelete="SET NULL"), nullable=True, index=True
    )
    panel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    test_code: Mapped[str] = mapped_column(String(32), nullable=False)
    test_name: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[LabItemStatus] = mapped_column(
        Enum(LabItemStatus, name="lab_item_status"),
        nullable=False,
        default=LabItemStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[LabOrder] = relationship("LabOrder", back_populates="items")
    panel: Mapped[LabTestPanel | None] = relationship("LabTestPanel")


class LabPrescriptionRequest(Base):
    """Doctor-prescribed laboratory investigation awaiting fulfillment."""

    __tablename__ = "lab_prescription_requests"

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
    status: Mapped[LabPrescriptionRequestStatus] = mapped_column(
        Enum(LabPrescriptionRequestStatus, name="lab_prescription_request_status"),
        nullable=False,
        default=LabPrescriptionRequestStatus.pending,
    )
    prescribed_test_ids: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=list
    )
    prescribed_panel_ids: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=list
    )
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    lab_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped["Patient"] = relationship("Patient", foreign_keys=[patient_id])
    doctor: Mapped["HospitalUser"] = relationship("HospitalUser", foreign_keys=[doctor_id])
    prescription: Mapped["Prescription"] = relationship("Prescription", foreign_keys=[prescription_id])
    items: Mapped[list[LabPrescriptionRequestItem]] = relationship(
        "LabPrescriptionRequestItem",
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="LabPrescriptionRequestItem.sort_order",
        foreign_keys="LabPrescriptionRequestItem.request_id",
    )
    lab_order: Mapped[LabOrder | None] = relationship(
        "LabOrder",
        back_populates="prescription_request",
        foreign_keys="LabOrder.prescription_request_id",
        uselist=False,
    )


class LabPrescriptionRequestItem(Base):
    """Line item in a doctor-prescribed lab investigation request."""

    __tablename__ = "lab_prescription_request_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_prescription_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    test_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_test_catalog.id", ondelete="SET NULL"), nullable=True
    )
    panel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_test_panels.id", ondelete="SET NULL"), nullable=True, index=True
    )
    panel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    test_code: Mapped[str] = mapped_column(String(32), nullable=False)
    test_name: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[LabRequestItemStatus] = mapped_column(
        Enum(LabRequestItemStatus, name="lab_request_item_status"),
        nullable=False,
        default=LabRequestItemStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    request: Mapped[LabPrescriptionRequest] = relationship(
        "LabPrescriptionRequest", back_populates="items", foreign_keys=[request_id]
    )


class LabResult(Base):
    """Laboratory test result parameter observation."""

    __tablename__ = "lab_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_order_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parameter_name: Mapped[str] = mapped_column(String(255), nullable=False)
    result_value: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_range: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[LabOrder] = relationship("LabOrder", back_populates="results")
