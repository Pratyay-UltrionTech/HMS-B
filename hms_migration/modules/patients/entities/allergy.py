"""
Target-native SQLAlchemy entity for Patient Allergies (Feature 16).

Covers active allergy tracking and medication safety interception.
Conforms to UltrionTech-Backend-Template modules/patients/entities/ specification.
"""

from __future__ import annotations

from datetime import date, datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.modules.patients.entities.patient import Patient


class AllergenType(str, enum.Enum):
    drug = "drug"
    food = "food"
    environmental = "environmental"
    other = "other"


class AllergySeverity(str, enum.Enum):
    mild = "mild"
    moderate = "moderate"
    severe = "severe"
    life_threatening = "life_threatening"


class PatientAllergy(Base):
    """
    Feature 16: Active Patient Allergy tracking.
    Intercepts medication orders and administrations if matching allergens are prescribed.
    """

    __tablename__ = "patient_allergies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    allergen: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    allergen_type: Mapped[AllergenType] = mapped_column(
        Enum(AllergenType, name="allergen_type"),
        nullable=False,
        default=AllergenType.drug,
        index=True,
    )
    severity: Mapped[AllergySeverity] = mapped_column(
        Enum(AllergySeverity, name="allergy_severity"),
        nullable=False,
        default=AllergySeverity.moderate,
    )
    reaction: Mapped[str | None] = mapped_column(String(255), nullable=True)
    diagnosed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    recorded_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship("Patient")
