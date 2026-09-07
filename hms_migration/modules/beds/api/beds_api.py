"""
FastAPI router for Bed Management and Inpatient admissions under /beds and /registration.

Conforms to UltrionTech-Backend-Template modules/beds/api/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session, joinedload

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.beds.actions.bed_actions import (
    GetBedDashboardAction,
    GetOccupancyReportAction,
    GetWardsRoomsCatalogAction,
    ListBedOptionsAction,
    ListRoomsAction,
    ListWardsAction,
)
from hms_migration.modules.beds.contracts.bed_contracts import (
    BedDashboardRow,
    BedOption,
    OccupancyReport,
    RoomOption,
    WardRoomOption,
    WardsRoomsCatalog,
)
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.actions.admission_actions import (
    AdmitPatientAction,
    AllocateBedAction,
    DischargePatientAction,
    ListDischargeRequestsAction,
    RequestDischargeAction,
    TransferBedAction,
    to_admission_detail,
)
from hms_migration.modules.inpatient.actions.registration_admission_actions import (
    RegistrationAdmitAction,
    RegistrationDischargeAction,
)
from hms_migration.modules.inpatient.contracts.inpatient_contracts import (
    AdmissionDetail,
    AdmissionSummary,
    AdmitPatientRequest,
    AdmitRequest,
    AllocateRequest,
    DischargeQueueItem,
    DischargeRequest,
    DischargeRequestCreate,
    DischargeResponse,
    TransferRequest,
)
from hms_migration.modules.inpatient.db.admissions_repository import AdmissionsRepository
from hms_migration.shared.auth.dependencies import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/beds", tags=["beds"])
registration_inpatient_router = APIRouter(prefix="/registration", tags=["registration"])


def _is_doctor(user: HospitalUser) -> bool:
    return bool(user.role and "doctor" in (user.role.name or "").lower())


def _list_staff_doctors(db: Session, hospital_id: UUID) -> list[dict[str, Any]]:
    users = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .all()
    )
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "phone": d.phone,
            "email": d.email,
            "specialization": d.specialization,
            "qualification": d.qualification,
            "medical_registration_number": d.medical_registration_number,
            "years_of_experience": d.years_of_experience,
            "consultation_room": d.consultation_room,
        }
        for d in users
        if _is_doctor(d)
    ]


# ── /beds Endpoints ─────────────────────────────────────────────────────────

@router.get("/dashboard", response_model=list[BedDashboardRow])
def bed_dashboard(
    ward_id: UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetBedDashboardAction(db).execute(hospital_id, ward_id, status_filter)


@router.get("/occupancy", response_model=OccupancyReport)
def occupancy_report(
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return GetOccupancyReportAction(db).execute(hospital_id)


@router.get("/wards", response_model=list[WardRoomOption])
def list_wards(
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListWardsAction(db).execute(hospital_id)


@router.get("/rooms", response_model=list[RoomOption])
def list_rooms(
    ward_id: UUID | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListRoomsAction(db).execute(hospital_id, ward_id)


@router.get("/options", response_model=list[BedOption])
def list_bed_options(
    ward_id: UUID | None = Query(default=None),
    room_id: UUID | None = Query(default=None),
    available_only: bool = Query(default=True),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListBedOptionsAction(db).execute(hospital_id, ward_id, room_id, available_only)


@router.get("/doctors")
def list_beds_doctors(
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return _list_staff_doctors(db, hospital_id)


@router.get("/admissions/active", response_model=list[AdmissionDetail])
def list_active_admissions(
    search: str | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    rows = AdmissionsRepository(db).list_active_admissions(hospital_id, search=search)
    return [to_admission_detail(a) for a in rows]


@router.post("/admit", response_model=AdmissionDetail, status_code=status.HTTP_201_CREATED)
def admit_patient(
    payload: AdmitRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return AdmitPatientAction(db).execute(hospital_id, payload, user)


@router.put("/allocate", response_model=AdmissionDetail)
def allocate_bed(
    payload: AllocateRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return AllocateBedAction(db).execute(hospital_id, payload, user)


@router.put("/transfer", response_model=AdmissionDetail)
def transfer_bed(
    payload: TransferRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return TransferBedAction(db).execute(hospital_id, payload, user)


@router.post("/discharge-request", response_model=AdmissionDetail)
def request_discharge(
    payload: DischargeRequestCreate,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return RequestDischargeAction(db).execute(hospital_id, payload, user)


@router.get("/discharge-requests", response_model=list[DischargeQueueItem])
def list_discharge_requests(
    db: Session = Depends(get_transitional_sync_session),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListDischargeRequestsAction(db).execute(hospital_id)


@router.post("/discharge", response_model=AdmissionDetail)
def discharge_patient(
    payload: DischargeRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return DischargePatientAction(db).execute(hospital_id, payload, user)


# ── /registration Inpatient Endpoints ───────────────────────────────────────

@registration_inpatient_router.get("/beds", response_model=list[BedOption])
def list_available_beds_registration(
    ward_id: UUID | None = Query(default=None),
    room_id: UUID | None = Query(default=None),
    available_only: bool = Query(default=True),
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return ListBedOptionsAction(db).execute(hospital_id, ward_id, room_id, available_only)


@registration_inpatient_router.get("/wards-rooms")
def list_wards_rooms_registration(
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    cat = GetWardsRoomsCatalogAction(db).execute(hospital_id)
    return cat.model_dump()


@registration_inpatient_router.post(
    "/patients/{patient_id}/admit",
    response_model=AdmissionSummary,
    status_code=status.HTTP_201_CREATED,
)
def admit_patient_registration(
    patient_id: UUID,
    payload: AdmitPatientRequest,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return RegistrationAdmitAction(db).execute(hospital_id, patient_id, payload, user)


@registration_inpatient_router.post(
    "/admissions/{admission_id}/discharge", response_model=DischargeResponse
)
def discharge_patient_registration(
    admission_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return RegistrationDischargeAction(db).execute(hospital_id, admission_id, user)


@registration_inpatient_router.get("/doctors")
def list_doctors_registration(
    db: Session = Depends(get_transitional_sync_session),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return _list_staff_doctors(db, hospital_id)
