"""
Actions managing appointment status transitions and lifecycle mutations.

Conforms to UltrionTech-Backend-Template modules/appointments/actions/ specification.
"""

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.appointments.contracts.appointments_contracts import (
    AdmitIpdRequest,
    AppointmentListItem,
    AssignNurseRequest,
    RescheduleRequest,
)
from modules.appointments.db.appointments_repository import AppointmentsRepository
from modules.appointments.db.availability_reader import AvailabilityReader
from modules.appointments.entities.enums import AppointmentStatus
from modules.appointments.exceptions.appointments_exceptions import (
    AppointmentConflictError,
    AppointmentNotFoundError,
    AppointmentValidationError,
)
from modules.appointments.services.appointment_lifecycle import (
    complete_appointment_record,
    mark_in_progress,
)
from modules.appointments.validators.appointments_validator import AppointmentsValidator
from shared.audit import write_audit_log


class CheckInAction:
    """Check in a scheduled appointment, move to waiting, and allocate queue token."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo

    def execute(self, appointment_id: UUID, user: dict[str, Any]) -> AppointmentListItem:
        appt = self.repo.get_by_id(appointment_id)
        if not appt:
            raise AppointmentNotFoundError()

        AppointmentsValidator.assert_can_check_in(appt)

        if not appt.queue_token:
            appt.queue_token = self.repo.next_queue_token(
                appt.appointment_date, appt.doctor_id
            )

        mark_in_progress(appt)
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="update",
            entity_type="appointment",
            entity_id=appt.id,
            summary=f"Checked in appointment {appt.id} (Queue Token #{appt.queue_token})",
        )
        self.db.commit()
        self.db.refresh(appt)

        items = self.repo.hydrate_appointment_items([appt])
        return items[0]


class CompleteAppointmentAction:
    """Mark appointment Completed after checking diagnostic blockers."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo

    def execute(
        self,
        appointment_id: UUID,
        user: dict[str, Any],
        *,
        force: bool = False,
    ) -> AppointmentListItem:
        appt = self.repo.get_by_id(appointment_id)
        if not appt:
            raise AppointmentNotFoundError()

        ok, blockers = complete_appointment_record(self.db, self.hospital_id, appt, force=force)
        if not ok:
            raise AppointmentValidationError(f"Cannot complete appointment: {'; '.join(blockers)}")

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="update",
            entity_type="appointment",
            entity_id=appt.id,
            summary=f"Marked appointment {appt.id} Completed",
        )
        self.db.commit()
        self.db.refresh(appt)

        items = self.repo.hydrate_appointment_items([appt])
        return items[0]


class CancelAppointmentAction:
    """Cancel an appointment and record cancellation reason in notes."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo

    def execute(
        self,
        appointment_id: UUID,
        reason: str | None,
        user: dict[str, Any],
    ) -> AppointmentListItem:
        appt = self.repo.get_by_id(appointment_id)
        if not appt:
            raise AppointmentNotFoundError()

        if appt.status == AppointmentStatus.completed:
            raise AppointmentValidationError("Cannot cancel an already completed appointment")

        appt.status = AppointmentStatus.cancelled
        if reason:
            note = f"Cancellation Reason: {reason.strip()}"
            existing = (appt.notes or "").strip()
            appt.notes = f"{existing}\n{note}".strip() if existing else note

        from modules.billing.entities.billing_entities import BillingSourceType
        from modules.billing.services.billing_service import cancel_charge_for_source
        cancel_charge_for_source(
            self.db,
            hospital_id=self.hospital_id,
            source_type=BillingSourceType.consultation,
            source_id=appt.id,
        )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="update",
            entity_type="appointment",
            entity_id=appt.id,
            summary=f"Cancelled appointment {appt.id}",
        )
        self.db.commit()
        self.db.refresh(appt)

        items = self.repo.hydrate_appointment_items([appt])
        return items[0]


class NoShowAction:
    """Mark an appointment as No Show."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo

    def execute(self, appointment_id: UUID, user: dict[str, Any]) -> AppointmentListItem:
        appt = self.repo.get_by_id(appointment_id)
        if not appt:
            raise AppointmentNotFoundError()

        if appt.status in {AppointmentStatus.completed, AppointmentStatus.cancelled}:
            raise AppointmentValidationError(
                f"Cannot mark {appt.status.value} appointment as no show"
            )

        appt.status = AppointmentStatus.no_show
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="update",
            entity_type="appointment",
            entity_id=appt.id,
            summary=f"Marked appointment {appt.id} as No Show",
        )
        self.db.commit()
        self.db.refresh(appt)

        items = self.repo.hydrate_appointment_items([appt])
        return items[0]


class RescheduleAction:
    """Reschedule an existing appointment to a new date/time slot."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
        avail_reader: AvailabilityReader,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo
        self.avail_reader = avail_reader

    def execute(
        self,
        appointment_id: UUID,
        payload: RescheduleRequest,
        user: dict[str, Any],
    ) -> AppointmentListItem:
        appt = self.repo.get_by_id(appointment_id)
        if not appt:
            raise AppointmentNotFoundError()

        AppointmentsValidator.assert_can_reschedule(appt)
        AppointmentsValidator.assert_not_past_slot(
            payload.appointment_date, payload.appointment_time
        )

        holiday = self.avail_reader.get_holiday(payload.appointment_date)
        if holiday:
            raise AppointmentValidationError(
                f"Hospital is closed on {payload.appointment_date} ({holiday['name']})"
            )

        if self.avail_reader.has_slot_conflict(
            appt.doctor_id, payload.appointment_date, payload.appointment_time, exclude_id=appt.id
        ):
            raise AppointmentConflictError("Doctor already has an appointment at this time")

        old_dt = f"{appt.appointment_date} {appt.appointment_time}"
        appt.appointment_date = payload.appointment_date
        appt.appointment_time = payload.appointment_time
        appt.status = AppointmentStatus.scheduled  # reset to scheduled upon reschedule
        appt.checked_in_at = None
        appt.queue_token = None

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="update",
            entity_type="appointment",
            entity_id=appt.id,
            summary=f"Rescheduled appointment {appt.id} from {old_dt} to {payload.appointment_date} {payload.appointment_time}",
        )
        self.db.commit()
        self.db.refresh(appt)

        items = self.repo.hydrate_appointment_items([appt])
        return items[0]


class AssignNurseAction:
    """Assign or unassign nurse to an appointment."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo

    def execute(
        self,
        appointment_id: UUID,
        payload: AssignNurseRequest,
        user: dict[str, Any],
    ) -> AppointmentListItem:
        appt = self.repo.get_by_id(appointment_id)
        if not appt:
            raise AppointmentNotFoundError()

        appt.nurse_id = payload.nurse_id
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="update",
            entity_type="appointment",
            entity_id=appt.id,
            summary=f"Assigned nurse {payload.nurse_id} to appointment {appt.id}",
        )
        self.db.commit()
        self.db.refresh(appt)

        items = self.repo.hydrate_appointment_items([appt])
        return items[0]


class AdmitIpdAction:
    """Admit patient to Inpatient ward/bed directly from appointment."""

    def __init__(
        self,
        db: Session,
        hospital_id: UUID,
        repo: AppointmentsRepository,
    ) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = repo

    def execute(
        self,
        appointment_id: UUID,
        payload: AdmitIpdRequest,
        user: dict[str, Any],
    ) -> dict[str, Any]:
        from sqlalchemy.exc import IntegrityError

        appt = self.repo.get_by_id(appointment_id)
        if not appt:
            raise AppointmentNotFoundError()

        from modules.patients.entities.patient import Patient
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == appt.patient_id, Patient.hospital_id == self.hospital_id)
            .first()
        )
        if not patient:
            raise AppointmentNotFoundError("Patient not found")

        from modules.inpatient.entities.admission import Admission, AdmissionStatus
        from modules.patients.entities.patient import PatientStatus as _PatientStatus
        # Invariant 1: requested/admitted/discharge_requested all block a new
        # episode. A pre-existing REQUESTED admission (form finalize or
        # Transfer button) is accepted with this bed instead (Invariant 11).
        from modules.inpatient.utils.admission_conflicts import (
            admission_conflict as _ipd_conflict,
            bed_conflict as _ipd_bed_conflict,
        )

        existing = (
            self.db.query(Admission)
            .filter(
                Admission.patient_id == appt.patient_id,
                Admission.hospital_id == self.hospital_id,
                Admission.status.in_(
                    [
                        AdmissionStatus.requested,
                        AdmissionStatus.admitted,
                        AdmissionStatus.discharge_requested,
                    ]
                ),
            )
            .with_for_update()
            .first()
        )
        if existing and existing.status != AdmissionStatus.requested:
            raise _ipd_conflict(
                "Patient is already admitted to another bed",
                admission_id=existing.id,
                admission_status=existing.status,
                bed_id=existing.bed_id,
                doctor_id=existing.doctor_id,
            )
        if existing and existing.source_appointment_id not in (None, appt.id):
            # Request belongs to a different visit; still a single open episode.
            raise _ipd_conflict(
                "Patient already has an admission request",
                admission_id=existing.id,
                admission_status=existing.status,
                doctor_id=existing.doctor_id,
            )

        from modules.beds.entities.bed import Bed
        # Row-level lock on the target bed to prevent concurrent double-booking
        bed = (
            self.db.query(Bed)
            .filter(
                Bed.id == payload.bed_id,
                Bed.hospital_id == self.hospital_id,
                Bed.room_id == payload.room_id,
                Bed.ward_id == payload.ward_id,
            )
            .with_for_update()
            .first()
        )
        if not bed:
            raise AppointmentNotFoundError("Selected bed not found")
        if bed.is_occupied:
            raise _ipd_bed_conflict(
                "Selected bed is already occupied or was just taken",
                bed_id=bed.id,
                admission_id=existing.id if existing is not None else None,
                admission_status=existing.status if existing is not None else None,
            )

        from modules.inpatient.db.admissions_repository import AdmissionsRepository
        from modules.inpatient.services.inpatient_billing_service import (
            InpatientBillingService,
            next_ip_encounter_id,
        )

        billing_svc = InpatientBillingService(self.db)
        actor_name = str(user.get("name") or "Nurse")

        try:
            if existing is not None:
                # Converge: accept the canonical request with this bed.
                from datetime import datetime as _dt
                from datetime import timezone as _tz

                now = _dt.now(_tz.utc)
                existing.status = AdmissionStatus.admitted
                existing.admitted_at = now
                existing.ward_id = payload.ward_id
                existing.room_id = payload.room_id
                existing.bed_id = bed.id
                if not existing.ip_id:
                    existing.ip_id = next_ip_encounter_id(self.db, self.hospital_id)
                if not existing.source_appointment_id:
                    existing.source_appointment_id = appt.id
                bed.is_occupied = True
                patient.status = _PatientStatus.admitted
                from modules.inpatient.entities.admission import BedStaySegment

                self.db.add(
                    BedStaySegment(
                        hospital_id=self.hospital_id,
                        admission_id=existing.id,
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
                admission = existing
                ip_id = existing.ip_id
            else:
                ip_id = next_ip_encounter_id(self.db, self.hospital_id)

                admissions_repo = AdmissionsRepository(self.db)
                admission = admissions_repo.create_admission(
                    hospital_id=self.hospital_id,
                    patient=patient,
                    bed=bed,
                    ward_id=payload.ward_id,
                    room_id=payload.room_id,
                    doctor_id=appt.doctor_id,
                    ip_id=ip_id,
                    notes=payload.notes,
                    source_appointment_id=appt.id,
                )
                # create_admission leaves history to the caller: open the stay
                # segment for this bed assignment (Invariant 7).
                from datetime import datetime as _dt2
                from datetime import timezone as _tz2
                from modules.inpatient.entities.admission import BedStaySegment as _Seg

                self.db.add(
                    _Seg(
                        hospital_id=self.hospital_id,
                        admission_id=admission.id,
                        ward_id=bed.ward_id,
                        room_id=bed.room_id,
                        bed_id=bed.id,
                        rate_per_day=float(getattr(bed.ward, "bed_charge_per_day", 0) or 0)
                        if bed.ward
                        else 0.0,
                        started_at=_dt2.now(_tz2.utc),
                        ended_at=None,
                    )
                )
        except IntegrityError as exc:
            self.db.rollback()
            raise _ipd_bed_conflict(
                "Admission was just created or bed just taken; please retry",
                bed_id=payload.bed_id,
            ) from exc

        billing_svc.ensure_financial_account(
            hospital_id=self.hospital_id,
            patient_id=patient.id,
            admission_id=admission.id,
            created_by_name=actor_name,
        )
        billing_svc.ensure_admission_charge(
            hospital_id=self.hospital_id,
            patient_id=patient.id,
            admission_id=admission.id,
            ward_name=bed.ward.name if bed.ward else None,
            admission_fee=float(getattr(bed.ward, "admission_fee", 0) or 0) if bed.ward else 0.0,
            created_by_name=actor_name,
        )

        consultant_id = getattr(payload, "doctor_id", None) or getattr(appt, "doctor_id", None)
        if consultant_id:
            from modules.inpatient.db.care_team_repository import CareTeamRepository
            from modules.inpatient.entities.care_team import AdmissionCareTeamRole
            user_id = user.get("id") or user.get("sub")
            CareTeamRepository(self.db, self.hospital_id).add_member(
                admission_id=admission.id,
                doctor_id=consultant_id,
                role=AdmissionCareTeamRole.primary_consultant,
                assigned_by_id=UUID(str(user_id)) if user_id else None,
                assigned_by_name=actor_name,
                notes="Primary consultant assigned upon transfer from OPD",
            )

        appt.status = AppointmentStatus.transferred_to_inpatient
        appt.admission_id = admission.id

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="create",
            entity_type="admission",
            entity_id=str(admission.id),
            summary=f"Admitted patient {patient.name} ({patient.uhid}) to Bed {bed.bed_code} ({ip_id}) from visit {appt.op_id or str(appt.id)}",
        )
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="update",
            entity_type="bed",
            entity_id=str(bed.id),
            summary=f"Bed {bed.bed_code} occupied by admission {admission.id}",
        )
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="update",
            entity_type="patient",
            entity_id=str(patient.id),
            summary="Patient admitted from appointment",
        )
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=user,
            action="update",
            entity_type="appointment",
            entity_id=str(appt.id),
            summary="Appointment transferred to inpatient",
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise _ipd_bed_conflict(
                "Admission was just created or bed just taken; please retry",
                bed_id=payload.bed_id,
                admission_id=admission.id,
                admission_status=admission.status,
            ) from exc
        return {
            "status": "admitted",
            "admission_id": str(admission.id),
            "ip_id": ip_id,
            "bed_code": bed.bed_code,
        }
