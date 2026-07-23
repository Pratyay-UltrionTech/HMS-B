"""Appointment lifecycle helpers: In Progress (waiting), completion deps, auto-sync."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import (
    Appointment,
    AppointmentStatus,
    LabOrder,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestStatus,
    Prescription,
    RadiologyOrder,
    RadiologyOrderStatus,
)

# DB value `waiting` = lifecycle "In Progress" (checked in / clinical work underway)
IN_PROGRESS = AppointmentStatus.waiting

TERMINAL = {
    AppointmentStatus.completed,
    AppointmentStatus.cancelled,
    AppointmentStatus.no_show,
}

OPEN_LAB = {
    LabOrderStatus.ordered,
    LabOrderStatus.sample_collected,
    LabOrderStatus.in_progress,
}

OPEN_RAD = {
    RadiologyOrderStatus.ordered,
    RadiologyOrderStatus.scheduled,
    RadiologyOrderStatus.in_progress,
}

OPEN_LAB_REQUESTS = {
    LabPrescriptionRequestStatus.pending,
    LabPrescriptionRequestStatus.partially_processed,
}


def status_display_label(status: AppointmentStatus | str | None) -> str:
    raw = status.value if isinstance(status, AppointmentStatus) else (status or "")
    return {
        "scheduled": "Scheduled",
        "waiting": "In Progress",
        "completed": "Completed",
        "cancelled": "Cancelled",
        "no_show": "No Show",
    }.get(raw, raw.replace("_", " ").title() or "Unknown")


def get_open_clinical_blockers(db: Session, hospital_id: UUID, appointment_id: UUID) -> list[str]:
    """Return human-readable blockers that prevent marking the visit Completed."""
    blockers: list[str] = []

    lab_rows = (
        db.query(LabOrder)
        .filter(
            LabOrder.hospital_id == hospital_id,
            LabOrder.appointment_id == appointment_id,
            LabOrder.status.in_(list(OPEN_LAB)),
        )
        .all()
    )
    for o in lab_rows:
        blockers.append(f"Lab order {o.order_no} is {o.status.value.replace('_', ' ')}")

    pending_reqs = (
        db.query(LabPrescriptionRequest)
        .filter(
            LabPrescriptionRequest.hospital_id == hospital_id,
            LabPrescriptionRequest.appointment_id == appointment_id,
            LabPrescriptionRequest.status.in_(list(OPEN_LAB_REQUESTS)),
        )
        .all()
    )
    for req in pending_reqs:
        blockers.append(f"Doctor lab request pending ({req.status.value.replace('_', ' ')})")

    rad_rows = (
        db.query(RadiologyOrder)
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.appointment_id == appointment_id,
            RadiologyOrder.status.in_(list(OPEN_RAD)),
        )
        .all()
    )
    for o in rad_rows:
        blockers.append(f"Radiology order {o.order_no} is {o.status.value.replace('_', ' ')}")

    return blockers


def has_prescription_for_appointment(db: Session, hospital_id: UUID, appointment_id: UUID) -> bool:
    return (
        db.query(Prescription.id)
        .filter(
            Prescription.hospital_id == hospital_id,
            Prescription.appointment_id == appointment_id,
        )
        .first()
        is not None
    )


def has_linked_orders(db: Session, hospital_id: UUID, appointment_id: UUID) -> bool:
    lab = (
        db.query(LabOrder.id)
        .filter(LabOrder.hospital_id == hospital_id, LabOrder.appointment_id == appointment_id)
        .first()
    )
    if lab:
        return True
    req = (
        db.query(LabPrescriptionRequest.id)
        .filter(
            LabPrescriptionRequest.hospital_id == hospital_id,
            LabPrescriptionRequest.appointment_id == appointment_id,
        )
        .first()
    )
    if req:
        return True
    rad = (
        db.query(RadiologyOrder.id)
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.appointment_id == appointment_id,
        )
        .first()
    )
    return rad is not None


def mark_in_progress(appt: Appointment, *, assign_checked_in: bool = True) -> bool:
    """Move Scheduled → In Progress (waiting). Returns True if status changed."""
    if appt.status != AppointmentStatus.scheduled:
        return False
    appt.status = IN_PROGRESS
    if assign_checked_in and not appt.checked_in_at:
        appt.checked_in_at = datetime.now(timezone.utc)
    return True


def can_complete_appointment(
    db: Session,
    hospital_id: UUID,
    appt: Appointment,
) -> tuple[bool, list[str]]:
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
    """Mark appointment Completed if lab/radiology dependencies are clear."""
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
    """
    Re-evaluate appointment after prescription / lab / radiology changes.

    Rules:
    - Open lab/rad → stay (or become) In Progress
    - No open lab/rad AND (has Rx OR had linked orders) → Completed
    """
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

    has_rx = has_prescription_for_appointment(db, hospital_id, appt.id)
    linked = has_linked_orders(db, hospital_id, appt.id)

    if has_rx or linked:
        complete_appointment_record(db, hospital_id, appt)

    return appt
