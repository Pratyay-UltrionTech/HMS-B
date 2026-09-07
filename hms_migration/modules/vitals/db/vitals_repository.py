"""
Data access repository for the Vitals module.

Conforms to UltrionTech-Backend-Template modules/<name>/db/ specification.
Handles database queries, tenant filtering, eager relationship loading,
and record persistence for VitalReading domain objects.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from hms_migration.infrastructure.postgres.base_repository import BaseRepository
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.vitals.db.appointment_reader import AppointmentReader
from hms_migration.modules.vitals.entities.vital_reading import VitalReading


class VitalsRepository(BaseRepository[VitalReading]):
    """Repository handling SQL queries and ORM operations for vitals."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        super().__init__(session=db, hospital_id=hospital_id)
        self.db = db
        self.appointment_reader = AppointmentReader(db=db, hospital_id=hospital_id)

    def _attach_metadata(self, rows: list[VitalReading]) -> list[VitalReading]:
        """Attach appointment and patient domain context onto VitalReading entities."""
        if not rows:
            return rows
        appt_ids = {r.appointment_id for r in rows if r.appointment_id}
        patient_ids = {r.patient_id for r in rows if r.patient_id}

        appts = {}
        for appt_id in appt_ids:
            appt = self.appointment_reader.get_appointment_by_id(appt_id)
            if appt:
                appts[appt_id] = appt

        # Resolve patients for any rows whose appointment did not load patient
        patient_map = {}
        for a in appts.values():
            if getattr(a, "patient", None):
                patient_map[a.patient_id] = a.patient

        missing_pids = [pid for pid in patient_ids if pid not in patient_map]
        if missing_pids:
            found_patients = (
                self.db.query(Patient)
                .filter(Patient.id.in_(missing_pids))
                .all()
            )
            for p in found_patients:
                patient_map[p.id] = p

        for r in rows:
            appt = appts.get(r.appointment_id)
            r.appointment = appt
            if appt and getattr(appt, "patient", None):
                r.patient = appt.patient
            else:
                r.patient = patient_map.get(r.patient_id)
        return rows

    def get_appointment_ids_with_vitals(self, appt_ids: list[UUID]) -> set[UUID]:
        """Return the subset of appointment IDs that have at least one recorded vital reading."""
        if not appt_ids:
            return set()
        rows = (
            self.db.query(VitalReading.appointment_id)
            .filter(
                VitalReading.hospital_id == self.hospital_id,
                VitalReading.appointment_id.in_(appt_ids),
            )
            .distinct()
            .all()
        )
        return {r[0] for r in rows}

    def get_vitals_for_appointments(self, appt_ids: list[UUID]) -> list[VitalReading]:
        """Fetch vital readings for a list of appointments, ordered by creation time."""
        if not appt_ids:
            return []
        rows = (
            self.db.query(VitalReading)
            .filter(
                VitalReading.hospital_id == self.hospital_id,
                VitalReading.appointment_id.in_(appt_ids),
            )
            .order_by(VitalReading.created_at.asc())
            .all()
        )
        return self._attach_metadata(rows)

    def list_vitals(
        self,
        appointment_id: UUID | None = None,
        patient_id: UUID | None = None,
    ) -> list[VitalReading]:
        """Query vital readings filtered by appointment_id or patient_id within this hospital."""
        q = self.db.query(VitalReading).filter(VitalReading.hospital_id == self.hospital_id)
        if appointment_id:
            q = q.filter(VitalReading.appointment_id == appointment_id)
        if patient_id:
            q = q.filter(VitalReading.patient_id == patient_id)
        rows = q.order_by(VitalReading.created_at.asc()).all()
        return self._attach_metadata(rows)

    def get_vital_by_id(self, vital_id: UUID) -> VitalReading | None:
        """Fetch a vital reading by ID scoped to this hospital."""
        row = (
            self.db.query(VitalReading)
            .filter(
                VitalReading.id == vital_id,
                VitalReading.hospital_id == self.hospital_id,
            )
            .first()
        )
        if row:
            self._attach_metadata([row])
        return row

    def get_vitals_by_ids(self, vital_ids: list[UUID]) -> list[VitalReading]:
        """Reload vital readings by IDs with joined relationships."""
        if not vital_ids:
            return []
        rows = (
            self.db.query(VitalReading)
            .filter(VitalReading.id.in_(vital_ids))
            .all()
        )
        return self._attach_metadata(rows)


    # Forwarding methods to appointment_reader for backward compatibility
    def get_today_appointments(self, on_date):
        return self.appointment_reader.get_today_appointments(on_date)

    def get_appointment_by_id(self, appointment_id: UUID):
        return self.appointment_reader.get_appointment_by_id(appointment_id)

    def get_next_queue_token(self, doctor_id: UUID, on_date):
        return self.appointment_reader.get_next_queue_token(doctor_id, on_date)
