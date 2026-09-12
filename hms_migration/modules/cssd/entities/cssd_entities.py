"""
Target-native SQLAlchemy entities for the CSSD (Central Sterile Services Department) domain.

Features covered:
- Feature 39: Instrument Set Catalogue (InstrumentSet, InstrumentSetItem)
- Feature 40: Sterilization Batch (SterilizationBatch, SterilizationBatchItem)
- Feature 41: Sterilization QC (SterilizationQC)
- Feature 42: Issue & Return Tracking (InstrumentSetIssue)
- Feature 43: Missing/Damaged Instrument Tracking (InstrumentDiscrepancyReport)

Tables:
- cssd_instrument_sets
- cssd_instrument_set_items
- cssd_sterilization_batches
- cssd_sterilization_batch_items
- cssd_sterilization_qc
- cssd_instrument_set_issues
- cssd_instrument_discrepancy_reports

Deviation note: Instrument sets and sterilization batches are deliberately kept separate
from `modules/equipment` (EquipmentCategory/EquipmentItem) — sterile instrument sets are
tracked as reusable kits with a composition (InstrumentSetItem), a sterilization/QC
lifecycle, and a issue/return workflow that does not map onto generic equipment
assignment. No Department entity is FK-able in `modules/masters` in a way that is reused
elsewhere as a hard FK (equipment/OT modules also store department as a plain string), so
`owning_department` / `issued_to_department` here are plain strings for consistency with
the rest of the codebase. `machine_id` on SterilizationBatch is a plain string rather than
an EquipmentItem FK because there is no dedicated "Sterilizer/Autoclave" equipment category
in `modules/equipment` today.
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
    pass


class SterilizationMethod(str, enum.Enum):
    steam = "steam"
    eto = "eto"
    plasma = "plasma"


class SterilizationBatchStatus(str, enum.Enum):
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    released = "released"
    quarantined = "quarantined"


class QcResult(str, enum.Enum):
    pass_ = "pass"
    fail = "fail"


class BiologicalIndicatorResult(str, enum.Enum):
    pass_ = "pass"
    fail = "fail"
    pending = "pending"


class BowieDickResult(str, enum.Enum):
    pass_ = "pass"
    fail = "fail"
    not_applicable = "not_applicable"


class InstrumentSetIssueStatus(str, enum.Enum):
    issued = "issued"
    returned = "returned"
    overdue = "overdue"


class ReturnCondition(str, enum.Enum):
    intact = "intact"
    damaged = "damaged"
    missing_items = "missing_items"


class DiscrepancyType(str, enum.Enum):
    missing = "missing"
    damaged = "damaged"


class DiscrepancyInvestigationStatus(str, enum.Enum):
    open = "open"
    investigating = "investigating"
    resolved = "resolved"
    replaced = "replaced"


class InstrumentSet(Base):
    """Feature 39: Sterile instrument set master (catalogue)."""

    __tablename__ = "cssd_instrument_sets"
    __table_args__ = (UniqueConstraint("hospital_id", "set_code", name="uq_cssd_set_code_hospital"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    set_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    set_name: Mapped[str] = mapped_column(String(255), nullable=False)
    owning_department: Mapped[str | None] = mapped_column(String(128), nullable=True)
    instrument_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    items: Mapped[list["InstrumentSetItem"]] = relationship(back_populates="instrument_set", cascade="all, delete-orphan")
    batch_links: Mapped[list["SterilizationBatchItem"]] = relationship(back_populates="instrument_set")
    issues: Mapped[list["InstrumentSetIssue"]] = relationship(back_populates="instrument_set")


class InstrumentSetItem(Base):
    """Feature 39: Itemized composition of an instrument set."""

    __tablename__ = "cssd_instrument_set_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instrument_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cssd_instrument_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instrument_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    instrument_set: Mapped["InstrumentSet"] = relationship(back_populates="items")


class SterilizationBatch(Base):
    """Feature 40: A sterilization cycle run, holding one or more instrument sets."""

    __tablename__ = "cssd_sterilization_batches"
    __table_args__ = (UniqueConstraint("hospital_id", "batch_number", name="uq_cssd_batch_number_hospital"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sterilization_method: Mapped[SterilizationMethod] = mapped_column(
        Enum(SterilizationMethod, name="cssd_sterilization_method"), nullable=False
    )
    machine_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cycle_start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    cycle_end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    pressure_kpa: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[SterilizationBatchStatus] = mapped_column(
        Enum(SterilizationBatchStatus, name="cssd_sterilization_batch_status"),
        nullable=False,
        default=SterilizationBatchStatus.in_progress,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    items: Mapped[list["SterilizationBatchItem"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )
    qc: Mapped["SterilizationQC | None"] = relationship(back_populates="batch", uselist=False)
    issues: Mapped[list["InstrumentSetIssue"]] = relationship(back_populates="batch")


class SterilizationBatchItem(Base):
    """Feature 40: Which instrument sets were loaded into a sterilization batch."""

    __tablename__ = "cssd_sterilization_batch_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cssd_sterilization_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instrument_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cssd_instrument_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )

    batch: Mapped["SterilizationBatch"] = relationship(back_populates="items")
    instrument_set: Mapped["InstrumentSet"] = relationship(back_populates="batch_links")


class SterilizationQC(Base):
    """Feature 41: One QC record per sterilization batch."""

    __tablename__ = "cssd_sterilization_qc"
    __table_args__ = (UniqueConstraint("batch_id", name="uq_cssd_qc_batch"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cssd_sterilization_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chemical_indicator_result: Mapped[QcResult] = mapped_column(
        Enum(QcResult, name="cssd_qc_result_chemical"), nullable=False
    )
    biological_indicator_result: Mapped[BiologicalIndicatorResult] = mapped_column(
        Enum(BiologicalIndicatorResult, name="cssd_qc_result_biological"),
        nullable=False,
        default=BiologicalIndicatorResult.pending,
    )
    bowie_dick_test_result: Mapped[BowieDickResult | None] = mapped_column(
        Enum(BowieDickResult, name="cssd_qc_result_bowie_dick"), nullable=True
    )
    overall_result: Mapped[QcResult] = mapped_column(Enum(QcResult, name="cssd_qc_result_overall"), nullable=False)
    checked_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    batch: Mapped["SterilizationBatch"] = relationship(back_populates="qc")


class InstrumentSetIssue(Base):
    """Feature 42: Issue & return tracking of sterile instrument sets."""

    __tablename__ = "cssd_instrument_set_issues"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instrument_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cssd_instrument_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cssd_sterilization_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issued_to_department: Mapped[str] = mapped_column(String(128), nullable=False)
    issued_to_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issue_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expected_return_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[InstrumentSetIssueStatus] = mapped_column(
        Enum(InstrumentSetIssueStatus, name="cssd_issue_status"),
        nullable=False,
        default=InstrumentSetIssueStatus.issued,
    )
    return_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    return_condition: Mapped[ReturnCondition | None] = mapped_column(
        Enum(ReturnCondition, name="cssd_return_condition"), nullable=True
    )

    instrument_set: Mapped["InstrumentSet"] = relationship(back_populates="issues")
    batch: Mapped["SterilizationBatch"] = relationship(back_populates="issues")
    discrepancy_reports: Mapped[list["InstrumentDiscrepancyReport"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan"
    )


class InstrumentDiscrepancyReport(Base):
    """Feature 43: Missing/damaged instrument tracking, triggered from a set return."""

    __tablename__ = "cssd_instrument_discrepancy_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cssd_instrument_set_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    discrepancy_type: Mapped[DiscrepancyType] = mapped_column(
        Enum(DiscrepancyType, name="cssd_discrepancy_type"), nullable=False
    )
    instrument_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity_affected: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reported_by_staff_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    investigation_status: Mapped[DiscrepancyInvestigationStatus] = mapped_column(
        Enum(DiscrepancyInvestigationStatus, name="cssd_discrepancy_investigation_status"),
        nullable=False,
        default=DiscrepancyInvestigationStatus.open,
    )
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    issue: Mapped["InstrumentSetIssue"] = relationship(back_populates="discrepancy_reports")
