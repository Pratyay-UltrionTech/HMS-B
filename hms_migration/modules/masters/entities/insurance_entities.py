"""
Target-native SQLAlchemy entities for Masters Insurance & TPA domain.

Table:
- insurance_providers
"""

from __future__ import annotations

from datetime import datetime, date
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from hms_migration.infrastructure.postgres.base import Base


class InsuranceProvider(Base):
    """Empanelled Insurance Company or Third Party Administrator (TPA)."""

    __tablename__ = "insurance_providers"
    __table_args__ = (
        UniqueConstraint("hospital_id", "code", name="uq_insurance_hospital_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="Private TPA", nullable=False)
    irdai_reg_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pre_auth_sla_hours: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    tariff_discount_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    tariff_notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_person: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    mou_valid_till: Mapped[date | None] = mapped_column(Date, nullable=True)
    active_claims_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_receivables_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
