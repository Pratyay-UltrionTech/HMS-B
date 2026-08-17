"""Human-readable encounter numbers: OP-YYYY-NNNNN, IP-YYYY-NNNNN, ER-YYYY-NNNNN."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Admission, Appointment

EncounterKind = str  # "OP" | "IP" | "ER"


def next_encounter_id(
    db: Session,
    hospital_id: UUID,
    kind: EncounterKind,
    *,
    year: int | None = None,
) -> str:
    year = year or date.today().year
    prefix = f"{kind}-{year}-"
    current: str | None = None
    if kind == "OP":
        current = (
            db.query(func.max(Appointment.op_id))
            .filter(Appointment.hospital_id == hospital_id, Appointment.op_id.like(f"{prefix}%"))
            .scalar()
        )
    elif kind == "IP":
        current = (
            db.query(func.max(Admission.ip_id))
            .filter(Admission.hospital_id == hospital_id, Admission.ip_id.like(f"{prefix}%"))
            .scalar()
        )
    elif kind == "ER":
        current = (
            db.query(func.max(Admission.er_id))
            .filter(Admission.hospital_id == hospital_id, Admission.er_id.like(f"{prefix}%"))
            .scalar()
        )
    else:
        raise ValueError(f"Unknown encounter kind: {kind}")

    seq = 0
    if current:
        try:
            seq = int(str(current).rsplit("-", 1)[-1])
        except ValueError:
            seq = 0
    return f"{prefix}{seq + 1:05d}"
