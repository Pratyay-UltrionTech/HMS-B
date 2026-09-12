"""
Database repository for Drug Interaction Rules (Feature 17).

Conforms to UltrionTech-Backend-Template modules/pharmacy/db/ specification.
All operations scoped strictly to hospital_id.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from hms_migration.modules.pharmacy.entities.drug_interaction_entities import DrugInteractionRule


class DrugInteractionRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_by_id(self, rule_id: UUID) -> DrugInteractionRule | None:
        return (
            self.db.query(DrugInteractionRule)
            .filter(
                DrugInteractionRule.id == rule_id,
                DrugInteractionRule.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_active_rules(self) -> list[DrugInteractionRule]:
        return (
            self.db.query(DrugInteractionRule)
            .filter(
                DrugInteractionRule.hospital_id == self.hospital_id,
                DrugInteractionRule.is_active.is_(True),
            )
            .order_by(DrugInteractionRule.created_at.desc())
            .all()
        )

    def find_interaction(self, drug_1: str, drug_2: str) -> DrugInteractionRule | None:
        """Find active rule for drug pair in either direction (A-B or B-A)."""
        d1 = drug_1.strip().lower()
        d2 = drug_2.strip().lower()

        # Fetch active rules for hospital and match substrings or exact
        rules = self.list_active_rules()
        for r in rules:
            ra = r.drug_a.strip().lower()
            rb = r.drug_b.strip().lower()
            if (ra in d1 or d1 in ra) and (rb in d2 or d2 in rb):
                return r
            if (ra in d2 or d2 in ra) and (rb in d1 or d1 in rb):
                return r
        return None

    def add(self, entity: object) -> None:
        self.db.add(entity)

    def commit(self) -> None:
        self.db.commit()

    def refresh(self, entity: object) -> None:
        self.db.refresh(entity)
