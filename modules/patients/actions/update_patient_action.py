"""
Action to update an existing patient record.

Conforms to UltrionTech-Backend-Template modules/patients/actions/ specification.
Handles:
- Tenant-scoped patient lookup (HTTP 404 if not found)
- Cross-patient mobile collision validation (HTTP 409)
- Partial field merging and display name recalculation
- Post-merge emergency contact bundle validation (HTTP 422)
- Mobile vs emergency contact clash validation (HTTP 422)
- Audit trail recording via shared audit foundation
- Directory response serialization
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from modules.patients.contracts.patients_contracts import (
    PatientDirectoryItem,
    PatientRegisterUpdate,
)
from modules.patients.db.patients_repository import PatientRepository
from modules.patients.validators.patients_validator import (
    calculate_age_from_dob,
    format_display_name,
    validate_update_emergency_contact,
)
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from shared.audit import write_audit_log


class UpdatePatientAction:
    """Action orchestrating patient update workflow."""

    def __init__(self, repo: PatientRepository) -> None:
        self.repo = repo

    def execute(
        self,
        patient_id: UUID,
        payload: PatientRegisterUpdate,
        user: dict[str, Any],
    ) -> PatientDirectoryItem:
        """Execute patient update with collision check, post-merge validation, and audit trail."""
        patient = self.repo.get_by_id(patient_id)
        if not patient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient not found",
            )

        data = payload.model_dump(exclude_unset=True)

        # FLAW-010: capture the demographic identity before this update so we can
        # detect corrections and propagate them to ACTIVE encounters' snapshots
        # (audit-logged) without rewriting discharged / historical encounters.
        before_demo = {
            "name": patient.name,
            "gender": patient.gender,
            "age": patient.age,
            "date_of_birth": patient.date_of_birth,
        }

        if "mobile" in data and data["mobile"]:
            mobile = data["mobile"].strip()
            clash = self.repo.find_by_mobile(mobile, exclude_id=patient.id)
            if clash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Mobile already used by another patient",
                )
            patient.mobile = mobile

        if "first_name" in data and data["first_name"]:
            patient.first_name = data["first_name"].strip()
        if "last_name" in data and data["last_name"]:
            patient.last_name = data["last_name"].strip()
        if "first_name" in data or "last_name" in data:
            patient.name = format_display_name(patient.first_name or "", patient.last_name or "")

        if "gender" in data and data["gender"]:
            patient.gender = data["gender"].strip()

        if "date_of_birth" in data:
            patient.date_of_birth = data["date_of_birth"]
            if data.get("age") is None and data["date_of_birth"]:
                patient.age = calculate_age_from_dob(data["date_of_birth"])

        if "age" in data and data["age"] is not None:
            patient.age = data["age"]

        if "email" in data:
            patient.email = str(data["email"]).lower() if data["email"] else None

        if "address" in data:
            patient.address = data["address"].strip() if data["address"] else None

        if "emergency_contact" in data:
            patient.emergency_contact = (
                data["emergency_contact"].strip() if data["emergency_contact"] else None
            )

        if "emergency_contact_name" in data:
            patient.emergency_contact_name = data["emergency_contact_name"]

        if "emergency_contact_relation" in data:
            patient.emergency_contact_relation = data["emergency_contact_relation"]

        if "blood_group" in data:
            patient.blood_group = data["blood_group"]

        if "has_insurance" in data and data["has_insurance"] is not None:
            patient.has_insurance = bool(data["has_insurance"])
            if not patient.has_insurance:
                patient.insurance_provider = None

        if "insurance_provider" in data:
            if getattr(patient, "has_insurance", False):
                patient.insurance_provider = data["insurance_provider"]
            else:
                patient.insurance_provider = None

        if "status" in data and data["status"] is not None:
            patient.status = data["status"]

        validate_update_emergency_contact(
            name=patient.emergency_contact_name,
            relation=patient.emergency_contact_relation,
            phone=patient.emergency_contact,
            patient_mobile=patient.mobile,
        )

        write_audit_log(
            self.repo.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="update",
            entity_type="patient",
            entity_id=patient.id,
            summary=f"Updated patient {patient.uhid} {patient.name}",
        )

        self._propagate_demographic_amendment(patient, before_demo, user)

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

    def _propagate_demographic_amendment(
        self,
        patient: Any,
        before: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        """FLAW-010 demographic amendment propagation.

        When the canonical Patient identity (name / gender / age) changes, the
        immutable snapshots of ACTIVE (non-discharged) admissions are refreshed
        so wristbands / worklists reflect the corrected identity, and a dedicated
        ``amend_demographics`` audit entry records the before/after values so
        nursing can reprint labels. Discharged / historical admissions keep the
        snapshot they were frozen with at admission/discharge time.
        """
        after = {
            "name": patient.name,
            "gender": patient.gender,
            "age": patient.age,
            "date_of_birth": patient.date_of_birth,
        }
        changed = {
            k: (before.get(k), after.get(k))
            for k in ("name", "gender", "age", "date_of_birth")
            if before.get(k) != after.get(k)
        }
        if not changed:
            return

        active = (
            self.repo.db.query(Admission)
            .filter(
                Admission.patient_id == patient.id,
                Admission.hospital_id == self.repo.hospital_id,
                Admission.status.in_(
                    [AdmissionStatus.admitted, AdmissionStatus.discharge_requested]
                ),
            )
            .all()
        )
        for adm in active:
            adm.patient_name = patient.name
            adm.gender = patient.gender
            adm.age_at_admission = patient.age

        changed_desc = "; ".join(
            f"{k}: {_fmt(old)} -> {_fmt(new)}" for k, (old, new) in changed.items()
        )
        write_audit_log(
            self.repo.db,
            hospital_id=self.repo.hospital_id,
            actor=user,
            action="amend_demographics",
            entity_type="patient",
            entity_id=patient.id,
            summary=(
                f"Demographics amended for {patient.uhid} {patient.name} "
                f"({changed_desc}); refreshed {len(active)} active admission snapshot(s); "
                "reprint wristbands/labels"
            ),
        )


def _fmt(value: Any) -> str:
    if value is None:
        return "<none>"
    return str(value)
