"""
Target action implementation for Drug Interactions Engine (Feature 17).

Conforms to UltrionTech-Backend-Template modules/pharmacy/actions/ specification.
Follows Vertical Slice: API -> Actions -> Repository -> Entity.
"""

from __future__ import annotations

import itertools
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.pharmacy.contracts.drug_interaction_contracts import (
    CheckDrugInteractionsRequest,
    DrugInteractionAlert,
    DrugInteractionReport,
    DrugInteractionRuleCreate,
    DrugInteractionRuleResponse,
)
from hms_migration.modules.pharmacy.db.drug_interaction_repository import DrugInteractionRepository
from hms_migration.modules.pharmacy.entities.drug_interaction_entities import (
    DrugInteractionRule,
    InteractionSeverity,
)
from hms_migration.shared.audit.service import write_audit_log

SEVERITY_ORDER = {
    InteractionSeverity.contraindicated: 4,
    InteractionSeverity.major: 3,
    InteractionSeverity.moderate: 2,
    InteractionSeverity.minor: 1,
}


class CreateDrugInteractionRuleAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.repo = DrugInteractionRepository(db, hospital_id)

    def execute(
        self,
        payload: DrugInteractionRuleCreate,
        actor: dict[str, Any],
    ) -> DrugInteractionRuleResponse:
        existing = self.repo.find_interaction(payload.drug_a, payload.drug_b)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Interaction rule for '{payload.drug_a}' and '{payload.drug_b}' already exists",
            )

        rule = DrugInteractionRule(
            hospital_id=self.hospital_id,
            drug_a=payload.drug_a.strip(),
            drug_b=payload.drug_b.strip(),
            severity=payload.severity,
            mechanism=payload.mechanism.strip(),
            clinical_recommendation=payload.clinical_recommendation.strip(),
            is_active=True,
        )
        self.repo.add(rule)
        self.repo.commit()
        self.repo.refresh(rule)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=actor,
            action="CREATE_DRUG_INTERACTION_RULE",
            entity_type="drug_interaction_rules",
            summary=f"Created interaction rule: {rule.drug_a} + {rule.drug_b} ({rule.severity.value})",
            entity_id=rule.id,
        )
        return DrugInteractionRuleResponse.model_validate(rule)


class ListDrugInteractionRulesAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = DrugInteractionRepository(db, hospital_id)

    def execute(self) -> list[DrugInteractionRuleResponse]:
        rules = self.repo.list_active_rules()
        return [DrugInteractionRuleResponse.model_validate(r) for r in rules]


class CheckDrugInteractionsAction:
    """
    Feature 17 Automated Pairwise Evaluation:
    Evaluates medication cocktail across all pairwise combinations to catch
    contraindicated, major, moderate, or minor pharmacodynamic/pharmacokinetic interactions.
    """

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = DrugInteractionRepository(db, hospital_id)

    def execute(self, payload: CheckDrugInteractionsRequest) -> DrugInteractionReport:
        medicines = [m.strip() for m in payload.medicines if m.strip()]
        if len(medicines) < 2:
            return DrugInteractionReport(has_conflicts=False, alerts=[])

        alerts: list[DrugInteractionAlert] = []
        highest_sev_rank = 0
        highest_sev_enum: InteractionSeverity | None = None

        seen_pairs: set[tuple[str, str]] = set()

        for med1, med2 in itertools.combinations(medicines, 2):
            rule = self.repo.find_interaction(med1, med2)
            if rule:
                pair_key = tuple(sorted([rule.drug_a.lower(), rule.drug_b.lower()]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                alert = DrugInteractionAlert(
                    drug_a=med1,
                    drug_b=med2,
                    severity=rule.severity,
                    mechanism=rule.mechanism,
                    clinical_recommendation=rule.clinical_recommendation,
                )
                alerts.append(alert)

                rank = SEVERITY_ORDER.get(rule.severity, 0)
                if rank > highest_sev_rank:
                    highest_sev_rank = rank
                    highest_sev_enum = rule.severity

        requires_override = highest_sev_rank >= SEVERITY_ORDER[InteractionSeverity.major]

        return DrugInteractionReport(
            has_conflicts=len(alerts) > 0,
            alerts=alerts,
            highest_severity=highest_sev_enum,
            requires_clinical_override=requires_override,
        )
