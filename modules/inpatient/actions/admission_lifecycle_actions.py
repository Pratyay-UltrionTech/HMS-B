"""
Canonical inpatient admission lifecycle service.

Single authoritative implementation for the REQUESTED → ADMITTED portion of
the inpatient episode (spec §§18-20). All entry points — Doctor IPD form
finalization, Transfer to Inpatient, Reception, Emergency, direct bed
admission — must converge here instead of implementing independent
Admission-creation logic.

Conventions:
- ``Admission.status == requested`` is the canonical admission-intent state.
  ``Appointment.status == ipd_transfer_requested`` is a derived compatibility
  mirror, always synchronized inside the same transaction (spec §6).
- ``Patient.status`` stays ``active`` while requested; flips to ``admitted``
  only on acceptance (spec §16).
- No bed, no occupancy change, no billing charge on request creation.
- Repeated requests are idempotent: the existing open episode is returned
  instead of raising (concurrency losers re-read the winner's row).
- Database race losers (partial-unique violations) are mapped to HTTP 409,
  never an unhandled 500 (spec §24).

Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from modules.inpatient.contracts.inpatient_contracts import AdmissionDetail
from modules.inpatient.db.admissions_repository import AdmissionsRepository
from modules.inpatient.entities.admission import AdmissionStatus
from modules.patients.entities.patient import Patient
from shared.audit.service import write_audit_log


def _link_appointment_compat(
    db: Session,
    hospital_id: UUID,
    source_appointment_id: UUID | None,
    admission_id: UUID,
    admitted: bool,
) -> None:
    """Mirror canonical admission state onto the source appointment row.

    Requested → ``ipd_transfer_requested``; accepted → ``transferred_to_inpatient``.
    Runs inside the caller's transaction so the two states cannot diverge (§6).
    """
    if source_appointment_id is None:
        return
    from modules.appointments.entities.appointment import Appointment, AppointmentStatus

    appt = (
        db.query(Appointment)
        .filter(
            Appointment.id == source_appointment_id,
            Appointment.hospital_id == hospital_id,
        )
        .first()
    )
    if appt is None:
        return
    if admitted:
        appt.status = AppointmentStatus.transferred_to_inpatient
    else:
        if appt.status != AppointmentStatus.transferred_to_inpatient:
            appt.status = AppointmentStatus.ipd_transfer_requested
    appt.admission_id = admission_id


class EnsureAdmissionRequestAction:
    """Idempotent ``ensure_admission_request(...)`` (spec §19).

    1. Lock/check existing open episode (requested/admitted/discharge_requested).
    2. Return it when present (no duplicate) — syncing the appointment mirror.
    3. Otherwise create ``Admission(status=requested)`` bedless.
    4. Link the source appointment as compatibility state.
    5. Audit + commit atomically; IntegrityError losers re-read the winner.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = AdmissionsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        patient_id: UUID,
        actor: dict[str, Any],
        doctor_id: UUID | None = None,
        notes: str | None = None,
        source_appointment_id: UUID | None = None,
    ) -> tuple[Admission, bool]:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            from shared.exceptions.base import NotFoundError

            raise NotFoundError("Patient not found")

        existing = self.repo.get_open_admission(hospital_id, patient_id, for_update=True)
        if existing:
            if source_appointment_id and not existing.source_appointment_id:
                existing.source_appointment_id = source_appointment_id
            _link_appointment_compat(
                self.db,
                hospital_id,
                source_appointment_id or existing.source_appointment_id,
                existing.id,
                admitted=existing.status != AdmissionStatus.requested,
            )
            write_audit_log(
                self.db,
                hospital_id=hospital_id,
                actor=actor,
                action="update",
                entity_type="admission",
                entity_id=str(existing.id),
                summary="Admission request already exists — linked (idempotent)",
            )
            self.db.commit()
            refreshed = self.repo.get_admission_by_id(hospital_id, existing.id)
            return refreshed or existing, False

        admission = self.repo.create_request(
            hospital_id=hospital_id,
            patient=patient,
            doctor_id=doctor_id,
            notes=notes,
            source_appointment_id=source_appointment_id,
        )
        _link_appointment_compat(
            self.db, hospital_id, source_appointment_id, admission.id, admitted=False
        )
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Admission requested for {patient.uhid} {patient.name}",
        )
        try:
            self.db.commit()
        except IntegrityError:
            # Lost a concurrent request race: another transaction created the
            # open episode first. Return the winner instead of 500 (spec §24).
            self.db.rollback()
            winner = self.repo.get_open_admission(hospital_id, patient_id)
            if winner:
                return winner, False
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An admission request already exists for this patient",
            )
        refreshed = self.repo.get_admission_by_id(hospital_id, admission.id)
        return refreshed or admission, True


class AcceptAdmissionAction:
    """Idempotent ``accept_admission(...)`` (spec §§8, 20).

    ``requested → admitted``, with or without a bed. Bedless acceptance
    yields the canonical "Admitted — Awaiting Bed" (bed_id NULL, no segment).
    All patient/bed/appointment/billing mutations occur in one transaction
    holding row locks on the admission and (when given) the target bed.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = AdmissionsRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        actor: dict[str, Any],
        admission_id: UUID | None = None,
        patient_id: UUID | None = None,
        ward_id: UUID | None = None,
        room_id: UUID | None = None,
        bed_id: UUID | None = None,
        doctor_id: UUID | None = None,
        notes: str | None = None,
    ) -> AdmissionDetail:
        from modules.inpatient.actions.admission_actions import to_admission_detail
        from modules.inpatient.services.inpatient_billing_service import (
            InpatientBillingService,
            next_ip_encounter_id,
        )
        from modules.patients.entities.patient import PatientStatus
        from shared.exceptions.base import ConflictError, NotFoundError, ValidationError

        if admission_id:
            # Locked load by id (joinedload omitted under FOR UPDATE).
            from modules.inpatient.entities.admission import Admission

            admission = (
                self.db.query(Admission)
                .filter(
                    Admission.id == admission_id,
                    Admission.hospital_id == hospital_id,
                )
                .with_for_update()
                .first()
            )
        elif patient_id:
            admission = self.repo.get_requested_admission(
                hospital_id, patient_id, for_update=True
            )
        else:
            raise ValidationError("admission_id or patient_id is required")
        if not admission:
            raise NotFoundError("Admission request not found")
        if admission.status != AdmissionStatus.requested:
            raise ConflictError(
                f"Admission is already {admission.status.value}; only requested admissions can be accepted"
            )

        with_bed = bed_id is not None
        if (ward_id is None) != (room_id is None) or (ward_id is None) != (bed_id is None):
            raise ValidationError("ward_id, room_id and bed_id must be provided together")

        bed = None
        if with_bed:
            from modules.beds.entities.bed import Bed

            bed = (
                self.db.query(Bed)
                .filter(Bed.id == bed_id, Bed.hospital_id == hospital_id)
                .with_for_update()
                .first()
            )
            if not bed:
                raise NotFoundError("Bed not found")
            if bed.is_occupied:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Selected bed is already occupied or was just taken",
                )
            if bed.ward_id != ward_id or bed.room_id != room_id:
                raise ValidationError("Ward/Room does not match selected bed")

        now = datetime.now(timezone.utc)
        admission.status = AdmissionStatus.admitted
        admission.admitted_at = now
        if doctor_id:
            admission.doctor_id = doctor_id
        if notes and notes.strip():
            admission.notes = notes.strip()
        if not admission.ip_id:
            admission.ip_id = next_ip_encounter_id(self.db, hospital_id)
        if with_bed:
            admission.ward_id = ward_id
            admission.room_id = room_id
            admission.bed_id = bed.id
            bed.is_occupied = True
            from modules.inpatient.entities.admission import BedStaySegment

            self.db.add(
                BedStaySegment(
                    hospital_id=hospital_id,
                    admission_id=admission.id,
                    ward_id=bed.ward_id,
                    room_id=bed.room_id,
                    bed_id=bed.id,
                    rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0)
                    if bed.ward
                    else 0.0,
                    started_at=now,
                    ended_at=None,
                )
            )

        patient = (
            self.db.query(Patient)
            .filter(
                Patient.id == admission.patient_id,
                Patient.hospital_id == hospital_id,
            )
            .first()
        )
        if patient:
            patient.status = PatientStatus.admitted

        # Appointment mirror: every pending request for this patient resolves
        # to transferred_to_inpatient inside the same transaction (spec §6).
        from modules.appointments.entities.appointment import Appointment, AppointmentStatus

        pending = (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == hospital_id,
                Appointment.patient_id == admission.patient_id,
                Appointment.status == AppointmentStatus.ipd_transfer_requested,
            )
            .all()
        )
        for appt in pending:
            appt.status = AppointmentStatus.transferred_to_inpatient
            appt.admission_id = admission.id
        if admission.source_appointment_id:
            src = (
                self.db.query(Appointment)
                .filter(
                    Appointment.id == admission.source_appointment_id,
                    Appointment.hospital_id == hospital_id,
                )
                .first()
            )
            if src:
                src.status = AppointmentStatus.transferred_to_inpatient
                src.admission_id = admission.id

        if with_bed and bed is not None:
            billing = InpatientBillingService(self.db)
            billing.ensure_admission_charge(
                hospital_id=hospital_id,
                patient_id=admission.patient_id,
                admission_id=admission.id,
                ward_name=bed.ward.name if bed.ward else None,
                admission_fee=float(getattr(bed.ward, "admission_fee", 0) or 0)
                if bed.ward
                else 0.0,
                created_by_name=str(actor.get("name") or "System"),
            )

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="update",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=(
                f"Accepted admission ({admission.ip_id}) "
                + (f"to bed {bed.bed_code}" if bed else "as awaiting bed")
            ),
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Admission state changed concurrently; please retry",
            ) from exc
        refreshed = self.repo.get_admission_by_id(hospital_id, admission.id)
        detail = to_admission_detail(refreshed or admission)
        detail.is_awaiting_bed = detail.bed_id is None and detail.status == AdmissionStatus.admitted
        return detail
