"""
Appointment domain entity.

Conforms to UltrionTech-Backend-Template modules/appointments/entities/ specification.
Maps directly to the appointments table on the target architecture Base metadata,
completely eliminating runtime imports of app.models.Appointment.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, Time, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.modules.appointments.entities.enums import AppointmentStatus

if TYPE_CHECKING:
    from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
    from hms_migration.modules.doctors.entities.doctor import HospitalUser
    from hms_migration.modules.inpatient.entities.admission import Admission
    from hms_migration.modules.patients.entities.patient import Patient


class Appointment(Base):
    """Outpatient clinical encounter / appointment record."""

    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("hospital_id", "op_id", name="uq_appointment_hospital_op_id"),
    )

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
    appointment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    appointment_time: Mapped[time] = mapped_column(Time, nullable=False)
    purpose: Mapped[str] = mapped_column(String(255), nullable=False)
    visit_type: Mapped[str] = mapped_column(String(64), nullable=False, default="OPD")
    appointment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointment_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    wing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    consultation_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    followup_eligibility: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name="appointment_status"),
        nullable=False,
        default=AppointmentStatus.scheduled,
    )
    booking_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="future")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    op_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    admission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    nurse_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    doctor: Mapped[HospitalUser | None] = relationship("HospitalUser", foreign_keys=[doctor_id])
    patient: Mapped[Patient | None] = relationship("Patient", foreign_keys=[patient_id])
    nurse: Mapped[HospitalUser | None] = relationship("HospitalUser", foreign_keys=[nurse_id])
    appointment_type: Mapped[AppointmentType | None] = relationship("AppointmentType", foreign_keys=[appointment_type_id])
    admission: Mapped[Admission | None] = relationship(
        "Admission",
        foreign_keys="Appointment.admission_id",
        primaryjoin="Appointment.admission_id==Admission.id",
        viewonly=True,
    )
