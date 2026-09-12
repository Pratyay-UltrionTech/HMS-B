"""
CSSD repository handling database interactions for the sterile instrument lifecycle domain.
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.cssd.entities.cssd_entities import (
    InstrumentDiscrepancyReport,
    InstrumentSet,
    InstrumentSetIssue,
    InstrumentSetItem,
    SterilizationBatch,
    SterilizationBatchItem,
    SterilizationQC,
)


class CssdRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Instrument Sets ───────────────────────────────────────────────────────

    def get_set_by_code(self, hospital_id: UUID, set_code: str) -> InstrumentSet | None:
        return (
            self.db.query(InstrumentSet)
            .filter(InstrumentSet.hospital_id == hospital_id, InstrumentSet.set_code == set_code)
            .first()
        )

    def get_set_by_id(self, set_id: UUID, hospital_id: UUID) -> InstrumentSet | None:
        return (
            self.db.query(InstrumentSet)
            .options(joinedload(InstrumentSet.items))
            .filter(InstrumentSet.id == set_id, InstrumentSet.hospital_id == hospital_id)
            .first()
        )

    def list_sets(self, hospital_id: UUID, active_only: bool | None = None) -> Sequence[InstrumentSet]:
        q = (
            self.db.query(InstrumentSet)
            .options(joinedload(InstrumentSet.items))
            .filter(InstrumentSet.hospital_id == hospital_id)
        )
        if active_only:
            q = q.filter(InstrumentSet.is_active.is_(True))
        return q.order_by(InstrumentSet.set_code.asc()).limit(500).all()

    # ── Sterilization Batches ────────────────────────────────────────────────

    def get_batch_by_number(self, hospital_id: UUID, batch_number: str) -> SterilizationBatch | None:
        return (
            self.db.query(SterilizationBatch)
            .filter(SterilizationBatch.hospital_id == hospital_id, SterilizationBatch.batch_number == batch_number)
            .first()
        )

    def get_batch_by_id(self, batch_id: UUID, hospital_id: UUID) -> SterilizationBatch | None:
        return (
            self.db.query(SterilizationBatch)
            .options(joinedload(SterilizationBatch.items), joinedload(SterilizationBatch.qc))
            .filter(SterilizationBatch.id == batch_id, SterilizationBatch.hospital_id == hospital_id)
            .first()
        )

    def list_batches(self, hospital_id: UUID, status_filter=None) -> Sequence[SterilizationBatch]:
        q = (
            self.db.query(SterilizationBatch)
            .options(joinedload(SterilizationBatch.items), joinedload(SterilizationBatch.qc))
            .filter(SterilizationBatch.hospital_id == hospital_id)
        )
        if status_filter:
            q = q.filter(SterilizationBatch.status == status_filter)
        return q.order_by(SterilizationBatch.created_at.desc()).limit(300).all()

    def next_batch_number(self, hospital_id: UUID) -> str:
        count = (
            self.db.query(func.count(SterilizationBatch.id))
            .filter(SterilizationBatch.hospital_id == hospital_id)
            .scalar()
            or 0
        )
        return f"STB{int(count) + 1:05d}"

    # ── QC ────────────────────────────────────────────────────────────────────

    def get_qc_by_batch_id(self, batch_id: UUID, hospital_id: UUID) -> SterilizationQC | None:
        return (
            self.db.query(SterilizationQC)
            .filter(SterilizationQC.batch_id == batch_id, SterilizationQC.hospital_id == hospital_id)
            .first()
        )

    # ── Issue & Return ────────────────────────────────────────────────────────

    def get_issue_by_id(self, issue_id: UUID, hospital_id: UUID) -> InstrumentSetIssue | None:
        return (
            self.db.query(InstrumentSetIssue)
            .options(
                joinedload(InstrumentSetIssue.instrument_set),
                joinedload(InstrumentSetIssue.batch),
            )
            .filter(InstrumentSetIssue.id == issue_id, InstrumentSetIssue.hospital_id == hospital_id)
            .first()
        )

    def list_issues(self, hospital_id: UUID, status_filter=None) -> Sequence[InstrumentSetIssue]:
        q = (
            self.db.query(InstrumentSetIssue)
            .options(
                joinedload(InstrumentSetIssue.instrument_set),
                joinedload(InstrumentSetIssue.batch),
            )
            .filter(InstrumentSetIssue.hospital_id == hospital_id)
        )
        if status_filter:
            q = q.filter(InstrumentSetIssue.status == status_filter)
        return q.order_by(InstrumentSetIssue.issue_time.desc()).limit(300).all()

    # ── Discrepancy Reports ───────────────────────────────────────────────────

    def get_discrepancy_by_id(self, report_id: UUID, hospital_id: UUID) -> InstrumentDiscrepancyReport | None:
        return (
            self.db.query(InstrumentDiscrepancyReport)
            .filter(
                InstrumentDiscrepancyReport.id == report_id,
                InstrumentDiscrepancyReport.hospital_id == hospital_id,
            )
            .first()
        )

    def list_discrepancies(self, hospital_id: UUID, status_filter=None) -> Sequence[InstrumentDiscrepancyReport]:
        q = self.db.query(InstrumentDiscrepancyReport).filter(InstrumentDiscrepancyReport.hospital_id == hospital_id)
        if status_filter:
            q = q.filter(InstrumentDiscrepancyReport.investigation_status == status_filter)
        return q.order_by(InstrumentDiscrepancyReport.reported_at.desc()).limit(300).all()
