"""
Pydantic contracts for Drug Interaction Rules & Evaluation (Feature 17).

Conforms to UltrionTech-Backend-Template modules/pharmacy/contracts/ specification.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.pharmacy.entities.drug_interaction_entities import InteractionSeverity


class DrugInteractionRuleCreate(BaseModel):
    drug_a: str = Field(min_length=2, max_length=128)
    drug_b: str = Field(min_length=2, max_length=128)
    severity: InteractionSeverity = InteractionSeverity.major
    mechanism: str = Field(min_length=5)
    clinical_recommendation: str = Field(min_length=5)


class DrugInteractionRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    drug_a: str
    drug_b: str
    severity: InteractionSeverity
    mechanism: str
    clinical_recommendation: str
    is_active: bool
    created_at: datetime


class CheckDrugInteractionsRequest(BaseModel):
    medicines: list[str] = Field(min_length=2)


class DrugInteractionAlert(BaseModel):
    drug_a: str
    drug_b: str
    severity: InteractionSeverity
    mechanism: str
    clinical_recommendation: str


class DrugInteractionReport(BaseModel):
    has_conflicts: bool
    alerts: list[DrugInteractionAlert]
    highest_severity: InteractionSeverity | None = None
    requires_clinical_override: bool = False
