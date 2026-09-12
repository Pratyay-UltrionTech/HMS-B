"""
Pydantic contracts for Patient Allergy domain and safety interception alerts.

Conforms to UltrionTech-Backend-Template modules/patients/contracts/ specification.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.patients.entities.allergy import AllergenType, AllergySeverity


class PatientAllergyCreate(BaseModel):
    allergen: str = Field(min_length=2, max_length=128)
    allergen_type: AllergenType = AllergenType.drug
    severity: AllergySeverity = AllergySeverity.moderate
    reaction: str | None = Field(default=None, max_length=255)
    diagnosed_date: date | None = None
    notes: str | None = None


class PatientAllergyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    patient_id: UUID
    allergen: str
    allergen_type: AllergenType
    severity: AllergySeverity
    reaction: str | None
    diagnosed_date: date | None
    is_active: bool
    recorded_by_name: str
    notes: str | None
    created_at: datetime
    updated_at: datetime


class CheckAllergyAlertRequest(BaseModel):
    medicine_name: str = Field(min_length=2)
    generic_name: str | None = None


class AllergyAlertWarning(BaseModel):
    has_conflict: bool
    severity: AllergySeverity | None = None
    matched_allergen: str | None = None
    reaction: str | None = None
    message: str | None = None
    requires_clinical_override: bool = False
