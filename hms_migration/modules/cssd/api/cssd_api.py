"""
CSSD (Central Sterile Services Department) API endpoints.

Prefix: /cssd
Tags: ["cssd"]
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.cssd.actions.cssd_actions import CssdActions
from hms_migration.modules.cssd.contracts.cssd_contracts import (
    DiscrepancyReportCreate,
    DiscrepancyReportResponse,
    DiscrepancyReportTransition,
    InstrumentSetCreate,
    InstrumentSetIssueCreate,
    InstrumentSetIssueResponse,
    InstrumentSetResponse,
    InstrumentSetReturn,
    InstrumentSetUpdate,
    SterilizationBatchComplete,
    SterilizationBatchCreate,
    SterilizationBatchFail,
    SterilizationBatchResponse,
    SterilizationQCCreate,
    SterilizationQCResponse,
)
from hms_migration.shared.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/cssd", tags=["cssd"])


# ── Feature 39: Instrument Set Catalogue ─────────────────────────────────────

@router.get("/instrument-sets", response_model=list[InstrumentSetResponse])
def list_instrument_sets(
    active_only: bool | None = Query(None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[InstrumentSetResponse]:
    return CssdActions(db, hospital_id, user).list_instrument_sets(active_only=active_only)


@router.post("/instrument-sets", response_model=InstrumentSetResponse, status_code=status.HTTP_201_CREATED)
def create_instrument_set(
    payload: InstrumentSetCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InstrumentSetResponse:
    return CssdActions(db, hospital_id, user).create_instrument_set(payload)


@router.put("/instrument-sets/{set_id}", response_model=InstrumentSetResponse)
def update_instrument_set(
    set_id: UUID,
    payload: InstrumentSetUpdate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InstrumentSetResponse:
    return CssdActions(db, hospital_id, user).update_instrument_set(set_id, payload)


# ── Feature 40: Sterilization Batch ──────────────────────────────────────────

@router.get("/sterilization-batches", response_model=list[SterilizationBatchResponse])
def list_batches(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[SterilizationBatchResponse]:
    return CssdActions(db, hospital_id, user).list_batches(status_filter=status_filter)


@router.get("/sterilization-batches/{batch_id}", response_model=SterilizationBatchResponse)
def get_batch(
    batch_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SterilizationBatchResponse:
    return CssdActions(db, hospital_id, user).get_batch(batch_id)


@router.post("/sterilization-batches", response_model=SterilizationBatchResponse, status_code=status.HTTP_201_CREATED)
def create_batch(
    payload: SterilizationBatchCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SterilizationBatchResponse:
    return CssdActions(db, hospital_id, user).create_batch(payload)


@router.post("/sterilization-batches/{batch_id}/complete", response_model=SterilizationBatchResponse)
def complete_batch(
    batch_id: UUID,
    payload: SterilizationBatchComplete,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SterilizationBatchResponse:
    return CssdActions(db, hospital_id, user).complete_batch(batch_id, payload)


@router.post("/sterilization-batches/{batch_id}/fail", response_model=SterilizationBatchResponse)
def fail_batch(
    batch_id: UUID,
    payload: SterilizationBatchFail,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SterilizationBatchResponse:
    return CssdActions(db, hospital_id, user).fail_batch(batch_id, payload)


# ── Feature 41: Sterilization QC ─────────────────────────────────────────────

@router.post("/sterilization-batches/{batch_id}/qc", response_model=SterilizationQCResponse, status_code=status.HTTP_201_CREATED)
def record_qc(
    batch_id: UUID,
    payload: SterilizationQCCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SterilizationQCResponse:
    return CssdActions(db, hospital_id, user).record_qc(batch_id, payload)


@router.get("/sterilization-batches/{batch_id}/qc", response_model=SterilizationQCResponse)
def get_qc(
    batch_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> SterilizationQCResponse:
    return CssdActions(db, hospital_id, user).get_qc(batch_id)


# ── Feature 42: Issue & Return Tracking ──────────────────────────────────────

@router.get("/issues", response_model=list[InstrumentSetIssueResponse])
def list_issues(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[InstrumentSetIssueResponse]:
    return CssdActions(db, hospital_id, user).list_issues(status_filter=status_filter)


@router.post("/issues", response_model=InstrumentSetIssueResponse, status_code=status.HTTP_201_CREATED)
def issue_set(
    payload: InstrumentSetIssueCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InstrumentSetIssueResponse:
    return CssdActions(db, hospital_id, user).issue_set(payload)


@router.post("/issues/{issue_id}/return", response_model=InstrumentSetIssueResponse)
def return_set(
    issue_id: UUID,
    payload: InstrumentSetReturn,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> InstrumentSetIssueResponse:
    return CssdActions(db, hospital_id, user).return_set(issue_id, payload)


# ── Feature 43: Missing/Damaged Instrument Tracking ──────────────────────────

@router.get("/discrepancy-reports", response_model=list[DiscrepancyReportResponse])
def list_discrepancies(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> list[DiscrepancyReportResponse]:
    return CssdActions(db, hospital_id, user).list_discrepancies(status_filter=status_filter)


@router.post("/discrepancy-reports", response_model=DiscrepancyReportResponse, status_code=status.HTTP_201_CREATED)
def report_discrepancy(
    payload: DiscrepancyReportCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> DiscrepancyReportResponse:
    return CssdActions(db, hospital_id, user).report_discrepancy(payload)


@router.post("/discrepancy-reports/{report_id}/transition", response_model=DiscrepancyReportResponse)
def transition_discrepancy(
    report_id: UUID,
    payload: DiscrepancyReportTransition,
    db: Session = Depends(get_transitional_sync_session),
    user: dict[str, Any] = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
) -> DiscrepancyReportResponse:
    return CssdActions(db, hospital_id, user).transition_discrepancy(report_id, payload)
