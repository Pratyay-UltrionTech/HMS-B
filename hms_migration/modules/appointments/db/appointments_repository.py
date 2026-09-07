"""
Database repository for the Appointments module.

Conforms to UltrionTech-Backend-Template modules/appointments/db/ specification.
Manages queries, status persistence, queue tokens, and encounter numbering for appointments.
"""

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Date, Integer, String, column, func, or_, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.base_repository import BaseRepository
from hms_migration.modules.appointments.contracts.appointments_contracts import (
    AppointmentListItem,
)
from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.patients.entities.patient import Patient

# SQLAlchemy Core table references for joined lookups
_hospital_users = table(
    "hospital_users",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("role_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("phone", String),
    column("email", String),
    column("specialization", String),
    column("qualification", String),
    column("medical_registration_number", String),
    column("years_of_experience", Integer),
    column("consultation_room", String),
    column("is_active", Boolean),
)

_staff_roles = table(
    "staff_roles",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("name", String),
)

_wings = table(
    "wings",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("is_active", Boolean),
)

_departments = table(
    "departments",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("is_active", Boolean),
    column("wing_id", PG_UUID(as_uuid=True)),
)

_admissions = table(
    "admissions",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("ip_id", String),
)


class AppointmentsRepository(BaseRepository[Appointment]):
    """Repository managing appointments persistence and composite lookups."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        super().__init__(session=db, hospital_id=hospital_id)
        self.db = db

    def get_by_id(self, appointment_id: UUID) -> Appointment | None:
        """Fetch an appointment by ID within current hospital tenant."""
        return (
            self.db.query(Appointment)
            .filter(
                Appointment.id == appointment_id,
                Appointment.hospital_id == self.hospital_id,
            )
            .first()
        )

    def next_encounter_id(self, kind: str = "OP") -> str:
        """Generate human-readable encounter sequence (e.g. OP-2026-00001)."""
        year = date.today().year
        prefix = f"{kind}-{year}-"
        current = (
            self.db.query(func.max(Appointment.op_id))
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.op_id.like(f"{prefix}%"),
            )
            .scalar()
        )
        seq = 0
        if current:
            try:
                seq = int(str(current).rsplit("-", 1)[-1])
            except ValueError:
                seq = 0
        return f"{prefix}{seq + 1:05d}"

    def next_queue_token(self, on_date: date, doctor_id: UUID) -> int:
        """Sequential queue token per doctor on a given date."""
        cur = (
            self.db.query(func.max(Appointment.queue_token))
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.doctor_id == doctor_id,
                Appointment.appointment_date == on_date,
            )
            .scalar()
        )
        return (cur or 0) + 1

    def list_today(
        self,
        doctor_id: UUID | None = None,
        status: AppointmentStatus | None = None,
    ) -> list[Appointment]:
        """List today's appointments ordered chronologically."""
        today = date.today()
        q = (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.appointment_date == today,
            )
        )
        if doctor_id:
            q = q.filter(Appointment.doctor_id == doctor_id)
        if status:
            q = q.filter(Appointment.status == status)
        return q.order_by(Appointment.appointment_time.asc()).all()

    def list_calendar(
        self,
        week_start: date,
        doctor_id: UUID | None = None,
    ) -> list[Appointment]:
        """List appointments for 7 days starting from week_start."""
        week_end = week_start + timedelta(days=7)
        q = (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.appointment_date >= week_start,
                Appointment.appointment_date < week_end,
                Appointment.status != AppointmentStatus.cancelled,
            )
        )
        if doctor_id:
            q = q.filter(Appointment.doctor_id == doctor_id)
        return q.order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc()).all()

    def list_queue(self, doctor_id: UUID | None = None) -> list[Appointment]:
        """List waiting / in-progress appointments for queue display."""
        today = date.today()
        q = (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.appointment_date == today,
                Appointment.status.in_([AppointmentStatus.waiting, AppointmentStatus.scheduled]),
            )
        )
        if doctor_id:
            q = q.filter(Appointment.doctor_id == doctor_id)
        return q.order_by(
            Appointment.queue_token.asc().nullslast(),
            Appointment.appointment_time.asc(),
        ).all()

    def list_history(
        self,
        patient_id: UUID | None = None,
        doctor_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        status: AppointmentStatus | None = None,
    ) -> list[Appointment]:
        """List historical appointments matching filters."""
        q = self.db.query(Appointment).filter(Appointment.hospital_id == self.hospital_id)
        if patient_id:
            q = q.filter(Appointment.patient_id == patient_id)
        if doctor_id:
            q = q.filter(Appointment.doctor_id == doctor_id)
        if from_date:
            q = q.filter(Appointment.appointment_date >= from_date)
        if to_date:
            q = q.filter(Appointment.appointment_date <= to_date)
        if status:
            q = q.filter(Appointment.status == status)
        return q.order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc()).all()

    def list_ipd_requests(self) -> list[Appointment]:
        """List appointments requesting IPD bed transfer."""
        return (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.status == AppointmentStatus.ipd_transfer_requested,
            )
            .order_by(Appointment.created_at.desc())
            .all()
        )

    # -----------------------------------------------------------------------
    # Metadata Helper Queries
    # -----------------------------------------------------------------------
    def list_active_doctors(self) -> list[dict[str, Any]]:
        """List active doctors for appointment booking."""
        stmt = (
            select(
                _hospital_users.c.id,
                _hospital_users.c.name,
                _hospital_users.c.phone,
                _hospital_users.c.email,
                _hospital_users.c.specialization,
                _hospital_users.c.qualification,
                _hospital_users.c.medical_registration_number,
                _hospital_users.c.years_of_experience,
                _hospital_users.c.consultation_room,
            )
            .select_from(
                _hospital_users.join(
                    _staff_roles, _hospital_users.c.role_id == _staff_roles.c.id
                )
            )
            .where(
                _hospital_users.c.hospital_id == self.hospital_id,
                _hospital_users.c.is_active.is_(True),
                func.lower(_staff_roles.c.name).like("%doctor%"),
            )
            .order_by(_hospital_users.c.name.asc())
        )
        return [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "phone": r["phone"],
                "email": r["email"],
                "specialization": r["specialization"],
                "qualification": r["qualification"],
                "medical_registration_number": r["medical_registration_number"],
                "years_of_experience": r["years_of_experience"],
                "consultation_room": r["consultation_room"],
            }
            for r in self.db.execute(stmt).mappings().all()
        ]

    def list_active_nurses(self) -> list[dict[str, Any]]:
        """List active nurses for assignment."""
        stmt = (
            select(
                _hospital_users.c.id,
                _hospital_users.c.name,
                _hospital_users.c.phone,
                _hospital_users.c.email,
            )
            .select_from(
                _hospital_users.join(
                    _staff_roles, _hospital_users.c.role_id == _staff_roles.c.id
                )
            )
            .where(
                _hospital_users.c.hospital_id == self.hospital_id,
                _hospital_users.c.is_active.is_(True),
                or_(
                    func.lower(_staff_roles.c.name).like("%nurse%"),
                    func.lower(_staff_roles.c.name).like("%nursing%"),
                ),
            )
            .order_by(_hospital_users.c.name.asc())
        )
        return [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "phone": r["phone"],
                "email": r["email"],
            }
            for r in self.db.execute(stmt).mappings().all()
        ]

    def list_wings(self) -> list[dict[str, Any]]:
        """List hospital wings."""
        stmt = (
            select(_wings.c.id, _wings.c.name)
            .where(_wings.c.hospital_id == self.hospital_id, _wings.c.is_active.is_(True))
            .order_by(_wings.c.name.asc())
        )
        return [dict(r) for r in self.db.execute(stmt).mappings().all()]

    def list_departments(self) -> list[dict[str, Any]]:
        """List hospital departments."""
        stmt = (
            select(_departments.c.id, _departments.c.name, _departments.c.wing_id)
            .where(_departments.c.hospital_id == self.hospital_id, _departments.c.is_active.is_(True))
            .order_by(_departments.c.name.asc())
        )
        return [dict(r) for r in self.db.execute(stmt).mappings().all()]

    def list_visit_types(self) -> list[dict[str, Any]]:
        """List active appointment types."""
        types = (
            self.db.query(AppointmentType)
            .filter(
                AppointmentType.hospital_id == self.hospital_id,
                AppointmentType.is_active.is_(True),
            )
            .order_by(AppointmentType.name.asc())
            .all()
        )
        return [
            {
                "id": str(t.id),
                "name": t.name,
                "slot_duration_minutes": t.slot_duration_minutes,
                "description": t.description,
                "is_follow_up": t.is_follow_up,
            }
            for t in types
        ]

    def hydrate_appointment_items(self, appointments: list[Appointment]) -> list[AppointmentListItem]:
        """Convert a list of Appointment ORM entities into AppointmentListItem DTOs."""
        if not appointments:
            return []

        # Collect foreign keys for bulk hydration
        patient_ids = list({a.patient_id for a in appointments if a.patient_id})
        user_ids = list(
            {a.doctor_id for a in appointments if a.doctor_id}
            | {a.nurse_id for a in appointments if a.nurse_id}
        )
        admission_ids = list({a.admission_id for a in appointments if a.admission_id})

        # Bulk fetch patients
        patients_map: dict[UUID, Any] = {}
        if patient_ids:
            p_rows = self.db.query(Patient).filter(Patient.id.in_(patient_ids)).all()
            patients_map = {p.id: p for p in p_rows}

        # Bulk fetch users (doctors/nurses)
        users_map: dict[UUID, str] = {}
        if user_ids:
            stmt = select(_hospital_users.c.id, _hospital_users.c.name).where(
                _hospital_users.c.id.in_(user_ids)
            )
            users_map = {r[0]: r[1] for r in self.db.execute(stmt).all()}

        # Bulk fetch admissions (for ip_id)
        admissions_map: dict[UUID, str] = {}
        if admission_ids:
            stmt = select(_admissions.c.id, _admissions.c.ip_id).where(
                _admissions.c.id.in_(admission_ids)
            )
            admissions_map = {r[0]: r[1] for r in self.db.execute(stmt).all()}

        items: list[AppointmentListItem] = []
        for a in appointments:
            pat = patients_map.get(a.patient_id)
            doc_name = users_map.get(a.doctor_id)
            nurse_name = users_map.get(a.nurse_id) if a.nurse_id else None
            ip_id = admissions_map.get(a.admission_id) if a.admission_id else None

            items.append(
                AppointmentListItem(
                    id=a.id,
                    hospital_id=a.hospital_id,
                    doctor_id=a.doctor_id,
                    patient_id=a.patient_id,
                    appointment_date=a.appointment_date,
                    appointment_time=a.appointment_time,
                    purpose=a.purpose or "",
                    visit_type=a.visit_type or "OPD",
                    appointment_type_id=a.appointment_type_id,
                    wing_id=a.wing_id,
                    department_id=a.department_id,
                    consultation_fee=float(a.consultation_fee or 0.0),
                    followup_eligibility=a.followup_eligibility,
                    status=a.status,
                    booking_kind=a.booking_kind or "future",
                    notes=a.notes,
                    queue_token=a.queue_token,
                    checked_in_at=a.checked_in_at,
                    created_at=a.created_at,
                    patient_name=pat.name if pat else None,
                    patient_uhid=pat.uhid if pat else None,
                    patient_mobile=pat.mobile if pat else None,
                    doctor_name=doc_name,
                    op_id=a.op_id,
                    admission_id=a.admission_id,
                    ip_id=ip_id,
                    nurse_id=a.nurse_id,
                    nurse_name=nurse_name,
                )
            )
        return items
