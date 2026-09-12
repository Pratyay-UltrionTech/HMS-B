"""
Target-native Pydantic contracts for Blood Bank Foundation.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from hms_migration.modules.blood_bank.entities.blood_bank_entities import (
    BloodComponentType,
    BloodGroup,
    BloodIssueStatus,
    BloodReturnDisposition,
    BloodTransfusionStatus,
    BloodUnitStatus,
    TransfusionAdverseReactionType,
)


# ---------------------------------------------------------------------------
# Feature 32: Donor & Donation
# ---------------------------------------------------------------------------

class BloodDonorCreate(BaseModel):
    donor_number: str | None = None
    name: str = Field(min_length=1, max_length=255)
    gender: str = Field(min_length=1, max_length=32)
    date_of_birth: date | None = None
    blood_group: BloodGroup
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = None
    is_eligible: bool = True
    deferral_reason: str | None = None
    deferral_until: date | None = None


class BloodDonorResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    donor_number: str
    name: str
    gender: str
    date_of_birth: date | None
    blood_group: BloodGroup
    phone: str | None
    email: str | None
    address: str | None
    last_donation_date: date | None
    is_eligible: bool
    deferral_reason: str | None
    deferral_until: date | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BloodDonationCreate(BaseModel):
    donor_id: UUID
    donation_type: str = Field(default="voluntary", max_length=64)
    bag_type: str = Field(default="triple", max_length=64)
    volume_ml: float = Field(default=450.0, gt=0.0)
    hemoglobin_g_dl: float | None = Field(default=None, ge=0.0)
    blood_pressure: str | None = None
    pulse: int | None = None
    notes: str | None = None


class BloodDonationResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    donation_number: str
    donor_id: UUID
    donation_date: datetime
    donation_type: str
    bag_type: str
    volume_ml: float
    hemoglobin_g_dl: float | None
    blood_pressure: str | None
    pulse: int | None
    collected_by: str
    notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 33: Blood Unit Inventory
# ---------------------------------------------------------------------------

class BloodUnitCreate(BaseModel):
    donation_id: UUID | None = None
    component_type: BloodComponentType = BloodComponentType.whole_blood
    blood_group: BloodGroup
    volume_ml: float = Field(gt=0.0)
    collection_date: datetime
    expiry_date: datetime
    storage_location: str = Field(default="Refrigerated Storage", max_length=128)


class BloodUnitSerologyClear(BaseModel):
    serology_cleared: bool = True
    storage_location: str | None = None


class BloodUnitResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    unit_number: str
    donation_id: UUID | None
    parent_unit_id: UUID | None
    component_type: BloodComponentType
    blood_group: BloodGroup
    volume_ml: float
    collection_date: datetime
    expiry_date: datetime
    storage_location: str
    serology_tested: bool
    serology_cleared: bool
    status: BloodUnitStatus
    reserved_for_patient_id: UUID | None
    reserved_for_admission_id: UUID | None
    reserved_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 34: Component Management & Separation
# ---------------------------------------------------------------------------

class ComponentSpec(BaseModel):
    component_type: BloodComponentType
    volume_ml: float = Field(gt=0.0)
    storage_location: str = Field(default="Blood Bank Refrigerator", max_length=128)
    shelf_life_days: int = Field(gt=0)


class ComponentSeparationRequest(BaseModel):
    method: str = Field(default="centrifugation", max_length=128)
    notes: str | None = None
    components: list[ComponentSpec]


class ComponentSeparationResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    separation_number: str
    parent_unit_id: UUID
    separated_at: datetime
    separated_by: str
    method: str
    notes: str | None
    created_components: list[BloodUnitResponse]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 35: Cross-Match Management
# ---------------------------------------------------------------------------

class CrossMatchCreate(BaseModel):
    blood_unit_id: UUID
    patient_id: UUID
    admission_id: UUID | None = None
    patient_blood_group: BloodGroup
    major_crossmatch_result: str = Field(min_length=1, max_length=32)
    minor_crossmatch_result: str = Field(default="not_performed", max_length=32)
    coombs_test_result: str | None = None
    antibody_screen_result: str | None = None
    is_compatible: bool
    authorized_by: str = Field(min_length=1, max_length=255)
    notes: str | None = None


class CrossMatchResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    cross_match_number: str
    blood_unit_id: UUID
    patient_id: UUID
    admission_id: UUID | None
    patient_blood_group: BloodGroup
    donor_blood_group: BloodGroup
    major_crossmatch_result: str
    minor_crossmatch_result: str
    coombs_test_result: str | None
    antibody_screen_result: str | None
    is_compatible: bool
    tested_by: str
    authorized_by: str
    tested_at: datetime
    notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 36: Blood Issue & Return
# ---------------------------------------------------------------------------

class BloodIssueCreate(BaseModel):
    blood_unit_id: UUID
    patient_id: UUID
    admission_id: UUID | None = None
    requisition_ref: str = Field(min_length=1, max_length=64)
    issued_to: str = Field(min_length=1, max_length=255)
    transport_box_temp_c: float | None = None
    notes: str | None = None


class BloodIssueResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    issue_number: str
    blood_unit_id: UUID
    patient_id: UUID
    admission_id: UUID | None
    requisition_ref: str
    issued_to: str
    issued_by: str
    issued_at: datetime
    transport_box_temp_c: float | None
    status: BloodIssueStatus
    notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class BloodReturnCreate(BaseModel):
    return_reason: str = Field(min_length=1)
    cold_chain_maintained: bool = True
    bag_intact: bool = True
    disposition: BloodReturnDisposition
    disposition_notes: str | None = None


class BloodReturnResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    return_number: str
    issue_id: UUID
    blood_unit_id: UUID
    returned_by: str
    received_by: str
    returned_at: datetime
    return_reason: str
    cold_chain_maintained: bool
    bag_intact: bool
    disposition: BloodReturnDisposition
    disposition_notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 37: Transfusion Record
# ---------------------------------------------------------------------------

class TransfusionStart(BaseModel):
    issue_id: UUID
    verified_by_staff_id: UUID
    pre_transfusion_vitals: str | None = None
    observations: str | None = None


class TransfusionComplete(BaseModel):
    completed_by_staff_id: UUID
    observations: str | None = None


class TransfusionAbort(BaseModel):
    completed_by_staff_id: UUID
    observations: str | None = None


class TransfusionReactionReport(BaseModel):
    completed_by_staff_id: UUID
    adverse_reaction_type: TransfusionAdverseReactionType
    adverse_reaction_details: str = Field(min_length=1)
    observations: str | None = None


class BloodTransfusionResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    transfusion_number: str
    issue_id: UUID
    blood_unit_id: UUID
    patient_id: UUID
    admission_id: UUID | None
    start_time: datetime
    end_time: datetime | None
    verified_by_staff_id: UUID | None
    completed_by_staff_id: UUID | None
    pre_transfusion_vitals: str | None
    observations: str | None
    status: BloodTransfusionStatus
    adverse_reaction_occurred: bool
    adverse_reaction_type: TransfusionAdverseReactionType | None
    adverse_reaction_details: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Feature 38: Blood Traceability (read-only aggregation)
# ---------------------------------------------------------------------------

class UnitTraceabilityResponse(BaseModel):
    unit: BloodUnitResponse
    donor: BloodDonorResponse | None
    donation: BloodDonationResponse | None
    parent_unit: BloodUnitResponse | None
    child_units: list[BloodUnitResponse]
    separation: ComponentSeparationResponse | None = None
    cross_matches: list[CrossMatchResponse]
    issue: BloodIssueResponse | None
    transfusion: BloodTransfusionResponse | None
    return_record: BloodReturnResponse | None

    model_config = {"from_attributes": True}


class DerivedUnitTrace(BaseModel):
    unit: BloodUnitResponse
    issue: BloodIssueResponse | None
    transfusion: BloodTransfusionResponse | None
    return_record: BloodReturnResponse | None


class DonationTrace(BaseModel):
    donation: BloodDonationResponse
    units: list[DerivedUnitTrace]


class DonorTraceabilityResponse(BaseModel):
    donor: BloodDonorResponse
    donations: list[DonationTrace]

    model_config = {"from_attributes": True}
