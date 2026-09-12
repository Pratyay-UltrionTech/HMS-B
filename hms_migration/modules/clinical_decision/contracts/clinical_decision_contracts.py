"""
Pydantic contracts for Clinical Decision Support and Medication Safety.

Covers Features 19, 20, and 21 request/response schemas.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hms_migration.modules.clinical_decision.entities.clinical_decision_entities import (
    ClinicalAlertSeverity,
    ClinicalAlertStatus,
    ClinicalOrderType,
    ClinicalRuleCategory,
    MedicationDiscrepancyType,
    MedicationReconciliationAction,
    MedicationReconciliationSource,
    MedicationReconciliationStage,
    MedicationReconciliationStatus,
)


# ---------------------------------------------------------------------------
# Feature 19 Contracts: Medication Reconciliation
# ---------------------------------------------------------------------------

class ReconciliationItemInput(BaseModel):
    medicine_name: str = Field(..., min_length=1, max_length=255)
    dosage: str = Field(..., min_length=1, max_length=128)
    frequency: str = Field(..., min_length=1, max_length=64)
    route: str = Field(default="oral", max_length=64)
    source: MedicationReconciliationSource = MedicationReconciliationSource.home_medication
    action: MedicationReconciliationAction = MedicationReconciliationAction.continue_med
    discrepancy_type: MedicationDiscrepancyType = MedicationDiscrepancyType.none
    discrepancy_reason: str | None = None
    clinical_override_justification: str | None = None
    resolved: bool = False


class MedicationReconciliationCreate(BaseModel):
    patient_id: UUID
    admission_id: UUID | None = None
    stage: MedicationReconciliationStage = MedicationReconciliationStage.admission
    notes: str | None = None
    items: list[ReconciliationItemInput] = []


class ReconciliationItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reconciliation_id: UUID
    medicine_name: str
    dosage: str
    frequency: str
    route: str
    source: MedicationReconciliationSource
    action: MedicationReconciliationAction
    discrepancy_type: MedicationDiscrepancyType
    discrepancy_reason: str | None
    clinical_override_justification: str | None
    resolved: bool
    created_at: datetime


class MedicationReconciliationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    patient_id: UUID
    admission_id: UUID | None
    stage: MedicationReconciliationStage
    status: MedicationReconciliationStatus
    reconciled_by_id: UUID | None
    reconciled_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    items: list[ReconciliationItemResponse] = []


class UpdateReconciliationItemsRequest(BaseModel):
    items: list[ReconciliationItemInput]
    mark_completed: bool = False
    reconciliation_notes: str | None = None


# ---------------------------------------------------------------------------
# Feature 20 Contracts: Clinical Alerts
# ---------------------------------------------------------------------------

class ClinicalRuleCreate(BaseModel):
    rule_code: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=2, max_length=255)
    category: ClinicalRuleCategory = ClinicalRuleCategory.vitals_alert
    severity: ClinicalAlertSeverity = ClinicalAlertSeverity.warning
    rule_definition: dict[str, Any] = Field(
        ...,
        description="Structured multi-parameter conditions e.g. {'logic': 'AND', 'conditions': [{'parameter_type': 'vital', 'name': 'heart_rate', 'operator': 'gte', 'value': 120}]}",
    )
    recommendation: str | None = None
    is_active: bool = True


class ClinicalRuleUpdate(BaseModel):
    name: str | None = None
    category: ClinicalRuleCategory | None = None
    severity: ClinicalAlertSeverity | None = None
    rule_definition: dict[str, Any] | None = None
    recommendation: str | None = None
    is_active: bool | None = None


class ClinicalRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    rule_code: str
    name: str
    category: ClinicalRuleCategory
    severity: ClinicalAlertSeverity
    rule_definition: dict[str, Any]
    recommendation: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class EvaluateAlertsRequest(BaseModel):
    admission_id: UUID | None = None
    additional_observations: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional live observation overrides for point-of-care check e.g. {'heart_rate': 130, 'temperature': 102.1}",
    )


class ClinicalAlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    patient_id: UUID
    admission_id: UUID | None
    rule_id: UUID | None
    severity: ClinicalAlertSeverity
    title: str
    message: str
    status: ClinicalAlertStatus
    acknowledged_by_id: UUID | None
    acknowledged_at: datetime | None
    clinical_override_reason: str | None
    triggered_at: datetime
    created_at: datetime
    updated_at: datetime


class AcknowledgeAlertRequest(BaseModel):
    status: ClinicalAlertStatus = ClinicalAlertStatus.acknowledged
    clinical_override_reason: str | None = None
    # Frontend alias: ClinicalDecisionWorkspace sends { notes }
    notes: str | None = None


# ---------------------------------------------------------------------------
# Feature 21 Contracts: Clinical Order Sets
# ---------------------------------------------------------------------------

class ClinicalOrderSetItemInput(BaseModel):
    order_type: ClinicalOrderType
    lab_test_catalog_id: UUID | None = None
    radiology_scan_catalog_id: UUID | None = None
    medicine_id: UUID | None = None
    item_name: str = Field(..., min_length=1, max_length=255)
    dosage_or_instruction: str | None = None
    frequency: str | None = None
    instructions: str | None = None
    is_mandatory: bool = True
    sort_order: int = 0


class ClinicalOrderSetCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=2, max_length=255)
    category: str = Field(default="General", max_length=128)
    description: str | None = None
    department_id: UUID | None = None
    is_active: bool = True
    items: list[ClinicalOrderSetItemInput] = []


class ClinicalOrderSetItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_set_id: UUID
    order_type: ClinicalOrderType
    lab_test_catalog_id: UUID | None
    radiology_scan_catalog_id: UUID | None
    medicine_id: UUID | None
    item_name: str
    dosage_or_instruction: str | None
    frequency: str | None
    instructions: str | None
    is_mandatory: bool
    sort_order: int


class ClinicalOrderSetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hospital_id: UUID
    code: str
    name: str
    category: str
    description: str | None
    department_id: UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    items: list[ClinicalOrderSetItemResponse] = []


class ApplyOrderSetRequest(BaseModel):
    patient_id: UUID
    admission_id: UUID | None = None
    selected_item_ids: list[UUID] | None = None
    clinical_notes: str | None = None


class ApplyOrderSetResult(BaseModel):
    order_set_id: UUID
    order_set_name: str
    patient_id: UUID
    admission_id: UUID | None
    lab_order_id: UUID | None = None
    radiology_order_ids: list[UUID] = []
    prescription_id: UUID | None = None
    nursing_instructions_logged: int = 0
    message: str
