"""Read-only contracts for the admission-centric inpatient chart."""

from uuid import UUID

from pydantic import BaseModel

from modules.inpatient.contracts.inpatient_contracts import AdmissionDetail
from modules.inpatient.contracts.nursing_contracts import (
    IpdClinicalNoteResponse,
    IpdIntakeOutputSummary,
    IpdVitalSignResponse,
    MedicationAdminResponse,
    NursingCarePlanResponse,
    NursingShiftHandoverResponse,
)


class BedStayHistoryItem(BaseModel):
    id: UUID
    ward_id: UUID | None
    room_id: UUID | None
    bed_id: UUID | None
    rate_per_day: float
    started_at: object
    ended_at: object | None

    model_config = {"from_attributes": True}


class AdmissionChartResponse(BaseModel):
    admission: AdmissionDetail
    doctor_notes: list[IpdClinicalNoteResponse]
    nursing_notes: list[IpdClinicalNoteResponse]
    care_plans: list[NursingCarePlanResponse]
    handovers: list[NursingShiftHandoverResponse]
    medication_administrations: list[MedicationAdminResponse]
    bed_history: list[BedStayHistoryItem]
    vitals_history: list[IpdVitalSignResponse] = []
    intake_output_summary: IpdIntakeOutputSummary | None = None
    latest_vitals: list[dict] = []
    care_team: list[dict] = []
    medication_orders: list[dict] = []
    surgeries: list[dict] = []
    financial_clearance: dict | None = None
