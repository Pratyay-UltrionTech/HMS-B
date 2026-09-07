"""
Authorization and doctor identity resolution helpers.

Conforms to UltrionTech-Backend-Template modules/doctors/permissions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.doctors.entities.doctor import HospitalUser


def is_doctor_role(name: str | None) -> bool:
    """Check whether a role name denotes a medical doctor."""
    return bool(name and "doctor" in name.lower())


def resolve_doctor_id(
    user: dict, doctor_id: UUID | None, hospital_id: UUID, db: Session
) -> UUID:
    """
    Staff may only access their own id. Admins may pick any doctor in the hospital.
    Preserves exact legacy error codes (401, 403, 400, 404).
    """
    if user.get("role") == "hospital_staff":
        try:
            own_id = UUID(str(user["user_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Staff identity missing"
            ) from exc
        if doctor_id and doctor_id != own_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only view your own records",
            )
        return own_id

    if not doctor_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="doctor_id is required"
        )
    doctor = (
        db.query(HospitalUser)
        .filter(HospitalUser.id == doctor_id, HospitalUser.hospital_id == hospital_id)
        .first()
    )
    if not doctor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found"
        )
    return doctor_id
