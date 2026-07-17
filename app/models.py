import enum
import uuid
from datetime import date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, Time, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PlanType(str, enum.Enum):
    basic = "basic"
    premium = "premium"
    platinum = "platinum"


class WardType(str, enum.Enum):
    icu = "icu"
    general = "general"
    private = "private"
    emergency = "emergency"


class Hospital(Base):
    __tablename__ = "hospitals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[PlanType] = mapped_column(Enum(PlanType, name="plan_type"), nullable=False, default=PlanType.basic)
    icon_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Wing(Base):
    __tablename__ = "wings"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_wing_hospital_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    departments: Mapped[list["Department"]] = relationship(back_populates="wing")


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_dept_hospital_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    wing: Mapped["Wing | None"] = relationship(back_populates="departments")


class ShiftType(Base):
    __tablename__ = "shift_types"
    __table_args__ = (UniqueConstraint("hospital_id", "department_id", "name", name="uq_shift_hospital_dept_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    department: Mapped["Department"] = relationship()


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("hospital_id", "holiday_date", "name", name="uq_holiday_hospital_date_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AppointmentType(Base):
    __tablename__ = "appointment_types"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_appt_type_hospital_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slot_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Ward(Base):
    __tablename__ = "wards"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_ward_hospital_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ward_type: Mapped[WardType] = mapped_column(Enum(WardType, name="ward_type"), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    rooms: Mapped[list["Room"]] = relationship(back_populates="ward", cascade="all, delete-orphan")


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (UniqueConstraint("hospital_id", "room_code", name="uq_room_hospital_code"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ward_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ward: Mapped["Ward"] = relationship(back_populates="rooms")


class OtRoom(Base):
    """Operation theatre rooms configured under Masters → Organization → OT."""

    __tablename__ = "ot_rooms"
    __table_args__ = (UniqueConstraint("hospital_id", "code", name="uq_ot_room_hospital_code"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    wing: Mapped["Wing | None"] = relationship()
    department: Mapped["Department"] = relationship()


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_supplier_hospital_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CustomFieldType(str, enum.Enum):
    text = "text"
    number = "number"
    date = "date"
    select = "select"


class StaffRole(Base):
    __tablename__ = "staff_roles"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_staff_role_hospital_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    fields: Mapped[list["RoleCustomField"]] = relationship(
        back_populates="role", cascade="all, delete-orphan", order_by="RoleCustomField.sort_order"
    )
    permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    users: Mapped[list["HospitalUser"]] = relationship(back_populates="role")


class RoleCustomField(Base):
    __tablename__ = "role_custom_fields"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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
    options: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated for select
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    role: Mapped["StaffRole"] = relationship(back_populates="fields")


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "module_key", name="uq_role_module_perm"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff_roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    module_key: Mapped[str] = mapped_column(String(64), nullable=False)
    can_view: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    role: Mapped["StaffRole"] = relationship(back_populates="permissions")


class HospitalUser(Base):
    __tablename__ = "hospital_users"
    __table_args__ = (UniqueConstraint("hospital_id", "email", name="uq_hospital_user_email"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff_roles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shift_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    custom_values: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    role: Mapped["StaffRole"] = relationship(back_populates="users")
    shift: Mapped["ShiftType | None"] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # create | update | delete | login
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class AppointmentStatus(str, enum.Enum):
    scheduled = "scheduled"
    waiting = "waiting"  # checked in / in queue
    completed = "completed"
    cancelled = "cancelled"
    no_show = "no_show"


class PatientStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    admitted = "admitted"


class AdmissionStatus(str, enum.Enum):
    admitted = "admitted"
    discharged = "discharged"


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("hospital_id", "mobile", name="uq_patient_hospital_mobile"),
        UniqueConstraint("hospital_id", "uhid", name="uq_patient_hospital_uhid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    uhid: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # display name = first + last
    mobile: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    emergency_contact: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[PatientStatus] = mapped_column(
        Enum(PatientStatus, name="patient_status"),
        nullable=False,
        default=PatientStatus.active,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    appointments: Mapped[list["Appointment"]] = relationship(back_populates="patient", cascade="all, delete-orphan")
    prescriptions: Mapped[list["Prescription"]] = relationship(back_populates="patient", cascade="all, delete-orphan")
    medical_records: Mapped[list["MedicalRecord"]] = relationship(back_populates="patient", cascade="all, delete-orphan")
    admissions: Mapped[list["Admission"]] = relationship(back_populates="patient", cascade="all, delete-orphan")
    lab_orders: Mapped[list["LabOrder"]] = relationship(back_populates="patient", cascade="all, delete-orphan")
    radiology_orders: Mapped[list["RadiologyOrder"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )
    ot_surgeries: Mapped[list["OtSurgery"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )
    dms_documents: Mapped[list["PatientDocument"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
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
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name="appointment_status"),
        nullable=False,
        default=AppointmentStatus.scheduled,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    patient: Mapped["Patient"] = relationship(back_populates="appointments")
    doctor: Mapped["HospitalUser"] = relationship()


class Bed(Base):
    __tablename__ = "beds"
    __table_args__ = (UniqueConstraint("hospital_id", "room_id", "bed_code", name="uq_bed_hospital_room_code"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ward_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bed_code: Mapped[str] = mapped_column(String(64), nullable=False)
    is_occupied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ward: Mapped["Ward"] = relationship()
    room: Mapped["Room"] = relationship()


class Admission(Base):
    __tablename__ = "admissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
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
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    discharged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    patient: Mapped["Patient"] = relationship(back_populates="admissions")
    ward: Mapped["Ward"] = relationship()
    room: Mapped["Room"] = relationship()
    bed: Mapped["Bed"] = relationship()
    doctor: Mapped["HospitalUser | None"] = relationship()


class Prescription(Base):
    __tablename__ = "prescriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symptoms: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    medicines: Mapped[str] = mapped_column(Text, nullable=False)
    dosage: Mapped[str] = mapped_column(Text, nullable=False)
    advice: Mapped[str | None] = mapped_column(Text, nullable=True)
    follow_up_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    patient: Mapped["Patient"] = relationship(back_populates="prescriptions")
    doctor: Mapped["HospitalUser"] = relationship()


class MedicalRecord(Base):
    __tablename__ = "medical_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)  # Blood Report, X-Ray, MRI, ECG, Other
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_data: Mapped[str | None] = mapped_column(Text, nullable=True)  # base64 data URL for small uploads
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    patient: Mapped["Patient"] = relationship(back_populates="medical_records")
    doctor: Mapped["HospitalUser"] = relationship()


class LabSampleType(str, enum.Enum):
    blood = "blood"
    urine = "urine"
    stool = "stool"
    swab = "swab"
    sputum = "sputum"
    other = "other"


class LabOrderStatus(str, enum.Enum):
    ordered = "ordered"
    sample_collected = "sample_collected"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class LabItemStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"


class LabTestCatalog(Base):
    __tablename__ = "lab_test_catalog"
    __table_args__ = (UniqueConstraint("hospital_id", "test_code", name="uq_lab_test_code"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    test_code: Mapped[str] = mapped_column(String(32), nullable=False)
    test_name: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(128), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sample_type: Mapped[LabSampleType] = mapped_column(
        Enum(LabSampleType, name="lab_sample_type"), nullable=False, default=LabSampleType.blood
    )
    tat_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LabOrder(Base):
    __tablename__ = "lab_orders"
    __table_args__ = (UniqueConstraint("hospital_id", "order_no", name="uq_lab_order_no"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ordered_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    ordered_by_role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[LabOrderStatus] = mapped_column(
        Enum(LabOrderStatus, name="lab_order_status"),
        nullable=False,
        default=LabOrderStatus.ordered,
    )
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_type: Mapped[LabSampleType | None] = mapped_column(
        Enum(LabSampleType, name="lab_sample_type", create_type=False), nullable=True
    )
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    collection_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    patient: Mapped["Patient"] = relationship(back_populates="lab_orders")
    doctor: Mapped["HospitalUser | None"] = relationship()
    items: Mapped[list["LabOrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="LabOrderItem.created_at"
    )
    results: Mapped[list["LabResult"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="LabResult.sort_order"
    )


class LabOrderItem(Base):
    __tablename__ = "lab_order_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    test_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_test_catalog.id", ondelete="SET NULL"), nullable=True
    )
    test_code: Mapped[str] = mapped_column(String(32), nullable=False)
    test_name: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[LabItemStatus] = mapped_column(
        Enum(LabItemStatus, name="lab_item_status"),
        nullable=False,
        default=LabItemStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    order: Mapped["LabOrder"] = relationship(back_populates="items")


class LabResult(Base):
    __tablename__ = "lab_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lab_order_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parameter_name: Mapped[str] = mapped_column(String(255), nullable=False)
    result_value: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_range: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    order: Mapped["LabOrder"] = relationship(back_populates="results")


class RadiologyOrderStatus(str, enum.Enum):
    ordered = "ordered"
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class RadiologyScanCatalog(Base):
    __tablename__ = "radiology_scan_catalog"
    __table_args__ = (UniqueConstraint("hospital_id", "scan_code", name="uq_rad_scan_code"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scan_code: Mapped[str] = mapped_column(String(32), nullable=False)
    scan_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="General")
    department: Mapped[str] = mapped_column(String(128), nullable=False, default="Radiology")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RadiologyOrder(Base):
    __tablename__ = "radiology_orders"
    __table_args__ = (UniqueConstraint("hospital_id", "order_no", name="uq_rad_order_no"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radiology_scan_catalog.id", ondelete="SET NULL"), nullable=True
    )
    scan_code: Mapped[str] = mapped_column(String(32), nullable=False)
    scan_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ordered_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    ordered_by_role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[RadiologyOrderStatus] = mapped_column(
        Enum(RadiologyOrderStatus, name="radiology_order_status"),
        nullable=False,
        default=RadiologyOrderStatus.ordered,
    )
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    machine: Mapped[str | None] = mapped_column(String(128), nullable=True)
    technician_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    findings: Mapped[str | None] = mapped_column(Text, nullable=True)
    impression: Mapped[str | None] = mapped_column(Text, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    report_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_uploaded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    patient: Mapped["Patient"] = relationship(back_populates="radiology_orders")
    doctor: Mapped["HospitalUser | None"] = relationship()


class OtPriority(str, enum.Enum):
    emergency = "emergency"
    urgent = "urgent"
    elective = "elective"


class OtSurgeryStatus(str, enum.Enum):
    scheduled = "scheduled"
    confirmed = "confirmed"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class OtSurgery(Base):
    __tablename__ = "ot_surgeries"
    __table_args__ = (UniqueConstraint("hospital_id", "surgery_no", name="uq_ot_surgery_no"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    surgery_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    surgeon_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospital_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assistant_surgeon: Mapped[str | None] = mapped_column(String(255), nullable=True)
    surgery_type: Mapped[str] = mapped_column(String(255), nullable=False)
    surgery_category: Mapped[str] = mapped_column(String(128), nullable=False, default="General")
    priority: Mapped[OtPriority] = mapped_column(
        Enum(OtPriority, name="ot_priority"),
        nullable=False,
        default=OtPriority.elective,
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ot_room_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ot_rooms.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ot_room: Mapped[str] = mapped_column(String(64), nullable=False, default="OT-1")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    anaesthetist: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    booked_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    booked_by_role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[OtSurgeryStatus] = mapped_column(
        Enum(OtSurgeryStatus, name="ot_surgery_status"),
        nullable=False,
        default=OtSurgeryStatus.scheduled,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shifted_to: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Operation notes
    pre_op_diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    procedure_performed: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings: Mapped[str | None] = mapped_column(Text, nullable=True)
    implants_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    complications: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_op_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    follow_up_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes_recorded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    ot_report_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ot_report_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consent_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_file_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    patient: Mapped["Patient"] = relationship(back_populates="ot_surgeries")
    surgeon: Mapped["HospitalUser | None"] = relationship()
    department: Mapped["Department | None"] = relationship()
    ot_room_ref: Mapped["OtRoom | None"] = relationship()


class PatientDocumentCategory(str, enum.Enum):
    aadhaar = "aadhaar"
    insurance = "insurance"
    consent = "consent"
    referral = "referral"
    discharge_summary = "discharge_summary"
    other = "other"


class PatientDocument(Base):
    """Hospital-wide patient document store for DMS (ID proofs, consents, etc.)."""

    __tablename__ = "patient_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    patient: Mapped["Patient"] = relationship(back_populates="dms_documents")


class EquipmentStatus(str, enum.Enum):
    available = "available"
    in_use = "in_use"
    under_maintenance = "under_maintenance"
    out_of_service = "out_of_service"


class EquipmentAssignTarget(str, enum.Enum):
    department = "department"
    room = "room"
    doctor = "doctor"
    nurse = "nurse"
    patient = "patient"


class MaintenanceStatus(str, enum.Enum):
    scheduled = "scheduled"
    due = "due"
    ok = "ok"
    completed = "completed"
    overdue = "overdue"


class EquipmentRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    assigned = "assigned"


class EquipmentCategory(Base):
    __tablename__ = "equipment_categories"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="uq_equip_cat_hospital_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    equipment: Mapped[list["EquipmentItem"]] = relationship(back_populates="category")


class EquipmentItem(Base):
    __tablename__ = "equipment_items"
    __table_args__ = (UniqueConstraint("hospital_id", "asset_id", name="uq_equip_asset_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    manufacturer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchase_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    department: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[EquipmentStatus] = mapped_column(
        Enum(EquipmentStatus, name="equipment_status"),
        nullable=False,
        default=EquipmentStatus.available,
    )
    # AMC / Warranty
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    warranty_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    warranty_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    amc_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    amc_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    vendor_contact: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    category: Mapped["EquipmentCategory | None"] = relationship(back_populates="equipment")
    assignments: Mapped[list["EquipmentAssignment"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )
    maintenances: Mapped[list["EquipmentMaintenance"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )
    service_logs: Mapped[list["EquipmentServiceLog"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan"
    )


class EquipmentAssignment(Base):
    __tablename__ = "equipment_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[EquipmentAssignTarget] = mapped_column(
        Enum(EquipmentAssignTarget, name="equipment_assign_target"),
        nullable=False,
        default=EquipmentAssignTarget.department,
    )
    target_name: Mapped[str] = mapped_column(String(255), nullable=False)
    assigned_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    equipment: Mapped["EquipmentItem"] = relationship(back_populates="assignments")


class EquipmentMaintenance(Base):
    __tablename__ = "equipment_maintenances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    last_service_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_service_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[MaintenanceStatus] = mapped_column(
        Enum(MaintenanceStatus, name="maintenance_status"),
        nullable=False,
        default=MaintenanceStatus.scheduled,
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    equipment: Mapped["EquipmentItem"] = relationship(back_populates="maintenances")


class EquipmentServiceLog(Base):
    __tablename__ = "equipment_service_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    work_done: Mapped[str] = mapped_column(Text, nullable=False)
    engineer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    equipment: Mapped["EquipmentItem"] = relationship(back_populates="service_logs")


class EquipmentRequest(Base):
    __tablename__ = "equipment_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    department: Mapped[str] = mapped_column(String(128), nullable=False)
    equipment_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[EquipmentRequestStatus] = mapped_column(
        Enum(EquipmentRequestStatus, name="equipment_request_status"),
        nullable=False,
        default=EquipmentRequestStatus.pending,
    )
    requested_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_equipment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
