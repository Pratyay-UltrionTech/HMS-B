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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import column, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.tenancy.entities.hospital import Hospital

logger = logging.getLogger("hms.appointments.lifecycle")

# IANA timezone used when a tenant has no explicit timezone configured.
DEFAULT_TIMEZONE = "Asia/Kolkata"

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
# Only unfulfilled doctor requests block completion. Once a lab order exists
# (partially_processed), the lab_orders check above is the source of truth —
# otherwise the same work blocks twice and the visit can never close.
_OPEN_LAB_REQUESTS = {"pending"}

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
    """Combine appointment date and time into a naive datetime.

    The appointment's date/time are interpreted in the tenant's configured
    timezone (FLAW-007), so a 10:00 AM appointment in an IST hospital is
    evaluated against IST wall-clock time regardless of the server's zone.
    """
    return datetime.combine(appt.appointment_date, appt.appointment_time)


def _tenant_tz(tz_name: str | None) -> ZoneInfo | None:
    """Resolve a tenant timezone to a ZoneInfo.

    Returns ``None`` when the zone is unknown OR the host has no IANA tz
    database installed (e.g. a bare Windows Python without the ``tzdata``
    package). A ``None`` result makes ``_tenant_local_now`` fall back to the
    server's local wall-clock, so the auto-cancel loop degrades gracefully
    instead of crashing.
    """
    try:
        return ZoneInfo(tz_name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def _tenant_local_now(tz: ZoneInfo | None, now: datetime | None) -> datetime:
    """Return the given instant expressed in the tenant's wall-clock time.

    When ``tz`` is None (no usable tz database), the server's local time is used
    (naive), preserving the pre-FLAW-007 fallback behavior on hosts without tzdata.
    """
    if tz is None:
        if now is not None and now.tzinfo is not None:
            return now.replace(tzinfo=None)
        return now or datetime.now()
    if now is not None and now.tzinfo is not None:
        return now.astimezone(tz)
    return datetime.now(tz)


def _appointment_tz_ids(db: Session, hospital_id: UUID | None) -> dict[UUID, ZoneInfo]:
    """Map hospital id -> resolved timezone.

    When ``hospital_id`` is provided, only that tenant is resolved. Otherwise
    every hospital is resolved so the auto-cancel loop can partition by tenant
    timezone instead of using a single server-local clock for all tenants.
    """
    q = db.query(Hospital.id, Hospital.timezone)
    if hospital_id is not None:
        q = q.filter(Hospital.id == hospital_id)
    return {row.id: _tenant_tz(row.timezone) for row in q.all()}


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

    Timezone-aware (FLAW-007): the loop partitions by tenant, resolves each
    hospital's configured timezone, and compares each appointment's scheduled
    date/time (interpreted in that tenant's local time) against the tenant's
    local wall-clock now. This prevents premature/late cancellation on UTC
    servers for non-UTC hospitals.
    """
    tz_by_hospital = _appointment_tz_ids(db, hospital_id)

    q = db.query(Appointment).filter(Appointment.status == AppointmentStatus.scheduled)
    if hospital_id is not None:
        q = q.filter(Appointment.hospital_id == hospital_id)

    cancelled = 0
    note = (
        f"Auto-cancelled: patient did not check in within {grace_minutes} minutes of the scheduled time."
    )
    for appt in q.all():
        tz = tz_by_hospital.get(appt.hospital_id)
        local_now = _tenant_local_now(tz, now)
        # Compare wall-clock times: appointment_local_dt returns a naive datetime,
        # so use the tenant-local wall clock (tzinfo stripped) for the cutoff.
        local_now_naive = local_now.replace(tzinfo=None)
        cutoff = local_now_naive - timedelta(minutes=grace_minutes)
        # Only consider appointments due on or before the tenant-local today.
        if appt.appointment_date > local_now.date():
            continue
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
    *,
    force: bool = False,
) -> tuple[bool, list[str]]:
    """Determine whether visit can transition to completed."""
    if appt.status in TERMINAL:
        return False, [f"Appointment is already {status_display_label(appt.status)}"]
    if appt.status not in {AppointmentStatus.scheduled, IN_PROGRESS}:
        return False, [f"Cannot complete from status {status_display_label(appt.status)}"]
    if not force:
        blockers = get_open_clinical_blockers(db, hospital_id, appt.id)
        if blockers:
            return False, blockers
    return True, []


def complete_appointment_record(
    db: Session,
    hospital_id: UUID,
    appt: Appointment,
    *,
    force: bool = False,
) -> tuple[bool, list[str]]:
    """Mark appointment Completed if clinical dependencies are clear (or forced)."""
    ok, blockers = can_complete_appointment(db, hospital_id, appt, force=force)
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
