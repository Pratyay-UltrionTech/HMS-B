"""
FastAPI router for Drug Interactions Engine (Feature 17) under /pharmacy.

Conforms to UltrionTech-Backend-Template modules/pharmacy/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.pharmacy.actions.drug_interaction_actions import (
    CheckDrugInteractionsAction,
    CreateDrugInteractionRuleAction,
    ListDrugInteractionRulesAction,
)
from hms_migration.modules.pharmacy.contracts.drug_interaction_contracts import (
    CheckDrugInteractionsRequest,
    DrugInteractionReport,
    DrugInteractionRuleCreate,
    DrugInteractionRuleResponse,
)
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/pharmacy", tags=["pharmacy", "drug-interactions"])


@router.post(
    "/interactions",
    response_model=DrugInteractionRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_interaction_rule(
    payload: DrugInteractionRuleCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Create a verified drug interaction rule between two compounds or drug classes."""
    return CreateDrugInteractionRuleAction(db, hospital_id).execute(payload, user)


@router.get(
    "/interactions",
    response_model=list[DrugInteractionRuleResponse],
)
def list_interaction_rules(
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """List active drug interaction rules configured for this hospital."""
    return ListDrugInteractionRulesAction(db, hospital_id).execute()


@router.post(
    "/check-interactions",
    response_model=DrugInteractionReport,
)
def check_drug_interactions(
    payload: CheckDrugInteractionsRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """
    Automated pairwise drug interaction evaluation across a prescription or administration list.
    Flags contraindicated and major risks requiring clinical justification override.
    """
    return CheckDrugInteractionsAction(db, hospital_id).execute(payload)
