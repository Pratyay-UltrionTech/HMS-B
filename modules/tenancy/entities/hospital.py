"""Target architecture entity for Hospital Tenancy."""

from __future__ import annotations

from datetime import datetime
import enum
import uuid

from sqlalchemy import JSON, Boolean, DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.postgres.base import Base


class PlanType(str, enum.Enum):
    """Hospital subscription plan tier."""

    basic = "basic"
    premium = "premium"
    platinum = "platinum"


class Hospital(Base):
    """Tenant hospital organization entity."""

    __tablename__ = "hospitals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[str] = mapped_column(
        String(12), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[PlanType] = mapped_column(
        Enum(PlanType, name="plan_type"), nullable=False, default=PlanType.basic
    )
    icon_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # IANA timezone identifier for this tenant (FLAW-007). Used by background
    # jobs (e.g. auto-cancel of missed appointments) to evaluate "local" time
    # against each hospital's configured timezone rather than server-local time.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    facility_settings: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True
    )
    gstin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pan: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
