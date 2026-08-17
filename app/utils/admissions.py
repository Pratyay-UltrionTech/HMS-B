"""Shared IPD admission creation (ward/bed allotment + IP encounter ID)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Admission,
    AdmissionStatus,
    Bed,
    HospitalUser,
    Patient,
    PatientStatus,
)
from app.utils.encounter_ids import next_encounter_id


def create_admission(
    db: Session,
    *,
    hospital_id: UUID,
    patient_id: UUID,
    ward_id: UUID,
    room_id: UUID,
    bed_id: UUID,
    doctor_id: UUID | None,
    notes: str | None,
    created_by_name: str,
    admitted_at: datetime | None = None,
    source_appointment_id: UUID | None = None,
) -> Admission:
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    active = (
        db.query(Admission)
        .filter(
            Admission.patient_id == patient_id,
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.admitted,
        )
        .first()
    )
    if active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Patient is already admitted")

    bed = (
        db.query(Bed)
        .options(joinedload(Bed.ward), joinedload(Bed.room))
        .filter(Bed.id == bed_id, Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
        .first()
    )
    if not bed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bed not found")
    if bed.is_occupied:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bed is already occupied")
    if bed.ward_id != ward_id or bed.room_id != room_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ward/Room does not match selected bed",
        )

    if doctor_id:
        doctor = (
            db.query(HospitalUser)
            .filter(HospitalUser.id == doctor_id, HospitalUser.hospital_id == hospital_id)
            .first()
        )
        if not doctor:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

    admission = Admission(
        hospital_id=hospital_id,
        patient_id=patient_id,
        ward_id=ward_id,
        room_id=room_id,
        bed_id=bed_id,
        doctor_id=doctor_id,
        status=AdmissionStatus.admitted,
        notes=notes.strip() if notes else None,
        admitted_at=admitted_at or datetime.now(timezone.utc),
        ip_id=next_encounter_id(db, hospital_id, "IP"),
        source_appointment_id=source_appointment_id,
    )
    bed.is_occupied = True
    patient.status = PatientStatus.admitted
    db.add(admission)
    db.flush()

    from app.utils.billing import ensure_admission_charge

    ensure_admission_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient_id,
        admission_id=admission.id,
        ward_name=bed.ward.name if bed.ward else None,
        admission_fee=float(getattr(bed.ward, "admission_fee", 0) or 0) if bed.ward else 0.0,
        created_by_name=created_by_name,
    )
    return admission
