"""
Target-native SQLAlchemy entity for Drug Interaction Rules (Feature 17).

Conforms to UltrionTech-Backend-Template modules/pharmacy/entities/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from hms_migration.infrastructure.postgres.base import Base


class InteractionSeverity(str, enum.Enum):
    contraindicated = "contraindicated"
    major = "major"
    moderate = "moderate"
    minor = "minor"


class DrugInteractionRule(Base):
    """
    Feature 17: Drug Interaction Rule entity.
    Stores clinically verified interaction pairs, severity levels, mechanisms,
    and action recommendations for automated interception.
    """

    __tablename__ = "drug_interaction_rules"
    __table_args__ = (
        UniqueConstraint("hospital_id", "drug_a", "drug_b", name="uq_drug_interaction_pair"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    drug_a: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    drug_b: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    severity: Mapped[InteractionSeverity] = mapped_column(
        Enum(InteractionSeverity, name="interaction_severity"),
        nullable=False,
        default=InteractionSeverity.major,
        index=True,
    )
    mechanism: Mapped[str] = mapped_column(Text, nullable=False)
    clinical_recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
