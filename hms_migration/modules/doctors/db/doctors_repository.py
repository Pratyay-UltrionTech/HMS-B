"""
Database repository for Doctor profiles, directory, and patient histories.

Conforms to UltrionTech-Backend-Template modules/doctors/db/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import column, func, or_, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.entities.admission import Admission, IpdFormSubmission
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.vitals.entities.vital_reading import VitalReading


class DoctorsRepository:
    """Repository handling doctor directory queries and clinical history aggregations."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_doctors(
        self, hospital_id: UUID, specialization: str | None = None
    ) -> list[dict[str, Any]]:
        """List active doctors with patient and today appointment counts."""
        q = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role), joinedload(HospitalUser.shift))
            .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        )
        if specialization:
            q = q.filter(HospitalUser.specialization.ilike(f"%{specialization.strip()}%"))

        users = q.order_by(HospitalUser.name.asc()).all()
        doctors = [u for u in users if u.role and "doctor" in (u.role.name or "").lower()]

        today = date.today()
        # Today appointment counts per doctor
        today_rows = (
            self.db.query(Appointment.doctor_id, func.count(Appointment.id))
            .filter(
                Appointment.hospital_id == hospital_id,
                Appointment.appointment_date == today,
                Appointment.status.notin_([AppointmentStatus.cancelled]),
            )
            .group_by(Appointment.doctor_id)
            .all()
        )
        today_map = {row[0]: row[1] for row in today_rows}

        # Distinct patient counts per doctor
        patient_rows = (
            self.db.query(Appointment.doctor_id, func.count(func.distinct(Appointment.patient_id)))
            .filter(
                Appointment.hospital_id == hospital_id,
                Appointment.status.notin_([AppointmentStatus.cancelled]),
            )
            .group_by(Appointment.doctor_id)
            .all()
        )
        patient_map = {row[0]: row[1] for row in patient_rows}

        result = []
        for d in doctors:
            result.append(
                {
                    "id": d.id,
                    "name": d.name,
                    "email": d.email,
                    "phone": d.phone,
                    "role_name": d.role.name if d.role else None,
                    "specialization": d.specialization,
                    "medical_registration_number": d.medical_registration_number,
                    "qualification": d.qualification,
                    "years_of_experience": d.years_of_experience,
                    "consultation_room": d.consultation_room,
                    "show_financial_details": getattr(d, "show_financial_details", True),
                    "department_id": d.shift.department_id if d.shift else None,
                    "department_name": None,
                    "custom_values": d.custom_values or {},
                    "is_active": d.is_active,
                    "patient_count": patient_map.get(d.id, 0),
                    "today_appointment_count": today_map.get(d.id, 0),
                }
            )
        return result

    def get_doctor(self, hospital_id: UUID, doctor_id: UUID) -> HospitalUser | None:
        """Fetch doctor by id with role and shift loaded."""
        return (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role), joinedload(HospitalUser.shift))
            .filter(HospitalUser.id == doctor_id, HospitalUser.hospital_id == hospital_id)
            .first()
        )

    def get_hospital_profile(self, hospital_id: UUID) -> dict[str, Any] | None:
        """Fetch hospital clinic profile using Core query."""
        _hospitals = table(
            "hospitals",
            column("id", PG_UUID(as_uuid=True)),
            column("hospital_id", column("hospital_id").type),
            column("name", column("name").type),
            column("address", column("address").type),
            column("phone", column("phone").type),
            column("email", column("email").type),
        )
        row = self.db.execute(
            select(
                _hospitals.c.id,
                _hospitals.c.hospital_id,
                _hospitals.c.name,
                _hospitals.c.address,
                _hospitals.c.phone,
                _hospitals.c.email,
            ).where(_hospitals.c.id == hospital_id)
        ).first()
        if not row:
            return None
        return {
            "id": row[0],
            "hospital_id": str(row[1] or ""),
            "name": row[2] or "Hospital",
            "address": row[3] or "—",
            "phone": row[4] or "—",
            "email": row[5] or "—",
        }

    def search_patients(
        self, hospital_id: UUID, query_str: str | None = None
    ) -> list[Patient]:
        """Search patients by name, uhid, or mobile."""
        q = self.db.query(Patient).filter(Patient.hospital_id == hospital_id)
        if query_str and query_str.strip():
            term = f"%{query_str.strip()}%"
            q = q.filter(
                or_(
                    Patient.name.ilike(term),
                    Patient.uhid.ilike(term),
                    Patient.mobile.ilike(term),
                )
            )
        return q.order_by(Patient.name.asc()).limit(50).all()

    def list_doctor_patients(
        self, hospital_id: UUID, doctor_id: UUID, search: str | None = None
    ) -> list[Patient]:
        """List patients associated with a specific doctor."""
        appt_patient_ids = (
            self.db.query(Appointment.patient_id)
            .filter(Appointment.hospital_id == hospital_id, Appointment.doctor_id == doctor_id)
            .distinct()
        )
        adm_patient_ids = (
            self.db.query(Admission.patient_id)
            .filter(Admission.hospital_id == hospital_id, Admission.doctor_id == doctor_id)
            .distinct()
        )
        q = self.db.query(Patient).filter(
            Patient.hospital_id == hospital_id,
            or_(Patient.id.in_(appt_patient_ids), Patient.id.in_(adm_patient_ids)),
        )
        if search and search.strip():
            term = f"%{search.strip()}%"
            q = q.filter(
                or_(
                    Patient.name.ilike(term),
                    Patient.uhid.ilike(term),
                    Patient.mobile.ilike(term),
                )
            )
        return q.order_by(Patient.name.asc()).all()
