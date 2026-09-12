"""
Target-native SQLAlchemy entities for Emergency Department Management domain.

Tables:
- emergency_encounters
- emergency_triage_assessments
- emergency_treatment_orders
- emergency_dispositions
"""

from __future__ import annotations

from datetime import datetime
import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.patients.entities.patient import Patient


class EmergencyArrivalSource(str, enum.Enum):
    walk_in = "walk_in"
    ambulance = "ambulance"
    referral = "referral"
    police_trauma = "police_trauma"
    internal_transfer = "internal_transfer"


class EmergencyStatus(str, enum.Enum):
    registered = "registered"
    triaged = "triaged"
    in_treatment = "in_treatment"
    disposition_planned = "disposition_planned"
    completed = "completed"
    cancelled = "cancelled"


class EmergencyOrderType(str, enum.Enum):
    medication = "medication"
    iv_fluid = "iv_fluid"
    procedure = "procedure"
    lab_investigation = "lab_investigation"
    radiology_scan = "radiology_scan"
    nursing_care = "nursing_care"


class EmergencyOrderStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class EmergencyDispositionType(str, enum.Enum):
    admit_ipd = "admit_ipd"
    admit_icu = "admit_icu"
    observation_ward = "observation_ward"
    discharge_home = "discharge_home"
    transfer_tertiary = "transfer_tertiary"
    lama = "lama"
    deceased = "deceased"


class EmergencyEncounter(Base):
    """Emergency encounter managing arrival, complaints, urgency, and clinical state."""

    __tablename__ = "emergency_encounters"
    __table_args__ = (
        UniqueConstraint("hospital_id", "er_id", name="uq_emergency_hospital_er_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    er_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    arrival_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    arrival_source: Mapped[EmergencyArrivalSource] = mapped_column(
        Enum(EmergencyArrivalSource, name="emergency_arrival_source"),
        nullable=False,
        default=EmergencyArrivalSource.walk_in,
    )
    presenting_complaints: Mapped[str] = mapped_column(Text, nullable=False)
    triage_level: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    attending_doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[EmergencyStatus] = mapped_column(
        Enum(EmergencyStatus, name="emergency_status"),
        nullable=False,
        default=EmergencyStatus.registered,
        index=True,
    )
    is_escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient | None] = relationship("Patient", foreign_keys=[patient_id])
    attending_doctor: Mapped[HospitalUser | None] = relationship("HospitalUser", foreign_keys=[attending_doctor_id])
    triage_assessment: Mapped[EmergencyTriageAssessment | None] = relationship(
        "EmergencyTriageAssessment", back_populates="encounter", uselist=False
    )
    orders: Mapped[list[EmergencyTreatmentOrder]] = relationship(
        "EmergencyTreatmentOrder", back_populates="encounter", cascade="all, delete-orphan"
    )
    disposition: Mapped[EmergencyDisposition | None] = relationship(
        "EmergencyDisposition", back_populates="encounter", uselist=False
    )


class EmergencyTriageAssessment(Base):
    """Clinical triage evaluation using ESI/MTS acuity, AVPU, vitals, and red flags."""

    __tablename__ = "emergency_triage_assessments"
    __table_args__ = (
        UniqueConstraint("hospital_id", "encounter_id", name="uq_triage_hospital_encounter"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emergency_encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    acuity_level: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    avpu: Mapped[str] = mapped_column(String(32), nullable=False, default="alert")
    pain_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    respiratory_distress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    systolic_bp: Mapped[float | None] = mapped_column(Float, nullable=True)
    diastolic_bp: Mapped[float | None] = mapped_column(Float, nullable=True)
    heart_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    respiratory_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    spo2: Mapped[float | None] = mapped_column(Float, nullable=True)
    red_flags: Mapped[dict | None] = mapped_column(JSONB().with_variant(JSON, "sqlite"), nullable=True)
    triage_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    triaged_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    triaged_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="Emergency Nurse")
    triaged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    encounter: Mapped[EmergencyEncounter] = relationship("EmergencyEncounter", back_populates="triage_assessment")


class EmergencyTreatmentOrder(Base):
    """Rapid emergency doctor orders (STAT medications, verbal orders, bedside procedures)."""

    __tablename__ = "emergency_treatment_orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emergency_encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_type: Mapped[EmergencyOrderType] = mapped_column(
        Enum(EmergencyOrderType, name="emergency_order_type"),
        nullable=False,
        default=EmergencyOrderType.medication,
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    dosage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    route: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_stat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verbal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ordered_by_doctor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ordered_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    executed_by_nurse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    executed_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    execution_status: Mapped[EmergencyOrderStatus] = mapped_column(
        Enum(EmergencyOrderStatus, name="emergency_order_status"),
        nullable=False,
        default=EmergencyOrderStatus.pending,
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    encounter: Mapped[EmergencyEncounter] = relationship("EmergencyEncounter", back_populates="orders")


class EmergencyDisposition(Base):
    """Final emergency patient outcome and disposition routing."""

    __tablename__ = "emergency_dispositions"
    __table_args__ = (
        UniqueConstraint("hospital_id", "encounter_id", name="uq_disposition_hospital_encounter"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emergency_encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    disposition_type: Mapped[EmergencyDispositionType] = mapped_column(
        Enum(EmergencyDispositionType, name="emergency_disposition_type"),
        nullable=False,
    )
    destination_ward_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    destination_bed_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    admission_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    transfer_facility: Mapped[str | None] = mapped_column(String(255), nullable=True)
    disposition_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    encounter: Mapped[EmergencyEncounter] = relationship("EmergencyEncounter", back_populates="disposition")
