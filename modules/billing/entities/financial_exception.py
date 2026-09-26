"""Financial Clearance Exception entity model for reusable, scoped financial gating exceptions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import enum
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.postgres.base import Base


class FinancialExceptionStatus(str, enum.Enum):
    """Approval lifecycle of a financial clearance exception."""

    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    consumed = "consumed"


class FinancialClearanceException(Base):
    """
    Reusable, scoped financial clearance exception entity.
    Supports non-emergency administrative authorization (with separation of duties)
    and emergency clinical override (with immediate logging and no care delays).
    Does NOT forgive debt; outstanding amounts remain collectible on the financial account.
    """

    __tablename__ = "financial_clearance_exceptions"
    __table_args__ = (
        Index("ix_fin_exc_hospital_id", "hospital_id"),
        Index("ix_fin_exc_admission", "hospital_id", "admission_id"),
        Index("ix_fin_exc_patient", "hospital_id", "patient_id"),
        Index("ix_fin_exc_status", "hospital_id", "status"),
        Index("ix_fin_exc_service", "hospital_id", "service_type", "action_type"),
        CheckConstraint(
            "status != 'approved' OR is_emergency = TRUE OR (approved_by_id IS NOT NULL AND approved_by_id != requested_by_id)",
            name="chk_fin_clearance_separation_of_duties",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    financial_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    service_type: Mapped[str] = mapped_column(String(64), nullable=False)  # admission_bed, laboratory, radiology, pharmacy, ot, discharge
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)   # bed_allocation, sample_collection, scan_execution, dispense, surgery_start, discharge
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    is_emergency: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    required_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    amount_covered: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    shortfall_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)

    reason_category: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=FinancialExceptionStatus.pending.value, nullable=False, index=True)

    requested_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
