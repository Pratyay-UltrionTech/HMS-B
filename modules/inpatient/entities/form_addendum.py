"""IPD Form Addendum entity model."""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.postgres.base import Base


class IpdFormAddendum(Base):
    """
    Chronological addendum attached to a finalized IPD clinical form submission.
    Preserves legal immutability of finalized records while recording subsequent notes.
    """

    __tablename__ = "ipd_form_addenda"
    __table_args__ = (
        Index("ix_form_addenda_hospital", "hospital_id"),
        Index("ix_form_addenda_submission", "hospital_id", "submission_id"),
        Index("ix_form_addenda_admission", "hospital_id", "admission_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ipd_form_submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="RESTRICT"), nullable=False
    )
    author_name: Mapped[str] = mapped_column(String(255), nullable=False)
    author_role: Mapped[str] = mapped_column(String(64), nullable=False, default="Clinician")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    submission: Mapped["IpdFormSubmission"] = relationship("IpdFormSubmission", foreign_keys=[submission_id])
