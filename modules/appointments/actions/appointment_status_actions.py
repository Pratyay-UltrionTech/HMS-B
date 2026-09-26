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
    """Admit patient to Inpatient ward/bed directly from appointment (Legacy adapter)."""

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
        appt = self.repo.get_by_id(appointment_id)
        if not appt:
            raise AppointmentNotFoundError()

        from modules.inpatient.actions.admission_lifecycle_actions import (
            EnsureAdmissionRequestAction,
            AcceptAdmissionAction,
        )
        from modules.inpatient.contracts.inpatient_contracts import AdmissionAcceptRequest
        from modules.beds.entities.bed import Bed

        # 1. Canonical admission request
        ensured, _ = EnsureAdmissionRequestAction(self.db).execute(
            hospital_id=self.hospital_id,
            patient_id=appt.patient_id,
            doctor_id=getattr(payload, "doctor_id", None) or appt.doctor_id,
            source_appointment_id=appt.id,
            notes=payload.notes,
            actor=user,
        )

        # 2. Canonical accept / bed allocation
        accepted = AcceptAdmissionAction(self.db).execute(
            hospital_id=self.hospital_id,
            actor=user,
            admission_id=ensured.id,
            ward_id=payload.ward_id,
            room_id=payload.room_id,
            bed_id=payload.bed_id,
        )

        bed = self.db.query(Bed).filter(Bed.id == payload.bed_id).first()
        bed_code = bed.bed_code if bed else ""

        return {
            "status": "admitted",
            "admission_id": str(accepted.id),
            "ip_id": accepted.ip_id,
            "bed_code": bed_code,
        }
