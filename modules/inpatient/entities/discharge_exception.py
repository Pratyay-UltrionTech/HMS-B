"""Discharge Financial Exception entity model."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
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
from modules.patients.entities.patient import Patient


class ExceptionStatus(str, enum.Enum):
    """Approval lifecycle of a discharge financial exception."""

    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class DischargeFinancialException(Base):
    """
    Controlled financial discharge exception entity.
    Allows administrative discharge when remaining patient balance > 0 under approved policy,
    without zeroing out or erasing the remaining billing receivable.
    """

    __tablename__ = "discharge_financial_exceptions"
    __table_args__ = (
        Index("ix_discharge_exc_hospital_id", "hospital_id"),
        Index("ix_discharge_exc_admission", "hospital_id", "admission_id"),
        Index("ix_discharge_exc_status", "hospital_id", "status"),
        CheckConstraint(
            "status != 'approved' OR (approved_by_id IS NOT NULL AND approved_by_id != requested_by_id)",
            name="chk_discharge_exception_separation_of_duties",
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
    financial_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    outstanding_amount_at_request: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    approved_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    remaining_receivable: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[ExceptionStatus] = mapped_column(
        Enum(ExceptionStatus, name="discharge_exception_status"),
        nullable=False,
        default=ExceptionStatus.pending,
        index=True,
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship("Patient", foreign_keys=[patient_id])
    admission: Mapped["Admission"] = relationship("Admission", foreign_keys=[admission_id])
