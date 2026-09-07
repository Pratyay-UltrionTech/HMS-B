"""
MIS reporting service helper functions.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.beds.entities.bed import Ward
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from hms_migration.modules.doctors.entities.doctor import HospitalUser


def day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def day_end(d: date) -> datetime:
    return datetime.combine(d, time.max, tzinfo=timezone.utc)


def filters_dict(
    date_from: date | None,
    date_to: date | None,
    department_id: UUID | None,
    doctor_id: UUID | None,
    patient_id: UUID | None,
    status: str | None,
) -> dict[str, Any]:
    return {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "department_id": str(department_id) if department_id else None,
        "doctor_id": str(doctor_id) if doctor_id else None,
        "patient_id": str(patient_id) if patient_id else None,
        "status": status,
    }


def is_doctor(u: HospitalUser) -> bool:
    return bool(u.role and "doctor" in (u.role.name or "").lower())


def consultation_fee(doctor: HospitalUser) -> float:
    cv = doctor.custom_values or {}
    for key in ("consultation_fee", "consultationFee", "fee", "consult_fee", "Consultation Fee"):
        val = cv.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return 0.0


def sum_billing_charges(
    db: Session,
    hospital_id: UUID,
    *,
    date_from: date,
    date_to: date,
    doctor_id: UUID | None = None,
) -> float:
    """Sum non-cancelled billing charge net amounts for the period."""
    q = db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0)).filter(
        BillingCharge.hospital_id == hospital_id,
        BillingCharge.status != BillingChargeStatus.cancelled,
        BillingCharge.created_at >= day_start(date_from),
        BillingCharge.created_at <= day_end(date_to),
    )
    if doctor_id is not None:
        appt_ids = [
            r[0]
            for r in db.query(Appointment.id)
            .filter(
                Appointment.hospital_id == hospital_id,
                Appointment.doctor_id == doctor_id,
                Appointment.appointment_date >= date_from,
                Appointment.appointment_date <= date_to,
            )
            .all()
        ]
        if not appt_ids:
            return 0.0
        q = q.filter(
            BillingCharge.source_type == BillingSourceType.consultation,
            BillingCharge.source_id.in_(appt_ids),
        )
    return float(q.scalar() or 0.0)


def bulk_doctor_consultation_billing_revenue(
    db: Session,
    hospital_id: UUID,
    doctor_ids: list[UUID],
    date_from: date,
    date_to: date,
) -> dict[UUID, float]:
    """Consultation charge totals keyed by doctor_id in one grouped query."""
    if not doctor_ids:
        return {}
    rows = (
        db.query(Appointment.doctor_id, func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .join(
            BillingCharge,
            (BillingCharge.source_id == Appointment.id)
            & (BillingCharge.hospital_id == hospital_id)
            & (BillingCharge.status != BillingChargeStatus.cancelled)
            & (BillingCharge.source_type == BillingSourceType.consultation),
        )
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.doctor_id.in_(doctor_ids),
            Appointment.appointment_date >= date_from,
            Appointment.appointment_date <= date_to,
        )
        .group_by(Appointment.doctor_id)
        .all()
    )
    return {r[0]: float(r[1] or 0.0) for r in rows}


def ward_ids_for_department(db: Session, hospital_id: UUID, department_id: UUID | None) -> list[UUID] | None:
    if not department_id:
        return None
    rows = (
        db.query(Ward.id)
        .filter(Ward.hospital_id == hospital_id, Ward.department_id == department_id, Ward.is_active.is_(True))
        .all()
    )
    return [r[0] for r in rows]
