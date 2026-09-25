"""IPD Doctor Medication Order entity model."""

from __future__ import annotations

from datetime import datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.postgres.base import Base
from modules.doctors.entities.doctor import HospitalUser
from modules.patients.entities.patient import Patient


class MedicationOrderStatus(str, enum.Enum):
    """Lifecycle status of an IPD doctor medication order."""

    active = "active"
    modified = "modified"
    discontinued = "discontinued"
    completed = "completed"
    cancelled = "cancelled"


class IpdMedicationOrder(Base):
    """
    Inpatient Doctor Medication Order.
    Authoritative prescription of inpatient medication regimen, structured to feed eMAR.
    """

    __tablename__ = "ipd_medication_orders"
    __table_args__ = (
        Index("ix_med_order_hospital_id", "hospital_id"),
        Index("ix_med_order_admission_status", "hospital_id", "admission_id", "status"),
        Index("ix_med_order_patient", "hospital_id", "patient_id"),
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
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    doctor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    medicine_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    medicine_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dose: Mapped[str] = mapped_column(String(64), nullable=False)
    dosage_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="mg")
    route: Mapped[str] = mapped_column(String(64), nullable=False, default="oral")
    frequency: Mapped[str] = mapped_column(String(64), nullable=False, default="od")
    schedule_timing: Mapped[str | None] = mapped_column(String(128), nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_prn: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prn_indication: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[MedicationOrderStatus] = mapped_column(
        Enum(MedicationOrderStatus, name="medication_order_status"),
        nullable=False,
        default=MedicationOrderStatus.active,
        index=True,
    )
    discontinued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discontinued_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    discontinued_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discontinued_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    doctor: Mapped[HospitalUser] = relationship("HospitalUser", foreign_keys=[doctor_id])
    patient: Mapped[Patient] = relationship("Patient", foreign_keys=[patient_id])
    admission: Mapped["Admission"] = relationship("Admission", foreign_keys=[admission_id])
    administrations: Mapped[list["MedicationAdministrationRecord"]] = relationship(
        "MedicationAdministrationRecord", foreign_keys="MedicationAdministrationRecord.order_id", back_populates="order"
    )
