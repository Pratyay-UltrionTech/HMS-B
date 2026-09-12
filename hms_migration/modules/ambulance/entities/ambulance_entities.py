"""
Target-native SQLAlchemy entities for Ambulance Management domain.

Tables:
- ambulance_vehicles
- ambulance_dispatches
"""

from __future__ import annotations

from datetime import datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.modules.emergency.entities.emergency_entities import EmergencyEncounter
from hms_migration.modules.patients.entities.patient import Patient


class AmbulanceType(str, enum.Enum):
    bls = "bls"
    als = "als"
    neonatal = "neonatal"
    patient_transport = "patient_transport"


class AmbulanceOperationalStatus(str, enum.Enum):
    available = "available"
    on_trip = "on_trip"
    maintenance = "maintenance"
    out_of_service = "out_of_service"


class DispatchPriority(str, enum.Enum):
    emergency = "emergency"
    urgent = "urgent"
    routine = "routine"


class DispatchStatus(str, enum.Enum):
    requested = "requested"
    dispatched = "dispatched"
    at_scene = "at_scene"
    transporting = "transporting"
    completed = "completed"
    cancelled = "cancelled"


class AmbulanceVehicle(Base):
    """Emergency ambulance vehicle registry."""

    __tablename__ = "ambulance_vehicles"
    __table_args__ = (
        UniqueConstraint("hospital_id", "registration_number", name="uq_ambulance_hospital_reg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    registration_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    vehicle_type: Mapped[AmbulanceType] = mapped_column(
        Enum(AmbulanceType, name="ambulance_type"), nullable=False, default=AmbulanceType.bls
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    operational_status: Mapped[AmbulanceOperationalStatus] = mapped_column(
        Enum(AmbulanceOperationalStatus, name="ambulance_operational_status"),
        nullable=False,
        default=AmbulanceOperationalStatus.available,
        index=True,
    )
    onboard_equipment: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    current_driver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    current_driver_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_paramedic_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    current_paramedic_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    dispatches: Mapped[list[AmbulanceDispatch]] = relationship(
        "AmbulanceDispatch", back_populates="ambulance", cascade="all, delete-orphan"
    )


class AmbulanceDispatch(Base):
    """Emergency ambulance dispatch request and trip log."""

    __tablename__ = "ambulance_dispatches"
    __table_args__ = (
        UniqueConstraint("hospital_id", "dispatch_code", name="uq_dispatch_hospital_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    dispatch_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ambulance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ambulance_vehicles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    emergency_encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emergency_encounters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    caller_name: Mapped[str] = mapped_column(String(255), nullable=False)
    caller_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    pickup_location: Mapped[str] = mapped_column(String(255), nullable=False)
    drop_location: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[DispatchPriority] = mapped_column(
        Enum(DispatchPriority, name="dispatch_priority"), nullable=False, default=DispatchPriority.emergency
    )
    driver_name: Mapped[str] = mapped_column(String(255), nullable=False)
    paramedic_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[DispatchStatus] = mapped_column(
        Enum(DispatchStatus, name="dispatch_status"), nullable=False, default=DispatchStatus.dispatched, index=True
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    arrived_pickup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    departed_pickup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    arrived_destination_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    odometer_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    odometer_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    ambulance: Mapped[AmbulanceVehicle] = relationship("AmbulanceVehicle", back_populates="dispatches")
    patient: Mapped[Patient | None] = relationship("Patient")
    emergency_encounter: Mapped[EmergencyEncounter | None] = relationship("EmergencyEncounter")
