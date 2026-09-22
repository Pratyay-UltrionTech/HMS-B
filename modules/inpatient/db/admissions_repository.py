"""
Database repository for Inpatient Admissions.

Conforms to UltrionTech-Backend-Template modules/inpatient/db/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from modules.beds.entities.bed import Bed
from modules.inpatient.entities.admission import (
    ACTIVE_INPATIENT_STATUSES,
    OPEN_ADMISSION_STATUSES,
    Admission,
    AdmissionStatus,
)
from modules.patients.entities.patient import Patient, PatientStatus

# Re-exported so action/service layers share one canonical definition of
# "active inpatient census" (spec §14) instead of hand-rolling predicates.
ACTIVE_STATUSES = (AdmissionStatus.admitted, AdmissionStatus.discharge_requested)
OPEN_STATUSES = (
    AdmissionStatus.requested,
    AdmissionStatus.admitted,
    AdmissionStatus.discharge_requested,
)


class AdmissionsRepository:
    """Repository handling database operations for Inpatient Admissions."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_admission_by_id(
        self,
        hospital_id: UUID,
        admission_id: UUID,
        statuses: Sequence[AdmissionStatus] | None = None,
    ) -> Admission | None:
        """Fetch admission by id, optionally filtering by statuses."""
        q = (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(Admission.id == admission_id, Admission.hospital_id == hospital_id)
        )
        if statuses:
            q = q.filter(Admission.status.in_(statuses))
        return q.first()

    def get_admission_by_patient_id(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        statuses: Sequence[AdmissionStatus] | None = None,
    ) -> Admission | None:
        """Fetch admission by patient_id, optionally filtering by statuses."""
        q = (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(Admission.patient_id == patient_id, Admission.hospital_id == hospital_id)
        )
        if statuses:
            q = q.filter(Admission.status.in_(statuses))
        return q.first()

    def get_active_admission(
        self,
        hospital_id: UUID,
        admission_id: UUID | None,
        patient_id: UUID | None,
        statuses: Sequence[AdmissionStatus] = (AdmissionStatus.admitted,),
    ) -> Admission | None:
        """Resolve active admission by admission_id or patient_id."""
        if admission_id:
            return self.get_admission_by_id(hospital_id, admission_id, statuses)
        if patient_id:
            return self.get_admission_by_patient_id(hospital_id, patient_id, statuses)
        return None

    def list_active_admissions(
        self,
        hospital_id: UUID,
        search: str | None = None,
        doctor_id: UUID | None = None,
    ) -> list[Admission]:
        """List currently active or discharge-requested admissions."""
        q = (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(
                Admission.hospital_id == hospital_id,
                Admission.status.in_([AdmissionStatus.admitted, AdmissionStatus.discharge_requested]),
            )
        )
        if doctor_id:
            q = q.filter(Admission.doctor_id == doctor_id)
        if search and search.strip():
            term = f"%{search.strip()}%"
            q = q.filter(
                Admission.patient_id.in_(
                    self.db.query(Patient.id).filter(
                        Patient.hospital_id == hospital_id,
                        or_(
                            Patient.name.ilike(term),
                            Patient.uhid.ilike(term),
                            Patient.mobile.ilike(term),
                        ),
                    )
                )
            )
        return q.order_by(Admission.admitted_at.desc()).all()

    def list_discharge_requests(self, hospital_id: UUID) -> list[Admission]:
        """List admissions awaiting nurse discharge completion."""
        return (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(
                Admission.hospital_id == hospital_id,
                Admission.status == AdmissionStatus.discharge_requested,
            )
            .order_by(Admission.admitted_at.asc())
            .all()
        )

    def create_admission(
        self,
        hospital_id: UUID,
        patient: Patient,
        bed: Bed,
        ward_id: UUID,
        room_id: UUID,
        doctor_id: UUID | None,
        ip_id: str,
        notes: str | None = None,
        admitted_at: datetime | None = None,
        source_appointment_id: UUID | None = None,
    ) -> Admission:
        """Create new admission and set bed and patient occupancy state."""
        admission = Admission(
            hospital_id=hospital_id,
            patient_id=patient.id,
            ward_id=ward_id,
            room_id=room_id,
            bed_id=bed.id,
            doctor_id=doctor_id,
            status=AdmissionStatus.admitted,
            notes=notes.strip() if notes else None,
            admitted_at=admitted_at or datetime.now(timezone.utc),
            ip_id=ip_id,
            source_appointment_id=source_appointment_id,
            # Immutable demographic snapshot (FLAW-010): freeze identity at the
            # moment of admission so later patient corrections do not rewrite
            # this encounter's wristband / discharge summary identity.
            patient_name=patient.name,
            gender=patient.gender,
            age_at_admission=patient.age,
        )
        bed.is_occupied = True
        patient.status = PatientStatus.admitted
        self.db.add(admission)
        self.db.flush()

        # Reconcile any pending IPD transfer requests for this patient so they
        # don't linger in admission queues once the patient has an active bed
        from modules.appointments.entities.appointment import Appointment, AppointmentStatus
        pending_appts = (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == hospital_id,
                Appointment.patient_id == patient.id,
                Appointment.status == AppointmentStatus.ipd_transfer_requested,
            )
            .all()
        )
        for appt in pending_appts:
            appt.status = AppointmentStatus.transferred_to_inpatient
            appt.admission_id = admission.id

        return admission

    # ── Canonical lifecycle helpers (spec §§18-20) ──────────────────────

    def get_open_admission(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        for_update: bool = False,
    ) -> Admission | None:
        """Return the patient's single open episode (requested/admitted/
        discharge_requested), optionally locking the row.

        Invariant 1: at most one such row may exist (DB partial index
        ``uq_admissions_active_patient`` backs this check under concurrency).
        """
        q = self.db.query(Admission).filter(
            Admission.hospital_id == hospital_id,
            Admission.patient_id == patient_id,
            Admission.status.in_(OPEN_STATUSES),
        )
        if for_update:
            q = q.with_for_update()
        return q.first()

    def get_requested_admission(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        for_update: bool = False,
    ) -> Admission | None:
        q = self.db.query(Admission).filter(
            Admission.hospital_id == hospital_id,
            Admission.patient_id == patient_id,
            Admission.status == AdmissionStatus.requested,
        )
        if for_update:
            q = q.with_for_update()
        return q.first()

    def list_admission_requests(self, hospital_id: UUID) -> list[Admission]:
        """Canonical admission-request queue (spec §7): requested only."""
        return (
            self.db.query(Admission)
            .options(
                joinedload(Admission.patient),
                joinedload(Admission.ward),
                joinedload(Admission.room),
                joinedload(Admission.bed),
                joinedload(Admission.doctor),
            )
            .filter(
                Admission.hospital_id == hospital_id,
                Admission.status == AdmissionStatus.requested,
            )
            .order_by(Admission.admitted_at.asc())
            .all()
        )

    def create_request(
        self,
        hospital_id: UUID,
        patient: Patient,
        doctor_id: UUID | None = None,
        notes: str | None = None,
        source_appointment_id: UUID | None = None,
    ) -> Admission:
        """Create a bedless admission request (no bed, no occupancy change).

        Caller must hold no conflicting open episode (see
        ``EnsureAdmissionRequestAction``). Patient stays ``active`` — the
        patient becomes ``admitted`` only on acceptance (spec §16).
        """
        admission = Admission(
            hospital_id=hospital_id,
            patient_id=patient.id,
            ward_id=None,
            room_id=None,
            bed_id=None,
            doctor_id=doctor_id,
            status=AdmissionStatus.requested,
            notes=notes.strip() if notes else None,
            admitted_at=datetime.now(timezone.utc),
            ip_id=None,
            source_appointment_id=source_appointment_id,
            patient_name=patient.name,
            gender=patient.gender,
            age_at_admission=patient.age,
        )
        self.db.add(admission)
        self.db.flush()
        return admission
