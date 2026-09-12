"""
FastAPI router for Blood Bank Foundation endpoints.
Base prefix: /api/blood-bank
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.blood_bank.actions.blood_bank_actions import BloodBankActions
from hms_migration.modules.blood_bank.actions.blood_bank_traceability_actions import (
    BloodTraceabilityActions,
)
from hms_migration.modules.blood_bank.contracts.blood_bank_contracts import (
    BloodDonationCreate,
    BloodDonationResponse,
    BloodDonorCreate,
    BloodDonorResponse,
    BloodIssueCreate,
    BloodIssueResponse,
    BloodReturnCreate,
    BloodReturnResponse,
    BloodTransfusionResponse,
    BloodUnitCreate,
    BloodUnitResponse,
    BloodUnitSerologyClear,
    ComponentSeparationRequest,
    ComponentSeparationResponse,
    CrossMatchCreate,
    CrossMatchResponse,
    DonorTraceabilityResponse,
    TransfusionAbort,
    TransfusionComplete,
    TransfusionReactionReport,
    TransfusionStart,
    UnitTraceabilityResponse,
)
from hms_migration.modules.blood_bank.db.blood_bank_repository import BloodBankRepository
from hms_migration.modules.blood_bank.entities.blood_bank_entities import BloodGroup, BloodUnitStatus
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/blood-bank", tags=["Blood Bank"])


# ---------------------------------------------------------------------------
# Feature 32: Donors & Donations
# ---------------------------------------------------------------------------

@router.post("/donors", response_model=BloodDonorResponse, status_code=status.HTTP_201_CREATED)
def register_donor(
    payload: BloodDonorCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodDonorResponse:
    return BloodBankActions(db, hospital_id, current_user).register_donor(payload)


@router.get("/donors", response_model=list[BloodDonorResponse])
def get_donors(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[BloodDonorResponse]:
    return BloodBankActions(db, hospital_id, current_user).get_donors(limit=limit)


@router.get("/donors/{donor_id}", response_model=BloodDonorResponse)
def get_donor(
    donor_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodDonorResponse:
    return BloodBankActions(db, hospital_id, current_user).get_donor(donor_id)


@router.post("/donations", response_model=BloodDonationResponse, status_code=status.HTTP_201_CREATED)
def record_donation(
    payload: BloodDonationCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodDonationResponse:
    return BloodBankActions(db, hospital_id, current_user).record_donation(payload)


# ---------------------------------------------------------------------------
# Feature 33: Blood Unit Inventory
# ---------------------------------------------------------------------------

@router.post("/units", response_model=BloodUnitResponse, status_code=status.HTTP_201_CREATED)
def create_unit_manual(
    payload: BloodUnitCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodUnitResponse:
    return BloodBankActions(db, hospital_id, current_user).create_unit_manual(payload)


@router.get("/units", response_model=list[BloodUnitResponse])
def get_units(
    blood_group: BloodGroup | None = Query(default=None),
    status_filter: BloodUnitStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[BloodUnitResponse]:
    return BloodBankActions(db, hospital_id, current_user).get_units(
        blood_group=blood_group, status_filter=status_filter
    )


@router.get("/units/{unit_id}", response_model=BloodUnitResponse)
def get_unit(
    unit_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodUnitResponse:
    return BloodBankActions(db, hospital_id, current_user).get_unit(unit_id)


@router.patch("/units/{unit_id}/clear-serology", response_model=BloodUnitResponse)
def clear_serology(
    unit_id: UUID,
    payload: BloodUnitSerologyClear,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodUnitResponse:
    return BloodBankActions(db, hospital_id, current_user).clear_serology(unit_id, payload)


# ---------------------------------------------------------------------------
# Feature 34: Component Management & Separation Lineage
# ---------------------------------------------------------------------------

@router.post("/units/{unit_id}/separate", response_model=ComponentSeparationResponse, status_code=status.HTTP_201_CREATED)
def separate_components(
    unit_id: UUID,
    payload: ComponentSeparationRequest,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> ComponentSeparationResponse:
    return BloodBankActions(db, hospital_id, current_user).separate_components(unit_id, payload)


# ---------------------------------------------------------------------------
# Feature 35: Cross-Match Management
# ---------------------------------------------------------------------------

@router.post("/cross-matches", response_model=CrossMatchResponse, status_code=status.HTTP_201_CREATED)
def record_cross_match(
    payload: CrossMatchCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> CrossMatchResponse:
    return BloodBankActions(db, hospital_id, current_user).record_cross_match(payload)


@router.get("/cross-matches", response_model=list[CrossMatchResponse])
def get_cross_matches(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[CrossMatchResponse]:
    records = BloodBankRepository(db, hospital_id).list_cross_matches(limit=limit)
    return [CrossMatchResponse.model_validate(r) for r in records]


@router.get("/patients/{patient_id}/cross-matches", response_model=list[CrossMatchResponse])
def get_cross_matches_for_patient(
    patient_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[CrossMatchResponse]:
    records = BloodBankRepository(db, hospital_id).get_cross_matches_for_patient(patient_id)
    return [CrossMatchResponse.model_validate(r) for r in records]


# ---------------------------------------------------------------------------
# Feature 36: Blood Issue & Return
# ---------------------------------------------------------------------------

@router.get("/issues", response_model=list[BloodIssueResponse])
def get_issues(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> list[BloodIssueResponse]:
    records = BloodBankRepository(db, hospital_id).list_issues(limit=limit)
    return [BloodIssueResponse.model_validate(r) for r in records]


@router.post("/issues", response_model=BloodIssueResponse, status_code=status.HTTP_201_CREATED)
def issue_blood_unit(
    payload: BloodIssueCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodIssueResponse:
    return BloodBankActions(db, hospital_id, current_user).issue_blood_unit(payload)


@router.post("/issues/{issue_id}/return", response_model=BloodReturnResponse, status_code=status.HTTP_201_CREATED)
def return_blood_unit(
    issue_id: UUID,
    payload: BloodReturnCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodReturnResponse:
    return BloodBankActions(db, hospital_id, current_user).return_blood_unit(issue_id, payload)


# ---------------------------------------------------------------------------
# Feature 37: Transfusion Record
# ---------------------------------------------------------------------------

@router.post("/transfusions", response_model=BloodTransfusionResponse, status_code=status.HTTP_201_CREATED)
def start_transfusion(
    payload: TransfusionStart,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodTransfusionResponse:
    return BloodBankActions(db, hospital_id, current_user).start_transfusion(payload)


@router.get("/transfusions/{transfusion_id}", response_model=BloodTransfusionResponse)
def get_transfusion(
    transfusion_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodTransfusionResponse:
    transfusion = BloodBankRepository(db, hospital_id).get_transfusion_by_id(transfusion_id)
    if not transfusion:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfusion record not found")
    return BloodTransfusionResponse.model_validate(transfusion)


@router.patch("/transfusions/{transfusion_id}/complete", response_model=BloodTransfusionResponse)
def complete_transfusion(
    transfusion_id: UUID,
    payload: TransfusionComplete,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodTransfusionResponse:
    return BloodBankActions(db, hospital_id, current_user).complete_transfusion(transfusion_id, payload)


@router.patch("/transfusions/{transfusion_id}/abort", response_model=BloodTransfusionResponse)
def abort_transfusion(
    transfusion_id: UUID,
    payload: TransfusionAbort,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodTransfusionResponse:
    return BloodBankActions(db, hospital_id, current_user).abort_transfusion(transfusion_id, payload)


@router.patch("/transfusions/{transfusion_id}/reaction", response_model=BloodTransfusionResponse)
def report_transfusion_reaction(
    transfusion_id: UUID,
    payload: TransfusionReactionReport,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> BloodTransfusionResponse:
    return BloodBankActions(db, hospital_id, current_user).report_transfusion_reaction(transfusion_id, payload)


# ---------------------------------------------------------------------------
# Feature 38: Blood Traceability (read-only)
# ---------------------------------------------------------------------------

@router.get("/units/{unit_id}/traceability", response_model=UnitTraceabilityResponse)
def get_unit_traceability(
    unit_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> UnitTraceabilityResponse:
    return BloodTraceabilityActions(db, hospital_id).get_unit_traceability(unit_id)


@router.get("/donors/{donor_id}/traceability", response_model=DonorTraceabilityResponse)
def get_donor_traceability(
    donor_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    current_user: dict[str, Any] = Depends(require_hospital_user),
) -> DonorTraceabilityResponse:
    return BloodTraceabilityActions(db, hospital_id).get_donor_traceability(donor_id)
