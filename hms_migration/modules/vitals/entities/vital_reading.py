"""
Independent VitalReading domain entity.

Conforms to UltrionTech-Backend-Template modules/vitals/entities/ specification.
Maps directly to the vital_readings table on the target architecture Base metadata,
completely eliminating runtime imports of app.models.VitalReading.
"""

from datetime import datetime
import uuid

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from hms_migration.infrastructure.postgres.base import Base


class VitalReading(Base):
    """OPD vital sign captured for a visit (e.g. BP, Pulse, Temperature)."""

    __tablename__ = "vital_readings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    suitable_range: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[str] = mapped_column(String(128), nullable=False)
    recorded_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
