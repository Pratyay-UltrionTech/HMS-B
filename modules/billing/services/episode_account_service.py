"""Episode (FinancialAccount) resolution for new charges.

Every charge that belongs to an admission must land on that admission's IPD
FinancialAccount so the episode's deposits can fund it and discharge settles it.
Callers pass whatever episode context they know; this helper never guesses an
admission for an OPD order.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

ACTIVE_ADMISSION_STATUSES = ("admitted", "discharge_requested")


@dataclass(frozen=True)
class EpisodeAccount:
    account_id: UUID | None
    admission_id: UUID | None


NO_EPISODE = EpisodeAccount(account_id=None, admission_id=None)


def _single_active_admission_id(db: Session, hospital_id: UUID, patient_id: UUID) -> UUID | None:
    from modules.inpatient.entities.admission import Admission, AdmissionStatus

    rows = (
        db.query(Admission.id)
        .filter(
            Admission.hospital_id == hospital_id,
            Admission.patient_id == patient_id,
            Admission.status.in_([AdmissionStatus(s) for s in ACTIVE_ADMISSION_STATUSES]),
        )
        .limit(2)
        .all()
    )
    return rows[0][0] if len(rows) == 1 else None


def resolve_episode_admission_id(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    *,
    admission_id: UUID | None = None,
    appointment_id: UUID | None = None,
    infer_active_admission: bool = False,
) -> UUID | None:
    """Admission a new charge belongs to, or None for OPD / unscoped work.

    Precedence: explicit ``admission_id`` (validated: same hospital and patient, 404/400
    otherwise) → the admission an appointment was transferred into → when the caller
    declares an IPD context (``infer_active_admission``), the patient's single active
    admission. Ambiguity (0 or 2+ active admissions) resolves to None.
    """
    from modules.inpatient.entities.admission import Admission

    if admission_id is not None:
        admission = (
            db.query(Admission)
            .filter(Admission.id == admission_id, Admission.hospital_id == hospital_id)
            .first()
        )
        if admission is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")
        if admission.patient_id != patient_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Admission does not belong to the selected patient",
            )
        return admission.id

    if appointment_id is not None:
        from modules.appointments.entities.appointment import Appointment

        row = (
            db.query(Appointment.admission_id)
            .filter(
                Appointment.id == appointment_id,
                Appointment.hospital_id == hospital_id,
                Appointment.patient_id == patient_id,
            )
            .first()
        )
        if row and row[0] is not None:
            return row[0]

    if infer_active_admission:
        return _single_active_admission_id(db, hospital_id, patient_id)
    return None


def resolve_episode_account(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    admission_id: UUID | None = None,
    appointment_id: UUID | None = None,
    *,
    infer_active_admission: bool = False,
    created_by_name: str = "System",
) -> EpisodeAccount:
    """IPD FinancialAccount (created if missing) for the episode a new charge belongs to.

    Returns ``NO_EPISODE`` for OPD / unscoped work: OPD diagnostics keep
    ``account_id = NULL`` (unchanged behaviour). See ``resolve_episode_admission_id``.
    """
    from modules.billing.entities.billing_entities import FinancialAccountType
    from modules.billing.services.billing_service import get_or_create_financial_account

    resolved = resolve_episode_admission_id(
        db,
        hospital_id,
        patient_id,
        admission_id=admission_id,
        appointment_id=appointment_id,
        infer_active_admission=infer_active_admission,
    )
    if resolved is None:
        return NO_EPISODE
    account = get_or_create_financial_account(
        db,
        hospital_id=hospital_id,
        patient_id=patient_id,
        account_type=FinancialAccountType.ipd,
        admission_id=resolved,
        created_by_name=created_by_name,
    )
    return EpisodeAccount(account_id=account.id, admission_id=resolved)
