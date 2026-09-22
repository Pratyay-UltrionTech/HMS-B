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
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.postgres.base import Base
from modules.beds.entities.bed import Bed, Room, Ward
from modules.doctors.entities.doctor import HospitalUser
from modules.patients.entities.patient import Patient


class AdmissionStatus(str, enum.Enum):
    """Clinical status of an inpatient admission.

    Canonical lifecycle: requested → admitted → discharge_requested → discharged.
    ``requested`` is the authoritative admission-intent state (admission episode
    decided but not yet operationally accepted). ``admitted`` with ``bed_id``
    NULL means "Admitted — Awaiting Bed".
    """

    requested = "requested"
    admitted = "admitted"
    discharge_requested = "discharge_requested"
    discharged = "discharged"


# Statuses in which the patient counts as having an open inpatient episode
# (exactly one such row per patient enforced by uq_admissions_open_patient).
OPEN_ADMISSION_STATUSES = ("requested", "admitted", "discharge_requested")

# Statuses in which the patient counts as an active inpatient for census,
# bed occupancy, billing, and cross-role "is inpatient?" derivation.
ACTIVE_INPATIENT_STATUSES = ("admitted", "discharge_requested")


class Admission(Base):
    """Inpatient admission record."""

    __tablename__ = "admissions"
    __table_args__ = (
        UniqueConstraint("hospital_id", "ip_id", name="uq_admission_hospital_ip_id"),
        UniqueConstraint("hospital_id", "er_id", name="uq_admission_hospital_er_id"),
        # One open episode (requested/admitted/discharge_requested) per bed.
        # Invariant 6: a bed cannot belong to two open admissions.
        Index(
            "uq_admissions_active_bed",
            "hospital_id",
            "bed_id",
            unique=True,
            postgresql_where=text(
                "status IN ('requested', 'admitted', 'discharge_requested')"
            ),
            sqlite_where=text(
                "status IN ('requested', 'admitted', 'discharge_requested')"
            ),
        ),
        # Invariant 1: a patient cannot have multiple open/requested admissions.
        Index(
            "uq_admissions_active_patient",
            "hospital_id",
            "patient_id",
            unique=True,
            postgresql_where=text(
                "status IN ('requested', 'admitted', 'discharge_requested')"
            ),
            sqlite_where=text(
                "status IN ('requested', 'admitted', 'discharge_requested')"
            ),
        ),
        # At most one canonical admission per source appointment (Invariant 11).
        Index(
            "uq_admission_source_appointment",
            "hospital_id",
            "source_appointment_id",
            unique=True,
            postgresql_where=text("source_appointment_id IS NOT NULL"),
            sqlite_where=text("source_appointment_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Location is nullable: requested admissions and "Admitted — Awaiting Bed"
    # (status=admitted, bed_id NULL) legitimately have no physical bed yet.
    # Invariant 4/5: bedless admissions remain visible in census/queues.
    ward_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    room_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    bed_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("beds.id", ondelete="RESTRICT"), nullable=True, index=True
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
        UUID(as_uuid=True),
        # use_alter breaks the admissions↔appointments dependency cycle for
        # DDL sorting (appointments.admission_id points back at admissions).
        ForeignKey("appointments.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        index=True,
    )
    # Immutable demographic snapshot captured at admission time (FLAW-010).
    # These freeze identity for wristbands / worklists / discharge summaries so
    # a later demographic correction to the Patient row does not retroactively
    # rewrite an active or discharged encounter. They are refreshed only for
    # ACTIVE (non-discharged) admissions via the audit-logged amend_demographics
    # flow; discharged admissions keep the values captured at discharge time.
    patient_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(32), nullable=True)
    age_at_admission: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
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


class BedStaySegment(Base):
    """Tracks duration and billing rate for individual bed occupancy segments during an admission."""

    __tablename__ = "bed_stay_segments"
    __table_args__ = (
        # Invariant 7: exactly one open segment per active bed-assigned admission.
        Index(
            "uq_open_bed_stay_segment_per_admission",
            "hospital_id",
            "admission_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ward_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="SET NULL"), nullable=True
    )
    room_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True
    )
    bed_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("beds.id", ondelete="SET NULL"), nullable=True
    )
    rate_per_day: Mapped[float] = mapped_column(nullable=False, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    admission: Mapped[Admission] = relationship("Admission", foreign_keys=[admission_id])
    ward: Mapped[Ward | None] = relationship("Ward", foreign_keys=[ward_id])
    room: Mapped[Room | None] = relationship("Room", foreign_keys=[room_id])
    bed: Mapped[Bed | None] = relationship("Bed", foreign_keys=[bed_id])
