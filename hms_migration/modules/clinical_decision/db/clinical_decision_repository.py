"""
Database repository for Clinical Decision Support and Medication Safety.

Performs isolated SQLAlchemy queries scoped strictly by hospital_id.
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from hms_migration.modules.clinical_decision.entities.clinical_decision_entities import (
    ClinicalAlert,
    ClinicalAlertStatus,
    ClinicalOrderSet,
    ClinicalOrderSetItem,
    ClinicalRule,
    MedicationReconciliation,
    MedicationReconciliationItem,
)


class ClinicalDecisionRepository:
    """Repository handling persistence for Features 19, 20, and 21."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    # -----------------------------------------------------------------------
    # Feature 19: Medication Reconciliation
    # -----------------------------------------------------------------------

    def create_reconciliation(self, reconciliation: MedicationReconciliation) -> MedicationReconciliation:
        reconciliation.hospital_id = self.hospital_id
        self.db.add(reconciliation)
        self.db.flush()
        self.db.refresh(reconciliation)
        return reconciliation

    def get_reconciliation_by_id(self, rec_id: UUID) -> MedicationReconciliation | None:
        stmt = (
            select(MedicationReconciliation)
            .where(
                MedicationReconciliation.id == rec_id,
                MedicationReconciliation.hospital_id == self.hospital_id,
            )
            .options(selectinload(MedicationReconciliation.items))
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_reconciliations_for_patient(self, patient_id: UUID) -> Sequence[MedicationReconciliation]:
        stmt = (
            select(MedicationReconciliation)
            .where(
                MedicationReconciliation.patient_id == patient_id,
                MedicationReconciliation.hospital_id == self.hospital_id,
            )
            .options(selectinload(MedicationReconciliation.items))
            .order_by(MedicationReconciliation.created_at.desc())
        )
        return self.db.execute(stmt).scalars().all()

    def list_reconciliations_for_admission(self, admission_id: UUID) -> Sequence[MedicationReconciliation]:
        stmt = (
            select(MedicationReconciliation)
            .where(
                MedicationReconciliation.admission_id == admission_id,
                MedicationReconciliation.hospital_id == self.hospital_id,
            )
            .options(selectinload(MedicationReconciliation.items))
            .order_by(MedicationReconciliation.created_at.desc())
        )
        return self.db.execute(stmt).scalars().all()

    # -----------------------------------------------------------------------
    # Feature 20: Clinical Rules & Alerts
    # -----------------------------------------------------------------------

    def create_rule(self, rule: ClinicalRule) -> ClinicalRule:
        rule.hospital_id = self.hospital_id
        self.db.add(rule)
        self.db.flush()
        self.db.refresh(rule)
        return rule

    def get_rule_by_id(self, rule_id: UUID) -> ClinicalRule | None:
        stmt = select(ClinicalRule).where(
            ClinicalRule.id == rule_id,
            ClinicalRule.hospital_id == self.hospital_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_rule_by_code(self, code: str) -> ClinicalRule | None:
        stmt = select(ClinicalRule).where(
            ClinicalRule.rule_code == code,
            ClinicalRule.hospital_id == self.hospital_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_active_rules(self) -> Sequence[ClinicalRule]:
        stmt = (
            select(ClinicalRule)
            .where(
                ClinicalRule.hospital_id == self.hospital_id,
                ClinicalRule.is_active.is_(True),
            )
            .order_by(ClinicalRule.category, ClinicalRule.name)
        )
        return self.db.execute(stmt).scalars().all()

    def create_alert(self, alert: ClinicalAlert) -> ClinicalAlert:
        alert.hospital_id = self.hospital_id
        self.db.add(alert)
        self.db.flush()
        self.db.refresh(alert)
        return alert

    def get_alert_by_id(self, alert_id: UUID) -> ClinicalAlert | None:
        stmt = select(ClinicalAlert).where(
            ClinicalAlert.id == alert_id,
            ClinicalAlert.hospital_id == self.hospital_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_alerts_for_patient(
        self, patient_id: UUID, status: ClinicalAlertStatus | None = None
    ) -> Sequence[ClinicalAlert]:
        stmt = select(ClinicalAlert).where(
            ClinicalAlert.patient_id == patient_id,
            ClinicalAlert.hospital_id == self.hospital_id,
        )
        if status:
            stmt = stmt.where(ClinicalAlert.status == status)
        stmt = stmt.order_by(ClinicalAlert.triggered_at.desc())
        return self.db.execute(stmt).scalars().all()

    def list_alerts(
        self, patient_id: UUID | None = None, status: ClinicalAlertStatus | None = None
    ) -> Sequence[ClinicalAlert]:
        """Hospital-scoped alert list used by GET /clinical/alerts.

        Supports the frontend query contract (?patient_id=&status=) so the
        Clinical Decision workspace can load without a patient pre-selected.
        """
        stmt = select(ClinicalAlert).where(
            ClinicalAlert.hospital_id == self.hospital_id,
        )
        if patient_id:
            stmt = stmt.where(ClinicalAlert.patient_id == patient_id)
        if status:
            stmt = stmt.where(ClinicalAlert.status == status)
        stmt = stmt.order_by(ClinicalAlert.triggered_at.desc()).limit(500)
        return self.db.execute(stmt).scalars().all()

    # -----------------------------------------------------------------------
    # Feature 21: Clinical Order Sets
    # -----------------------------------------------------------------------

    def create_order_set(self, order_set: ClinicalOrderSet) -> ClinicalOrderSet:
        order_set.hospital_id = self.hospital_id
        self.db.add(order_set)
        self.db.flush()
        self.db.refresh(order_set)
        return order_set

    def get_order_set_by_id(self, set_id: UUID) -> ClinicalOrderSet | None:
        stmt = (
            select(ClinicalOrderSet)
            .where(
                ClinicalOrderSet.id == set_id,
                ClinicalOrderSet.hospital_id == self.hospital_id,
            )
            .options(selectinload(ClinicalOrderSet.items))
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_order_set_by_code(self, code: str) -> ClinicalOrderSet | None:
        stmt = select(ClinicalOrderSet).where(
            ClinicalOrderSet.code == code,
            ClinicalOrderSet.hospital_id == self.hospital_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_order_sets(self, category: str | None = None) -> Sequence[ClinicalOrderSet]:
        stmt = (
            select(ClinicalOrderSet)
            .where(
                ClinicalOrderSet.hospital_id == self.hospital_id,
                ClinicalOrderSet.is_active.is_(True),
            )
            .options(selectinload(ClinicalOrderSet.items))
        )
        if category:
            stmt = stmt.where(ClinicalOrderSet.category == category)
        stmt = stmt.order_by(ClinicalOrderSet.category, ClinicalOrderSet.name)
        return self.db.execute(stmt).scalars().all()
