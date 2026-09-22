"""
Target architecture entities for the Doctor and Staff domain.

Conforms to UltrionTech-Backend-Template modules/doctors/entities/ specification.
Maps directly to hospital_users, staff_roles, shift_types, doctor_leaves, and holidays
on the target architecture Base metadata, completely independent of app.models.
"""

from __future__ import annotations

from datetime import date, datetime, time
import uuid

import enum
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
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.postgres.base import Base


class CustomFieldType(str, enum.Enum):
    text = "text"
    select = "select"
    checkbox = "checkbox"
    date = "date"
    number = "number"


class StaffRole(Base):
    """Staff role entity."""

    __tablename__ = "staff_roles"
    __table_args__ = (
        UniqueConstraint("hospital_id", "name", name="uq_staff_role_hospital_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission", back_populates="role", cascade="all, delete-orphan"
    )
    custom_fields: Mapped[list["RoleCustomField"]] = relationship(
        "RoleCustomField", back_populates="role", cascade="all, delete-orphan"
    )


class RoleCustomField(Base):
    """Custom attribute field definition for a staff role."""

    __tablename__ = "role_custom_fields"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff_roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    field_type: Mapped[CustomFieldType] = mapped_column(
        Enum(CustomFieldType, name="custom_field_type"), nullable=False, default=CustomFieldType.text
    )
    options: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    role: Mapped["StaffRole"] = relationship("StaffRole", back_populates="custom_fields")


class RolePermission(Base):
    """Module-level permission record for a staff role."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "module_key", name="uq_role_module_perm"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff_roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    module_key: Mapped[str] = mapped_column(String(64), nullable=False)
    can_view: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Granular action permissions
    can_create: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_delete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_approve: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_validate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_release: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_dispense: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_refund: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_cancel: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_administer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    role: Mapped["StaffRole"] = relationship("StaffRole", back_populates="permissions")


class ShiftType(Base):
    """Staff work shift definition."""

    __tablename__ = "shift_types"
    __table_args__ = (
        UniqueConstraint("hospital_id", "department_id", "name", name="uq_shift_hospital_dept_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HospitalUser(Base):
    """Hospital staff user record, including doctor profiles."""

    __tablename__ = "hospital_users"
    __table_args__ = (
        UniqueConstraint("hospital_id", "email", name="uq_hospital_user_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff_roles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shift_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Doctor specific fields
    specialization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    medical_registration_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qualification: Mapped[str | None] = mapped_column(String(255), nullable=True)
    years_of_experience: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consultation_room: Mapped[str | None] = mapped_column(String(128), nullable=True)
    digital_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    show_financial_details: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    custom_values: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # FLAW-024: Session revocation version. Incremented whenever a user's role or
    # active status changes so previously-issued JWTs can be rejected server-side.
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    role: Mapped[StaffRole | None] = relationship("StaffRole", foreign_keys=[role_id])
    shift: Mapped[ShiftType | None] = relationship("ShiftType", foreign_keys=[shift_id])

    assigned_roles: Mapped[list["HospitalUserRole"]] = relationship(
        "HospitalUserRole",
        primaryjoin="HospitalUser.id == HospitalUserRole.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    assigned_departments: Mapped[list["HospitalUserDepartment"]] = relationship(
        "HospitalUserDepartment",
        primaryjoin="HospitalUser.id == HospitalUserDepartment.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    assigned_locations: Mapped[list["HospitalUserLocation"]] = relationship(
        "HospitalUserLocation",
        primaryjoin="HospitalUser.id == HospitalUserLocation.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    practitioner: Mapped["Practitioner | None"] = relationship(
        "Practitioner",
        primaryjoin="HospitalUser.id == Practitioner.user_id",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )


class HospitalUserRole(Base):
    """Many-to-many assignment mapping hospital users to one or more staff roles."""

    __tablename__ = "hospital_user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff_roles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    role: Mapped[StaffRole] = relationship("StaffRole")
    user: Mapped[HospitalUser] = relationship("HospitalUser", back_populates="assigned_roles", foreign_keys=[user_id])


class HospitalUserDepartment(Base):
    """User-to-Department organizational assignment supporting primary and secondary departments."""

    __tablename__ = "hospital_user_departments"
    __table_args__ = (
        UniqueConstraint("user_id", "department_id", name="uq_user_dept"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_supervise: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[HospitalUser] = relationship("HospitalUser", back_populates="assigned_departments", foreign_keys=[user_id])


class HospitalUserLocation(Base):
    """User-to-Ward/Location operational assignment for nursing and ward-level scoping."""

    __tablename__ = "hospital_user_locations"
    __table_args__ = (
        UniqueConstraint("user_id", "ward_id", name="uq_user_ward"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ward_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="CASCADE"), nullable=True, index=True
    )
    wing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wings.id", ondelete="CASCADE"), nullable=True, index=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[HospitalUser] = relationship("HospitalUser", back_populates="assigned_locations", foreign_keys=[user_id])


class Practitioner(Base):
    """Clinical practitioner professional registration and credential record."""

    __tablename__ = "practitioners"
    __table_args__ = (
        UniqueConstraint("hospital_id", "user_id", name="uq_practitioner_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    designation: Mapped[str] = mapped_column(String(128), nullable=False)
    medical_registration_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qualification: Mapped[str | None] = mapped_column(String(255), nullable=True)
    years_of_experience: Mapped[int | None] = mapped_column(Integer, nullable=True)
    digital_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[HospitalUser] = relationship("HospitalUser", back_populates="practitioner", foreign_keys=[user_id])


class DoctorLeave(Base):
    """Doctor leave schedule block."""

    __tablename__ = "doctor_leaves"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    leave_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    leave_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    doctor: Mapped[HospitalUser | None] = relationship("HospitalUser", foreign_keys=[doctor_id])


class Holiday(Base):
    """Hospital public / recognized holiday."""

    __tablename__ = "holidays"
    __table_args__ = (
        UniqueConstraint("hospital_id", "holiday_date", "name", name="uq_holiday_hospital_date_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StaffDailyShift(Base):
    """Per-day shift assignment override for a staff member (roster control)."""

    __tablename__ = "staff_daily_shifts"
    __table_args__ = (
        UniqueConstraint("hospital_id", "user_id", "roster_date", name="uq_staff_daily_shift"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    roster_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shift_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="on_duty")  # on_duty | off | leave
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["HospitalUser"] = relationship(foreign_keys=[user_id])
    shift: Mapped["ShiftType | None"] = relationship(foreign_keys=[shift_id])
