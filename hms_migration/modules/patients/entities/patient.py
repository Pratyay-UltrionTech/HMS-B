"""
Independent Patient domain entity.

Conforms to UltrionTech-Backend-Template modules/patients/entities/ specification.
Maps directly to the patients table on the target architecture Base metadata,
completely independent of app.models.Patient and isolated from legacy ORM relationships.
"""

from datetime import date, datetime
import enum
import uuid

from sqlalchemy import Boolean, Date, DateTime, Enum, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from hms_migration.infrastructure.postgres.base import Base


class PatientStatus(str, enum.Enum):
    """Clinical and administrative status of a registered patient."""

    active = "active"
    inactive = "inactive"
    admitted = "admitted"


class Patient(Base):
    """Core patient demographic record and clinical identity root."""

    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("hospital_id", "mobile", name="uq_patient_hospital_mobile"),
        UniqueConstraint("hospital_id", "uhid", name="uq_patient_hospital_uhid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    uhid: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mobile: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    emergency_contact: Mapped[str | None] = mapped_column(String(64), nullable=True)
    emergency_contact_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    emergency_contact_relation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(16), nullable=True)
    has_insurance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    insurance_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    insurance_details: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True
    )
    status: Mapped[PatientStatus] = mapped_column(
        Enum(PatientStatus, name="patient_status"),
        nullable=False,
        default=PatientStatus.active,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
