"""
Action to register a new patient.

Conforms to UltrionTech-Backend-Template modules/patients/actions/ specification.
Handles:
- Multi-tenant mobile uniqueness validation (HTTP 409)
- Sequential UHID generation
- Independent Patient entity persistence
- Audit trail recording via shared audit foundation
- Directory response serialization
"""

from typing import Any

from fastapi import HTTPException, status

from hms_migration.modules.patients.contracts.patients_contracts import (
    PatientDirectoryItem,
    PatientRegister,
)
from hms_migration.modules.patients.db.patients_repository import PatientRepository
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus
from hms_migration.modules.patients.validators.patients_validator import (
    calculate_age_from_dob,
    format_display_name,
)
from hms_migration.shared.audit import write_audit_log


class RegisterPatientAction:
    """Action orchestrating patient registration workflow."""

    def __init__(self, repo: PatientRepository) -> None:
        self.repo = repo

    def execute(
        self,
        payload: PatientRegister,
        user: dict[str, Any],
    ) -> PatientDirectoryItem:
        """Execute patient registration, enforcing uniqueness, UHID generation, and audit logging."""
        mobile = payload.mobile.strip()
        existing = self.repo.find_by_mobile(mobile)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Patient with this mobile already exists",
            )

        first = payload.first_name.strip()
        last = payload.last_name.strip()
        age = payload.age if payload.age is not None else calculate_age_from_dob(payload.date_of_birth)
        uhid = self.repo.generate_next_uhid()

        patient = Patient(
            hospital_id=self.repo.hospital_id,
            uhid=uhid,
            first_name=first,
            last_name=last,
            name=format_display_name(first, last),
            mobile=mobile,
            email=str(payload.email).lower() if payload.email else None,
            age=age,
            date_of_birth=payload.date_of_birth,
            gender=payload.gender.strip(),
            address=payload.address.strip() if payload.address else None,
            emergency_contact=payload.emergency_contact.strip() if payload.emergency_contact else None,
            emergency_contact_name=payload.emergency_contact_name,
            emergency_contact_relation=payload.emergency_contact_relation,
            blood_group=payload.blood_group,
            has_insurance=bool(payload.has_insurance),
            insurance_provider=payload.insurance_provider if payload.has_insurance else None,
            insurance_details=None,
            status=PatientStatus.active,
        )

        self.repo.add(patient)
        self.repo.flush()

        write_audit_log(
            self.repo.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="create",
            entity_type="patient",
            entity_id=patient.id,
            summary=f"Registered patient {patient.uhid} {patient.name}",
        )

        self.repo.commit()
        self.repo.refresh(patient)

        last_visit = self.repo.profile_reader.get_last_visit(patient.id)
        return PatientDirectoryItem(
            id=patient.id,
            uhid=patient.uhid,
            name=patient.name,
            first_name=patient.first_name or "",
            last_name=patient.last_name or "",
            mobile=patient.mobile,
            email=patient.email,
            gender=patient.gender,
            age=patient.age,
            date_of_birth=patient.date_of_birth,
            blood_group=patient.blood_group,
            status=patient.status,
            last_visit=last_visit,
            created_at=patient.created_at,
        )
