"""
Target-native SQLAlchemy entities for Critical Care domain.

Tables:
- icu_patient_profiles
- icu_flowsheet_entries
- clinical_deterioration_alerts
- code_blue_incidents
- code_blue_events
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
from hms_migration.modules.inpatient.entities.admission import Admission
from hms_migration.modules.patients.entities.patient import Patient


class CodeBlueStatus(str, enum.Enum):
    activated = "activated"
    team_arrived = "team_arrived"
    in_progress = "in_progress"
    concluded = "concluded"


class CodeBlueOutcome(str, enum.Enum):
    rosc_achieved = "rosc_achieved"
    transferred_icu = "transferred_icu"
    deceased = "deceased"
    false_alarm = "false_alarm"


class DeteriorationRiskLevel(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    emergency = "emergency"


class AlertStatus(str, enum.Enum):
    active = "active"
    acknowledged = "acknowledged"
    escalated = "escalated"
    resolved = "resolved"


class IcuPatientProfile(Base):
    """Critical care parameters for occupied ICU beds (ventilator, invasive lines, GCS)."""

    __tablename__ = "icu_patient_profiles"
    __table_args__ = (
        UniqueConstraint("hospital_id", "admission_id", name="uq_icu_hospital_admission"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ventilator_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    peep: Mapped[float | None] = mapped_column(Float, nullable=True)
    fio2_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    invasive_line_days: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    inotrope_support: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    inotrope_details: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gcs_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sofa_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    care_indicators: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    admission: Mapped[Admission] = relationship("Admission")
    patient: Mapped[Patient] = relationship("Patient")


class IcuFlowsheetEntry(Base):
    """Hourly multi-system intensive care observation (vitals, ventilator, ABG, fluid balance)."""

    __tablename__ = "icu_flowsheet_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    recorded_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    recorded_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    heart_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    systolic_bp: Mapped[float | None] = mapped_column(Float, nullable=True)
    diastolic_bp: Mapped[float | None] = mapped_column(Float, nullable=True)
    mean_arterial_pressure: Mapped[float | None] = mapped_column(Float, nullable=True)
    spo2: Mapped[float | None] = mapped_column(Float, nullable=True)
    respiratory_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    abg_ph: Mapped[float | None] = mapped_column(Float, nullable=True)
    abg_pco2: Mapped[float | None] = mapped_column(Float, nullable=True)
    abg_po2: Mapped[float | None] = mapped_column(Float, nullable=True)
    abg_hco3: Mapped[float | None] = mapped_column(Float, nullable=True)
    abg_lactate: Mapped[float | None] = mapped_column(Float, nullable=True)
    hourly_iv_intake_ml: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hourly_enteral_intake_ml: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hourly_urine_output_ml: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hourly_drain_output_ml: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hourly_balance_ml: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClinicalDeteriorationAlert(Base):
    """Automated clinical early-warning scoring (NEWS2/MEWS) alert record."""

    __tablename__ = "clinical_deterioration_alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    calculator_type: Mapped[str] = mapped_column(String(32), nullable=False, default="NEWS2")
    total_score: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    risk_level: Mapped[DeteriorationRiskLevel] = mapped_column(
        Enum(DeteriorationRiskLevel, name="deterioration_risk_level"), nullable=False, index=True
    )
    parameter_breakdown: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )
    alert_status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus, name="alert_status"), nullable=False, default=AlertStatus.active, index=True
    )
    acknowledged_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    acknowledged_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship("Patient")


class CodeBlueIncident(Base):
    """Code Blue resuscitation event lifecycle."""

    __tablename__ = "code_blue_incidents"
    __table_args__ = (
        UniqueConstraint("hospital_id", "incident_code", name="uq_code_blue_hospital_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    location_description: Mapped[str] = mapped_column(String(255), nullable=False)
    ward_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    bed_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[CodeBlueStatus] = mapped_column(
        Enum(CodeBlueStatus, name="code_blue_status"),
        nullable=False,
        default=CodeBlueStatus.activated,
        index=True,
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    activated_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    team_arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    concluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[CodeBlueOutcome | None] = mapped_column(
        Enum(CodeBlueOutcome, name="code_blue_outcome"), nullable=True
    )
    team_members: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    summary_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    patient: Mapped[Patient | None] = relationship("Patient")
    events: Mapped[list[CodeBlueEvent]] = relationship(
        "CodeBlueEvent", back_populates="incident", cascade="all, delete-orphan"
    )


class CodeBlueEvent(Base):
    """Timeline interventions during resuscitation (CPR cycle, shock, drugs, rhythm)."""

    __tablename__ = "code_blue_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("code_blue_incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    incident: Mapped[CodeBlueIncident] = relationship("CodeBlueIncident", back_populates="events")
