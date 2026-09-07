"""
Target architecture entities for Inpatient Admissions and IPD Form Submissions.

Conforms to UltrionTech-Backend-Template modules/inpatient/entities/ specification.
Maps directly to admissions and ipd_form_submissions tables on target Base metadata.
"""

from __future__ import annotations

from datetime import datetime
import enum
import uuid

from sqlalchemy import (
    DateTime,
    Enum,
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
from hms_migration.modules.beds.entities.bed import Bed, Room, Ward
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.patients.entities.patient import Patient


class AdmissionStatus(str, enum.Enum):
    """Clinical status of an inpatient admission."""

    admitted = "admitted"
    discharge_requested = "discharge_requested"
    discharged = "discharged"


class Admission(Base):
    """Inpatient admission record."""

    __tablename__ = "admissions"
    __table_args__ = (
        UniqueConstraint("hospital_id", "ip_id", name="uq_admission_hospital_ip_id"),
        UniqueConstraint("hospital_id", "er_id", name="uq_admission_hospital_er_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ward_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bed_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("beds.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[AdmissionStatus] = mapped_column(
        Enum(AdmissionStatus, name="admission_status"),
        nullable=False,
        default=AdmissionStatus.admitted,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    discharge_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    admitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    discharged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ip_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    er_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    patient: Mapped[Patient | None] = relationship("Patient", foreign_keys=[patient_id])
    ward: Mapped[Ward | None] = relationship("Ward", foreign_keys=[ward_id])
    room: Mapped[Room | None] = relationship("Room", foreign_keys=[room_id])
    bed: Mapped[Bed | None] = relationship("Bed", foreign_keys=[bed_id])
    doctor: Mapped[HospitalUser | None] = relationship("HospitalUser", foreign_keys=[doctor_id])


class IpdFormSubmissionStatus(str, enum.Enum):
    """Lifecycle status of an IPD clinical form."""

    draft = "draft"
    final = "final"


class IpdFormSubmission(Base):
    """Filled IPD clinical / consent forms attached to a patient admission record."""

    __tablename__ = "ipd_form_submissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    form_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    form_title: Mapped[str] = mapped_column(String(255), nullable=False)
    form_data: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )
    html_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[IpdFormSubmissionStatus] = mapped_column(
        Enum(IpdFormSubmissionStatus, name="ipd_form_submission_status"),
        nullable=False,
        default=IpdFormSubmissionStatus.draft,
    )
    filled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    filled_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    filled_by_role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    medical_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    patient_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    patient: Mapped[Patient | None] = relationship("Patient", foreign_keys=[patient_id])
    admission: Mapped[Admission | None] = relationship("Admission", foreign_keys=[admission_id])
    filled_by: Mapped[HospitalUser | None] = relationship("HospitalUser", foreign_keys=[filled_by_id])
