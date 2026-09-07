"""
Appointment lifecycle management service.

Conforms to UltrionTech-Backend-Template modules/appointments/services/ specification.
Manages visit state transitions (scheduled -> waiting -> completed / cancelled / no-show),
clinical dependency blockers, and auto-cancellation of overdue appointments.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from uuid import UUID

from sqlalchemy import column, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus

logger = logging.getLogger("hms.appointments.lifecycle")

IN_PROGRESS = AppointmentStatus.waiting
NO_SHOW_GRACE_MINUTES = 15

TERMINAL = {
    AppointmentStatus.completed,
    AppointmentStatus.transferred_to_inpatient,
    AppointmentStatus.cancelled,
    AppointmentStatus.no_show,
}

# Open status sets for diagnostics
_OPEN_LAB = {"ordered", "sample_collected", "in_progress"}
_OPEN_RAD = {"ordered", "scheduled", "in_progress"}
_OPEN_LAB_REQUESTS = {"pending", "partially_processed"}

# SQLAlchemy Core table references for clinical blocker checks
_lab_orders = table(
    "lab_orders",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("appointment_id", PG_UUID(as_uuid=True)),
    column("order_no", column("order_no").type),
    column("status", column("status").type),
)

_lab_prescription_requests = table(
    "lab_prescription_requests",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("appointment_id", PG_UUID(as_uuid=True)),
    column("status", column("status").type),
)

_radiology_orders = table(
    "radiology_orders",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("appointment_id", PG_UUID(as_uuid=True)),
    column("order_no", column("order_no").type),
    column("status", column("status").type),
)


def status_display_label(status: AppointmentStatus | str | None) -> str:
    """Return a human-readable display string for an appointment status."""
    raw = status.value if isinstance(status, AppointmentStatus) else (status or "")
    return {
        "scheduled": "Scheduled",
        "waiting": "Checked in",
        "completed": "Completed",
        "transferred_to_inpatient": "Transferred to Inpatient",
        "ipd_transfer_requested": "IPD requested",
        "cancelled": "Cancelled",
        "no_show": "No Show",
    }.get(raw, raw.replace("_", " ").title() or "Unknown")


def appointment_local_dt(appt: Appointment) -> datetime:
    """Combine appointment date and time into a naive datetime."""
    return datetime.combine(appt.appointment_date, appt.appointment_time)


def mark_in_progress(appt: Appointment, *, assign_checked_in: bool = True) -> bool:
    """
    Transition appointment from Scheduled to In Progress (waiting).
    Assigns checked_in_at timestamp if not present.
    Returns True if status changed, False if already in progress or terminal.
    """
    if appt.status != AppointmentStatus.scheduled:
        return False
    appt.status = IN_PROGRESS
    if assign_checked_in and not appt.checked_in_at:
        appt.checked_in_at = datetime.now(timezone.utc)
    return True


def get_open_clinical_blockers(db: Session, hospital_id: UUID, appointment_id: UUID) -> list[str]:
    """Return list of diagnostic orders that block completing the appointment."""
    blockers: list[str] = []
    try:
        # Check lab orders
        stmt_lab = select(_lab_orders.c.order_no, _lab_orders.c.status).where(
            _lab_orders.c.hospital_id == hospital_id,
            _lab_orders.c.appointment_id == appointment_id,
            _lab_orders.c.status.in_(_OPEN_LAB),
        )
        for row in db.execute(stmt_lab).all():
            st = str(row[1]).replace("_", " ") if row[1] else "open"
            blockers.append(f"Lab order {row[0]} is {st}")

        # Check lab prescription requests
        stmt_req = select(_lab_prescription_requests.c.status).where(
            _lab_prescription_requests.c.hospital_id == hospital_id,
            _lab_prescription_requests.c.appointment_id == appointment_id,
            _lab_prescription_requests.c.status.in_(_OPEN_LAB_REQUESTS),
        )
        for row in db.execute(stmt_req).all():
            st = str(row[0]).replace("_", " ") if row[0] else "pending"
            blockers.append(f"Doctor lab request pending ({st})")

        # Check radiology orders
        stmt_rad = select(_radiology_orders.c.order_no, _radiology_orders.c.status).where(
            _radiology_orders.c.hospital_id == hospital_id,
            _radiology_orders.c.appointment_id == appointment_id,
            _radiology_orders.c.status.in_(_OPEN_RAD),
        )
        for row in db.execute(stmt_rad).all():
            st = str(row[1]).replace("_", " ") if row[1] else "open"
            blockers.append(f"Radiology order {row[0]} is {st}")
    except Exception as exc:
        logger.debug("Diagnostics blocker inspection skipped or failed: %s", exc)

    return blockers


def can_complete_appointment(
    db: Session,
    hospital_id: UUID,
    appt: Appointment,
) -> tuple[bool, list[str]]:
    """Determine whether visit can transition to completed."""
    if appt.status in TERMINAL:
        return False, [f"Appointment is already {status_display_label(appt.status)}"]
    if appt.status not in {AppointmentStatus.scheduled, IN_PROGRESS}:
        return False, [f"Cannot complete from status {status_display_label(appt.status)}"]
    blockers = get_open_clinical_blockers(db, hospital_id, appt.id)
    if blockers:
        return False, blockers
    return True, []


def complete_appointment_record(
    db: Session,
    hospital_id: UUID,
    appt: Appointment,
) -> tuple[bool, list[str]]:
    """Mark appointment Completed if clinical dependencies are clear."""
    ok, blockers = can_complete_appointment(db, hospital_id, appt)
    if not ok:
        return False, blockers
    if appt.status == AppointmentStatus.scheduled:
        mark_in_progress(appt)
    appt.status = AppointmentStatus.completed
    return True, []


def sync_appointment_after_clinical_change(
    db: Session,
    hospital_id: UUID,
    appointment_id: UUID | None,
) -> Appointment | None:
    """Re-evaluate appointment status when associated lab/radiology orders change."""
    if not appointment_id:
        return None

    appt = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.hospital_id == hospital_id)
        .first()
    )
    if not appt or appt.status in TERMINAL:
        return appt

    blockers = get_open_clinical_blockers(db, hospital_id, appt.id)
    if blockers:
        mark_in_progress(appt)
    return appt


def auto_cancel_missed_appointments(
    db: Session,
    hospital_id: UUID | None = None,
    *,
    grace_minutes: int = NO_SHOW_GRACE_MINUTES,
    now: datetime | None = None,
) -> int:
    """
    Auto-cancel Scheduled appointments past scheduled time + grace period
    when patient has not checked in.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(minutes=grace_minutes)

    q = db.query(Appointment).filter(Appointment.status == AppointmentStatus.scheduled)
    if hospital_id is not None:
        q = q.filter(Appointment.hospital_id == hospital_id)
    q = q.filter(Appointment.appointment_date <= now.date())

    cancelled = 0
    note = (
        f"Auto-cancelled: patient did not check in within {grace_minutes} minutes of the scheduled time."
    )
    for appt in q.all():
        appt_dt = appointment_local_dt(appt)
        if appt_dt > cutoff:
            continue
        appt.status = AppointmentStatus.cancelled
        existing = (appt.notes or "").strip()
        if note not in existing:
            appt.notes = f"{existing}\n{note}".strip() if existing else note
        cancelled += 1

    if cancelled:
        db.commit()
        logger.info("Auto-cancelled %s missed appointment(s)", cancelled)
    return cancelled
