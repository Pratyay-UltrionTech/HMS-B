"""
CSSD (Central Sterile Services Department) domain business action handlers.

Enforced lifecycle rules (see spec Features 39-43):
- SterilizationBatch: in_progress -> completed | failed. `released` / `quarantined` are only
  ever set by the QC action (record_qc), never directly by a batch-status endpoint.
- SterilizationQC: can only be recorded once a batch is `completed`. Recording QC with an
  overall pass sets the batch to `released`; an overall fail sets it to `quarantined`.
- InstrumentSetIssue: a set may only be issued from a batch that is `released`. Returning
  a set with a damaged/missing condition optionally (explicitly, via return_condition)
  creates a Feature 43 InstrumentDiscrepancyReport in the same action.
- InstrumentDiscrepancyReport.investigation_status: open -> investigating -> resolved |
  replaced. Skipping straight from open to resolved/replaced is rejected — investigating
  must be recorded first. This is the one clear rule chosen per the spec's requirement to
  "enforce, no skipping".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.cssd.contracts.cssd_contracts import (
    DiscrepancyReportCreate,
    DiscrepancyReportResponse,
    DiscrepancyReportTransition,
    InstrumentSetCreate,
    InstrumentSetIssueCreate,
    InstrumentSetIssueResponse,
    InstrumentSetItemResponse,
    InstrumentSetResponse,
    InstrumentSetReturn,
    InstrumentSetUpdate,
    SterilizationBatchComplete,
    SterilizationBatchCreate,
    SterilizationBatchFail,
    SterilizationBatchItemResponse,
    SterilizationBatchResponse,
    SterilizationQCCreate,
    SterilizationQCResponse,
)
from hms_migration.modules.cssd.db.cssd_repository import CssdRepository
from hms_migration.modules.cssd.entities.cssd_entities import (
    DiscrepancyInvestigationStatus,
    InstrumentDiscrepancyReport,
    InstrumentSet,
    InstrumentSetIssue,
    InstrumentSetIssueStatus,
    InstrumentSetItem,
    QcResult,
    ReturnCondition,
    SterilizationBatch,
    SterilizationBatchItem,
    SterilizationBatchStatus,
    SterilizationQC,
)
from hms_migration.shared.audit import write_audit_log

# Valid forward transitions for a discrepancy report's investigation status.
# open -> investigating -> resolved | replaced. No other transition is permitted
# (in particular, open cannot jump straight to resolved/replaced).
_DISCREPANCY_TRANSITIONS: dict[DiscrepancyInvestigationStatus, set[DiscrepancyInvestigationStatus]] = {
    DiscrepancyInvestigationStatus.open: {DiscrepancyInvestigationStatus.investigating},
    DiscrepancyInvestigationStatus.investigating: {
        DiscrepancyInvestigationStatus.resolved,
        DiscrepancyInvestigationStatus.replaced,
    },
    DiscrepancyInvestigationStatus.resolved: set(),
    DiscrepancyInvestigationStatus.replaced: set(),
}


def _set_to_response(row: InstrumentSet) -> InstrumentSetResponse:
    return InstrumentSetResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        set_code=row.set_code,
        set_name=row.set_name,
        owning_department=row.owning_department,
        instrument_count=row.instrument_count,
        description=row.description,
        is_active=row.is_active,
        created_at=row.created_at,
        items=[InstrumentSetItemResponse.model_validate(i) for i in row.items],
    )


def _batch_to_response(row: SterilizationBatch) -> SterilizationBatchResponse:
    return SterilizationBatchResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        batch_number=row.batch_number,
        sterilization_method=row.sterilization_method,
        machine_id=row.machine_id,
        operator_staff_id=row.operator_staff_id,
        cycle_start_time=row.cycle_start_time,
        cycle_end_time=row.cycle_end_time,
        temperature_c=row.temperature_c,
        pressure_kpa=row.pressure_kpa,
        status=row.status,
        created_at=row.created_at,
        items=[
            SterilizationBatchItemResponse(
                id=i.id,
                instrument_set_id=i.instrument_set_id,
                instrument_set_name=i.instrument_set.set_name if i.instrument_set else None,
            )
            for i in row.items
        ],
    )


def _issue_to_response(row: InstrumentSetIssue) -> InstrumentSetIssueResponse:
    return InstrumentSetIssueResponse(
        id=row.id,
        instrument_set_id=row.instrument_set_id,
        instrument_set_name=row.instrument_set.set_name if row.instrument_set else None,
        batch_id=row.batch_id,
        batch_number=row.batch.batch_number if row.batch else None,
        issued_to_department=row.issued_to_department,
        issued_to_staff_id=row.issued_to_staff_id,
        issue_time=row.issue_time,
        expected_return_time=row.expected_return_time,
        status=row.status,
        return_time=row.return_time,
        returned_by_staff_id=row.returned_by_staff_id,
        return_condition=row.return_condition,
    )


class CssdActions:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = CssdRepository(db)

    # ── Feature 39: Instrument Set Catalogue ─────────────────────────────────

    def list_instrument_sets(self, active_only: bool | None = None) -> list[InstrumentSetResponse]:
        rows = self.repo.list_sets(self.hospital_id, active_only=active_only)
        return [_set_to_response(r) for r in rows]

    def create_instrument_set(self, payload: InstrumentSetCreate) -> InstrumentSetResponse:
        set_code = (payload.set_code or "").strip().upper()
        if not set_code:
            count = len(self.repo.list_sets(self.hospital_id))
            set_code = f"IS{count + 1:04d}"
        if self.repo.get_set_by_code(self.hospital_id, set_code):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Instrument set code already exists")

        items = [
            InstrumentSetItem(
                hospital_id=self.hospital_id,
                instrument_name=i.instrument_name.strip(),
                quantity=i.quantity,
            )
            for i in payload.items
        ]
        row = InstrumentSet(
            hospital_id=self.hospital_id,
            set_code=set_code,
            set_name=payload.set_name.strip(),
            owning_department=payload.owning_department.strip() if payload.owning_department else None,
            description=payload.description,
            is_active=payload.is_active,
            instrument_count=sum(i.quantity for i in items) if items else 0,
            items=items,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="cssd_instrument_set",
            entity_id=row.id,
            summary=f"Created instrument set {row.set_code} — {row.set_name}",
        )
        self.db.commit()
        self.db.refresh(row)
        return _set_to_response(row)

    def update_instrument_set(self, set_id: UUID, payload: InstrumentSetUpdate) -> InstrumentSetResponse:
        row = self.repo.get_set_by_id(set_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument set not found")

        data = payload.model_dump(exclude_unset=True)
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip() or None
            setattr(row, k, v)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="cssd_instrument_set",
            entity_id=row.id,
            summary=f"Updated instrument set {row.set_code}",
        )
        self.db.commit()
        self.db.refresh(row)
        return _set_to_response(row)

    # ── Feature 40: Sterilization Batch ──────────────────────────────────────

    def list_batches(self, status_filter: str | None = None) -> list[SterilizationBatchResponse]:
        status_enum = None
        if status_filter:
            try:
                status_enum = SterilizationBatchStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
        rows = self.repo.list_batches(self.hospital_id, status_filter=status_enum)
        return [_batch_to_response(r) for r in rows]

    def get_batch(self, batch_id: UUID) -> SterilizationBatchResponse:
        row = self.repo.get_batch_by_id(batch_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sterilization batch not found")
        return _batch_to_response(row)

    def create_batch(self, payload: SterilizationBatchCreate) -> SterilizationBatchResponse:
        batch_number = (payload.batch_number or "").strip().upper() or self.repo.next_batch_number(self.hospital_id)
        if self.repo.get_batch_by_number(self.hospital_id, batch_number):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Batch number already exists")

        items: list[SterilizationBatchItem] = []
        for set_id in payload.instrument_set_ids:
            iset = self.repo.get_set_by_id(set_id, self.hospital_id)
            if not iset:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Instrument set {set_id} not found")
            items.append(SterilizationBatchItem(hospital_id=self.hospital_id, instrument_set_id=iset.id))

        row = SterilizationBatch(
            hospital_id=self.hospital_id,
            batch_number=batch_number,
            sterilization_method=payload.sterilization_method,
            machine_id=payload.machine_id,
            operator_staff_id=payload.operator_staff_id,
            temperature_c=payload.temperature_c,
            pressure_kpa=payload.pressure_kpa,
            status=SterilizationBatchStatus.in_progress,
            items=items,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="cssd_sterilization_batch",
            entity_id=row.id,
            summary=f"Started sterilization batch {row.batch_number} ({row.sterilization_method.value})",
        )
        self.db.commit()
        self.db.refresh(row)
        return _batch_to_response(row)

    def complete_batch(self, batch_id: UUID, payload: SterilizationBatchComplete) -> SterilizationBatchResponse:
        row = self.repo.get_batch_by_id(batch_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sterilization batch not found")
        if row.status != SterilizationBatchStatus.in_progress:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only an in_progress batch can be completed",
            )

        row.status = SterilizationBatchStatus.completed
        row.cycle_end_time = datetime.now(timezone.utc)
        if payload.temperature_c is not None:
            row.temperature_c = payload.temperature_c
        if payload.pressure_kpa is not None:
            row.pressure_kpa = payload.pressure_kpa

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="cssd_sterilization_batch",
            entity_id=row.id,
            summary=f"Completed sterilization cycle for batch {row.batch_number}",
        )
        self.db.commit()
        self.db.refresh(row)
        return _batch_to_response(row)

    def fail_batch(self, batch_id: UUID, payload: SterilizationBatchFail) -> SterilizationBatchResponse:
        row = self.repo.get_batch_by_id(batch_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sterilization batch not found")
        if row.status != SterilizationBatchStatus.in_progress:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only an in_progress batch can be marked failed",
            )

        row.status = SterilizationBatchStatus.failed
        row.cycle_end_time = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="cssd_sterilization_batch",
            entity_id=row.id,
            summary=f"Sterilization cycle failed for batch {row.batch_number}: {payload.remarks or ''}".strip(),
        )
        self.db.commit()
        self.db.refresh(row)
        return _batch_to_response(row)

    # ── Feature 41: Sterilization QC ──────────────────────────────────────────

    def record_qc(self, batch_id: UUID, payload: SterilizationQCCreate) -> SterilizationQCResponse:
        batch = self.repo.get_batch_by_id(batch_id, self.hospital_id)
        if not batch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sterilization batch not found")
        if batch.status != SterilizationBatchStatus.completed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="QC can only be recorded once the sterilization cycle is completed",
            )
        if self.repo.get_qc_by_batch_id(batch_id, self.hospital_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="QC already recorded for this batch")

        overall = (
            QcResult.pass_
            if payload.chemical_indicator_result == QcResult.pass_
            and payload.biological_indicator_result == "pass"
            and (payload.bowie_dick_test_result in (None, "not_applicable", "pass"))
            else QcResult.fail
        )

        qc = SterilizationQC(
            hospital_id=self.hospital_id,
            batch_id=batch.id,
            chemical_indicator_result=payload.chemical_indicator_result,
            biological_indicator_result=payload.biological_indicator_result,
            bowie_dick_test_result=payload.bowie_dick_test_result,
            overall_result=overall,
            checked_by_staff_id=payload.checked_by_staff_id,
            remarks=payload.remarks,
        )
        self.db.add(qc)

        batch.status = (
            SterilizationBatchStatus.released if overall == QcResult.pass_ else SterilizationBatchStatus.quarantined
        )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="cssd_sterilization_qc",
            entity_id=qc.id if qc.id else batch.id,
            summary=f"QC recorded for batch {batch.batch_number}: overall={overall.value} -> {batch.status.value}",
        )
        self.db.commit()
        self.db.refresh(qc)
        return SterilizationQCResponse.model_validate(qc)

    def get_qc(self, batch_id: UUID) -> SterilizationQCResponse:
        qc = self.repo.get_qc_by_batch_id(batch_id, self.hospital_id)
        if not qc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="QC record not found for this batch")
        return SterilizationQCResponse.model_validate(qc)

    # ── Feature 42: Issue & Return Tracking ──────────────────────────────────

    def list_issues(self, status_filter: str | None = None) -> list[InstrumentSetIssueResponse]:
        status_enum = None
        if status_filter:
            try:
                status_enum = InstrumentSetIssueStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
        rows = self.repo.list_issues(self.hospital_id, status_filter=status_enum)
        return [_issue_to_response(r) for r in rows]

    def issue_set(self, payload: InstrumentSetIssueCreate) -> InstrumentSetIssueResponse:
        iset = self.repo.get_set_by_id(payload.instrument_set_id, self.hospital_id)
        if not iset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument set not found")
        batch = self.repo.get_batch_by_id(payload.batch_id, self.hospital_id)
        if not batch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sterilization batch not found")
        if batch.status != SterilizationBatchStatus.released:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only a released (QC-passed) batch can be issued for clinical use",
            )
        if not any(i.instrument_set_id == iset.id for i in batch.items):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Instrument set was not part of this sterilization batch",
            )

        row = InstrumentSetIssue(
            hospital_id=self.hospital_id,
            instrument_set_id=iset.id,
            batch_id=batch.id,
            issued_to_department=payload.issued_to_department.strip(),
            issued_to_staff_id=payload.issued_to_staff_id,
            expected_return_time=payload.expected_return_time,
            status=InstrumentSetIssueStatus.issued,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="cssd_instrument_set_issue",
            entity_id=row.id,
            summary=f"Issued set {iset.set_code} (batch {batch.batch_number}) to {row.issued_to_department}",
        )
        self.db.commit()
        self.db.refresh(row)
        return _issue_to_response(row)

    def return_set(self, issue_id: UUID, payload: InstrumentSetReturn) -> InstrumentSetIssueResponse:
        row = self.repo.get_issue_by_id(issue_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue record not found")
        if row.status != InstrumentSetIssueStatus.issued:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only an issued set can be returned")

        row.status = InstrumentSetIssueStatus.returned
        row.return_time = datetime.now(timezone.utc)
        row.returned_by_staff_id = payload.returned_by_staff_id
        row.return_condition = payload.return_condition

        summary = f"Returned set for issue {row.id} — condition: {payload.return_condition.value}"

        if payload.return_condition in (ReturnCondition.damaged, ReturnCondition.missing_items):
            discrepancy_type = "damaged" if payload.return_condition == ReturnCondition.damaged else "missing"
            report = InstrumentDiscrepancyReport(
                hospital_id=self.hospital_id,
                issue_id=row.id,
                discrepancy_type=discrepancy_type,
                instrument_name=(payload.discrepancy_instrument_name or "Unspecified").strip(),
                quantity_affected=payload.discrepancy_quantity_affected,
                reported_by_staff_id=payload.returned_by_staff_id,
                investigation_status=DiscrepancyInvestigationStatus.open,
                resolution_notes=payload.discrepancy_remarks,
            )
            self.db.add(report)
            summary += f" (auto-created discrepancy report: {discrepancy_type})"

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="cssd_instrument_set_issue",
            entity_id=row.id,
            summary=summary,
        )
        self.db.commit()
        self.db.refresh(row)
        return _issue_to_response(row)

    # ── Feature 43: Missing/Damaged Instrument Tracking ──────────────────────

    def list_discrepancies(self, status_filter: str | None = None) -> list[DiscrepancyReportResponse]:
        status_enum = None
        if status_filter:
            try:
                status_enum = DiscrepancyInvestigationStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
        rows = self.repo.list_discrepancies(self.hospital_id, status_filter=status_enum)
        return [DiscrepancyReportResponse.model_validate(r) for r in rows]

    def report_discrepancy(self, payload: DiscrepancyReportCreate) -> DiscrepancyReportResponse:
        issue = self.repo.get_issue_by_id(payload.issue_id, self.hospital_id)
        if not issue:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue record not found")

        row = InstrumentDiscrepancyReport(
            hospital_id=self.hospital_id,
            issue_id=issue.id,
            discrepancy_type=payload.discrepancy_type,
            instrument_name=payload.instrument_name.strip(),
            quantity_affected=payload.quantity_affected,
            reported_by_staff_id=payload.reported_by_staff_id,
            investigation_status=DiscrepancyInvestigationStatus.open,
        )
        self.db.add(row)
        self.db.flush()

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="cssd_instrument_discrepancy_report",
            entity_id=row.id,
            summary=f"Discrepancy reported ({payload.discrepancy_type.value}): {row.instrument_name}",
        )
        self.db.commit()
        self.db.refresh(row)
        return DiscrepancyReportResponse.model_validate(row)

    def transition_discrepancy(
        self, report_id: UUID, payload: DiscrepancyReportTransition
    ) -> DiscrepancyReportResponse:
        row = self.repo.get_discrepancy_by_id(report_id, self.hospital_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discrepancy report not found")

        allowed = _DISCREPANCY_TRANSITIONS.get(row.investigation_status, set())
        if payload.investigation_status not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot transition discrepancy report from "
                    f"{row.investigation_status.value} to {payload.investigation_status.value}. "
                    "Allowed sequence is open -> investigating -> resolved|replaced."
                ),
            )

        row.investigation_status = payload.investigation_status
        if payload.resolution_notes:
            row.resolution_notes = payload.resolution_notes.strip()
        if payload.investigation_status in (
            DiscrepancyInvestigationStatus.resolved,
            DiscrepancyInvestigationStatus.replaced,
        ):
            row.resolved_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="cssd_instrument_discrepancy_report",
            entity_id=row.id,
            summary=f"Discrepancy report {row.id} -> {row.investigation_status.value}",
        )
        self.db.commit()
        self.db.refresh(row)
        return DiscrepancyReportResponse.model_validate(row)
