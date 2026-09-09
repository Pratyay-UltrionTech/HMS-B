"""
Target architecture entities for Clinical Records, Prescriptions, and Patient Documents.

Conforms to UltrionTech-Backend-Template modules/clinical_records/entities/ specification.
Maps directly to prescriptions, medical_records, and patient_documents tables on target Base metadata.
"""

from __future__ import annotations

from datetime import date, datetime
import enum
import uuid

from sqlalchemy import (
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

from typing import TYPE_CHECKING

from hms_migration.infrastructure.postgres.base import Base

if TYPE_CHECKING:
    from hms_migration.modules.doctors.entities.doctor import HospitalUser
    from hms_migration.modules.patients.entities.patient import Patient


class PatientDocumentCategory(str, enum.Enum):
    """Document classification category."""

    aadhaar = "aadhaar"
    insurance = "insurance"
    consent = "consent"
    referral = "referral"
    discharge_summary = "discharge_summary"
    other = "other"


class Prescription(Base):
    """Doctor outpatient/inpatient prescription record."""

    __tablename__ = "prescriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    symptoms: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    medicines: Mapped[str] = mapped_column(Text, nullable=False)
    dosage: Mapped[str] = mapped_column(Text, nullable=False)
    advice: Mapped[str | None] = mapped_column(Text, nullable=True)
    follow_up_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    signature_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped[Patient | None] = relationship("Patient", foreign_keys=[patient_id])
    doctor: Mapped[HospitalUser | None] = relationship("HospitalUser", foreign_keys=[doctor_id])


class MedicalRecord(Base):
    """Clinical record, investigation, or lab/radiology report attachment."""

    __tablename__ = "medical_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    lab_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    radiology_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped[Patient | None] = relationship("Patient", foreign_keys=[patient_id])
    doctor: Mapped[HospitalUser | None] = relationship("HospitalUser", foreign_keys=[doctor_id])


class PatientDocument(Base):
    """Hospital patient document repository record (DMS)."""

    __tablename__ = "patient_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[PatientDocumentCategory] = mapped_column(
        Enum(PatientDocumentCategory, name="patient_document_category"),
        nullable=False,
        default=PatientDocumentCategory.other,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    uploaded_by_role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped[Patient | None] = relationship("Patient", foreign_keys=[patient_id])
