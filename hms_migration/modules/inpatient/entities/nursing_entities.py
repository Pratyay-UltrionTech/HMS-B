"""
Target-native SQLAlchemy entities for Nursing Management & Merged Clinical Notes.

Covers:
- Feature 12: Nursing Care Plan (nursing_care_plans)
- Feature 13: Nursing Notes (MERGED into IPD Clinical Notes -> ipd_clinical_notes)
- Feature 14: Nursing Handover (nursing_shift_handovers)
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
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.modules.inpatient.entities.admission import Admission
from hms_migration.modules.patients.entities.patient import Patient


class CarePlanStatus(str, enum.Enum):
    active = "active"
    resolved = "resolved"
    revised = "revised"


class ClinicalNoteType(str, enum.Enum):
    nursing_progress = "nursing_progress"
    doctor_progress = "doctor_progress"
    shift_summary = "shift_summary"
    nursing_observation = "nursing_observation"
    procedure_note = "procedure_note"


class ShiftType(str, enum.Enum):
    morning = "morning"
    evening = "evening"
    night = "night"


class HandoverShiftType(str, enum.Enum):
    morning_to_evening = "morning_to_evening"
    evening_to_night = "evening_to_night"
    night_to_morning = "night_to_morning"


class MedicationAdminStatus(str, enum.Enum):
    scheduled = "scheduled"
    administered = "administered"
    withheld = "withheld"
    missed = "missed"
    refused = "refused"


class NursingCarePlan(Base):
    """Structured nursing care planning (diagnosis, goals, interventions, reassessments)."""

    __tablename__ = "nursing_care_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    diagnosis_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nursing_diagnosis: Mapped[str] = mapped_column(String(255), nullable=False)
    goals: Mapped[str] = mapped_column(Text, nullable=False)
    interventions: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    evaluation_frequency: Mapped[str] = mapped_column(String(64), nullable=False, default="every_shift")
    status: Mapped[CarePlanStatus] = mapped_column(
        Enum(CarePlanStatus, name="care_plan_status"), nullable=False, default=CarePlanStatus.active, index=True
    )
    created_by_nurse_name: Mapped[str] = mapped_column(String(255), nullable=False)
    reassessment_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reassessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    admission: Mapped[Admission] = relationship("Admission")
    patient: Mapped[Patient] = relationship("Patient")


class IpdClinicalNote(Base):
    """
    Feature 13: Merged IPD Clinical Notes (Doctor and Nursing Documentation).
    Unifies physician notes, nursing progress notes, and shift observations
    in one coherent clinical record without data fragmentation.
    """

    __tablename__ = "ipd_clinical_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    author_name: Mapped[str] = mapped_column(String(255), nullable=False)
    author_role: Mapped[str] = mapped_column(String(64), nullable=False, default="nurse")
    note_type: Mapped[ClinicalNoteType] = mapped_column(
        Enum(ClinicalNoteType, name="clinical_note_type"),
        nullable=False,
        default=ClinicalNoteType.nursing_progress,
        index=True,
    )
    shift_type: Mapped[ShiftType | None] = mapped_column(
        Enum(ShiftType, name="shift_type_enum"), nullable=True
    )
    subjective: Mapped[str | None] = mapped_column(Text, nullable=True)
    objective_vitals: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    assessment: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    note_content: Mapped[str] = mapped_column(Text, nullable=False)
    signature_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    admission: Mapped[Admission] = relationship("Admission")
    patient: Mapped[Patient] = relationship("Patient")


class NursingShiftHandover(Base):
    """Structured clinical shift handover (SBAR format) transferring patient acuity and tasks."""

    __tablename__ = "nursing_shift_handovers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shift_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    shift_type: Mapped[HandoverShiftType] = mapped_column(
        Enum(HandoverShiftType, name="handover_shift_type"), nullable=False
    )
    outgoing_nurse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    outgoing_nurse_name: Mapped[str] = mapped_column(String(255), nullable=False)
    incoming_nurse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    incoming_nurse_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    situation: Mapped[str] = mapped_column(Text, nullable=False)
    background: Mapped[str] = mapped_column(Text, nullable=False)
    assessment_acuity: Mapped[str] = mapped_column(String(128), nullable=False)
    pending_stat_orders: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    critical_lab_alerts: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    high_risk_precautions: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    pending_tasks: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    is_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    admission: Mapped[Admission] = relationship("Admission")
    patient: Mapped[Patient] = relationship("Patient")


class MedicationAdministrationRecord(Base):
    """
    Feature 15: Electronic Medication Administration Record (eMAR).
    MERGED into IPD clinical flow. Tracks scheduled medication administrations,
    actual admin timestamps, nurse identity, pre-admin vitals, withholding reasons,
    and witness nurse dual sign-off for high-alert medications (Feature 18).
    """

    __tablename__ = "medication_administration_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    medicine_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    medicine_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dose: Mapped[str] = mapped_column(String(64), nullable=False)
    route: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduled_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[MedicationAdminStatus] = mapped_column(
        Enum(MedicationAdminStatus, name="medication_admin_status"),
        nullable=False,
        default=MedicationAdminStatus.scheduled,
        index=True,
    )
    administered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    administering_nurse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    administering_nurse_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_high_alert: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    witness_nurse_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    witness_nurse_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vitals_before_admin: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True, default=dict
    )
    notes_or_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    admission: Mapped[Admission] = relationship("Admission")
    patient: Mapped[Patient] = relationship("Patient")
