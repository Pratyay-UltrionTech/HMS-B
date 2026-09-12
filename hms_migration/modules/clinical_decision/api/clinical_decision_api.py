"""
FastAPI router for Clinical Decision Support and Medication Safety under /clinical.

Covers:
- Feature 19: Medication Reconciliation
- Feature 20: Clinical Alerts & Rule Engine
- Feature 21: Clinical Order Sets
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.clinical_decision.actions.clinical_decision_actions import ClinicalDecisionActions
from hms_migration.modules.clinical_decision.contracts.clinical_decision_contracts import (
    AcknowledgeAlertRequest,
    ApplyOrderSetRequest,
    ApplyOrderSetResult,
    ClinicalAlertResponse,
    ClinicalOrderSetCreate,
    ClinicalOrderSetResponse,
    ClinicalRuleCreate,
    ClinicalRuleResponse,
    EvaluateAlertsRequest,
    MedicationReconciliationCreate,
    MedicationReconciliationResponse,
    UpdateReconciliationItemsRequest,
)
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/clinical", tags=["Clinical Decision Support"])


# ---------------------------------------------------------------------------
# Feature 19: Medication Reconciliation
# ---------------------------------------------------------------------------

@router.post(
    "/medication-reconciliation",
    response_model=MedicationReconciliationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_medication_reconciliation(
    payload: MedicationReconciliationCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> MedicationReconciliationResponse:
    return ClinicalDecisionActions(db, hospital_id, actor).create_reconciliation(payload)


@router.get(
    "/medication-reconciliation/{rec_id}",
    response_model=MedicationReconciliationResponse,
)
def get_medication_reconciliation(
    rec_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> MedicationReconciliationResponse:
    return ClinicalDecisionActions(db, hospital_id, actor).get_reconciliation(rec_id)


@router.get(
    "/admissions/{admission_id}/medication-reconciliation",
    response_model=list[MedicationReconciliationResponse],
)
def list_reconciliations_for_admission(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> list[MedicationReconciliationResponse]:
    return ClinicalDecisionActions(db, hospital_id, actor).list_reconciliations_for_admission(admission_id)


@router.get(
    "/patients/{patient_id}/medication-reconciliation",
    response_model=list[MedicationReconciliationResponse],
)
def list_reconciliations_for_patient(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> list[MedicationReconciliationResponse]:
    return ClinicalDecisionActions(db, hospital_id, actor).list_reconciliations_for_patient(patient_id)


@router.put(
    "/medication-reconciliation/{rec_id}/items",
    response_model=MedicationReconciliationResponse,
)
def update_reconciliation_items(
    rec_id: UUID,
    payload: UpdateReconciliationItemsRequest,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> MedicationReconciliationResponse:
    return ClinicalDecisionActions(db, hospital_id, actor).update_reconciliation_items(rec_id, payload)


# ---------------------------------------------------------------------------
# Feature 20: Clinical Rules & Alerts
# ---------------------------------------------------------------------------

@router.post(
    "/rules",
    response_model=ClinicalRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_clinical_rule(
    payload: ClinicalRuleCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> ClinicalRuleResponse:
    return ClinicalDecisionActions(db, hospital_id, actor).create_rule(payload)


@router.get(
    "/rules",
    response_model=list[ClinicalRuleResponse],
)
def list_clinical_rules(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> list[ClinicalRuleResponse]:
    return ClinicalDecisionActions(db, hospital_id, actor).list_rules()


@router.post(
    "/alerts/evaluate/{patient_id}",
    response_model=list[ClinicalAlertResponse],
)
def evaluate_patient_clinical_alerts(
    patient_id: UUID,
    payload: EvaluateAlertsRequest | None = None,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> list[ClinicalAlertResponse]:
    return ClinicalDecisionActions(db, hospital_id, actor).evaluate_patient_alerts(patient_id, payload)


@router.get(
    "/alerts/patient/{patient_id}",
    response_model=list[ClinicalAlertResponse],
)
def list_patient_clinical_alerts(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> list[ClinicalAlertResponse]:
    return ClinicalDecisionActions(db, hospital_id, actor).evaluate_patient_alerts(patient_id, None)


@router.get(
    "/alerts",
    response_model=list[ClinicalAlertResponse],
)
def list_clinical_alerts(
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> list[ClinicalAlertResponse]:
    """Hospital-scoped alert feed used by the Clinical Decision workspace.

    Frontend calls GET /api/clinical/alerts[?patient_id=&status=].
    Previously missing, which surfaced as 404 spam in the console.
    """
    return ClinicalDecisionActions(db, hospital_id, actor).list_alerts(patient_id, status)


def _acknowledge(
    alert_id: UUID,
    payload: AcknowledgeAlertRequest | None,
    db: Session,
    hospital_id: UUID,
    actor: dict[str, Any],
) -> ClinicalAlertResponse:
    # Frontend sends PATCH with {"notes": "..."} and no status; accept empty body too.
    req = payload or AcknowledgeAlertRequest()
    return ClinicalDecisionActions(db, hospital_id, actor).acknowledge_alert(alert_id, req)


@router.put(
    "/alerts/{alert_id}/acknowledge",
    response_model=ClinicalAlertResponse,
)
def acknowledge_clinical_alert(
    alert_id: UUID,
    payload: AcknowledgeAlertRequest | None = None,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> ClinicalAlertResponse:
    return _acknowledge(alert_id, payload, db, hospital_id, actor)


@router.patch(
    "/alerts/{alert_id}/acknowledge",
    response_model=ClinicalAlertResponse,
)
def acknowledge_clinical_alert_patch(
    alert_id: UUID,
    payload: AcknowledgeAlertRequest | None = None,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> ClinicalAlertResponse:
    """PATCH alias — frontend uses PATCH with { notes }."""
    return _acknowledge(alert_id, payload, db, hospital_id, actor)


# ---------------------------------------------------------------------------
# Feature 21: Clinical Order Sets
# ---------------------------------------------------------------------------

@router.post(
    "/order-sets",
    response_model=ClinicalOrderSetResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_clinical_order_set(
    payload: ClinicalOrderSetCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> ClinicalOrderSetResponse:
    return ClinicalDecisionActions(db, hospital_id, actor).create_order_set(payload)


@router.get(
    "/order-sets",
    response_model=list[ClinicalOrderSetResponse],
)
def list_clinical_order_sets(
    category: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> list[ClinicalOrderSetResponse]:
    return ClinicalDecisionActions(db, hospital_id, actor).list_order_sets(category)


@router.get(
    "/order-sets/{set_id}",
    response_model=ClinicalOrderSetResponse,
)
def get_clinical_order_set(
    set_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> ClinicalOrderSetResponse:
    return ClinicalDecisionActions(db, hospital_id, actor).get_order_set(set_id)


@router.post(
    "/order-sets/{set_id}/apply",
    response_model=ApplyOrderSetResult,
)
def apply_clinical_order_set(
    set_id: UUID,
    payload: ApplyOrderSetRequest,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_user),
) -> ApplyOrderSetResult:
    return ClinicalDecisionActions(db, hospital_id, actor).apply_order_set(set_id, payload)
