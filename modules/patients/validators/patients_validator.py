"""
Validation and normalization logic for the Patient domain.

Conforms to UltrionTech-Backend-Template modules/patients/validators/ specification.
Preserves exact OG HMS-B business validation rules for patient registration, updates,
emergency contact bundles, and age calculations.
"""

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, status

BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "Unknown"]

EMERGENCY_RELATIONS = ("Father", "Mother", "Spouse", "Sibling", "Child", "Friend", "Other")
EmergencyRelation = Literal["Father", "Mother", "Spouse", "Sibling", "Child", "Friend", "Other"]


def normalize_optional_text(value: str | None, *, max_len: int | None = None) -> str | None:
    """Strip leading/trailing whitespace, return None if empty, enforce max_len."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if max_len is not None and len(cleaned) > max_len:
        raise ValueError(f"must be at most {max_len} characters")
    return cleaned


def format_display_name(first: str, last: str) -> str:
    """Format full display name from first and last name components."""
    return f"{first.strip()} {last.strip()}".strip()


def calculate_age_from_dob(dob: date | None) -> int | None:
    """Calculate integer age from date of birth against today's date."""
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_emergency_contact_bundle(
    *,
    name: str | None,
    relation: str | None,
    phone: str | None,
    required: bool = True,
) -> None:
    """Validate emergency contact fields. When required=True, name, relation, and phone are mandatory."""
    name_n = normalize_optional_text(name)
    relation_n = normalize_optional_text(relation)
    phone_n = phone.strip() if isinstance(phone, str) and phone.strip() else phone
    if relation_n and relation_n not in EMERGENCY_RELATIONS:
        raise ValueError(f"emergency_contact_relation must be one of: {', '.join(EMERGENCY_RELATIONS)}")
    if required:
        missing = []
        if not name_n:
            missing.append("emergency_contact_name")
        if not relation_n:
            missing.append("emergency_contact_relation")
        if not phone_n:
            missing.append("emergency_contact")
        if missing:
            raise ValueError("Emergency contact name, relation, and phone are required")
        return
    if (name_n or relation_n or phone_n) and not phone_n:
        raise ValueError("emergency_contact phone is required when any emergency contact field is entered")


def validate_update_emergency_contact(
    *,
    name: str | None,
    relation: str | None,
    phone: str | None,
    patient_mobile: str | None,
) -> None:
    """Validate merged emergency contact fields on patient update, raising HTTP 422 on failure."""
    try:
        validate_emergency_contact_bundle(
            name=name,
            relation=relation,
            phone=phone,
            required=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if patient_mobile and phone and patient_mobile == phone:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Emergency contact number must be different from patient mobile number",
        )
