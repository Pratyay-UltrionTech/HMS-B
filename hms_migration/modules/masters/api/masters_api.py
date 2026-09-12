"""
Target-native Masters API endpoints.

Prefix: /masters
Tags: ["masters"]
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.masters.actions.masters_actions import MastersActions
from hms_migration.modules.masters.contracts.masters_contracts import (
    AppointmentTypeCreate,
    AppointmentTypeResponse,
    AppointmentTypeUpdate,
    ConsultationPricingCreate,
    ConsultationPricingResponse,
    ConsultationPricingUpdate,
    DepartmentCreate,
    DepartmentResponse,
    DepartmentUpdate,
    HolidayCreate,
    HolidayResponse,
    HolidayUpdate,
    OtRoomCreate,
    OtRoomResponse,
    OtRoomUpdate,
    RoomCreate,
    RoomResponse,
    RoomUpdate,
    ShiftTypeCreate,
    ShiftTypeResponse,
    ShiftTypeUpdate,
    SupplierCreate,
    SupplierResponse,
    SupplierUpdate,
    WardCreate,
    WardResponse,
    WardUpdate,
    WingCreate,
    WingResponse,
    WingUpdate,
)
from hms_migration.modules.masters.contracts.insurance_contracts import (
    InsuranceProviderCreate,
    InsuranceProviderResponse,
    InsuranceProviderUpdate,
)
from hms_migration.shared.auth import (
    get_hospital_context,
    require_hospital_admin,
    require_hospital_user,
)

router = APIRouter(prefix="/masters", tags=["masters"])


# ── Wings ──────────────────────────────────────────────────────────────────────
@router.get("/wings", response_model=list[WingResponse])
def list_wings(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[WingResponse]:
    return MastersActions(db, hospital_id).list_wings()


@router.post("/wings", response_model=WingResponse, status_code=status.HTTP_201_CREATED)
def create_wing(
    payload: WingCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> WingResponse:
    return MastersActions(db, hospital_id, actor).create_wing(payload)


@router.put("/wings/{wing_id}", response_model=WingResponse)
def update_wing(
    wing_id: UUID,
    payload: WingUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> WingResponse:
    return MastersActions(db, hospital_id, actor).update_wing(wing_id, payload)


@router.delete("/wings/{wing_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_wing(
    wing_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_wing(wing_id)


# ── Departments ────────────────────────────────────────────────────────────────
@router.get("/departments", response_model=list[DepartmentResponse])
def list_departments(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[DepartmentResponse]:
    return MastersActions(db, hospital_id).list_departments()


@router.post("/departments", response_model=DepartmentResponse, status_code=status.HTTP_201_CREATED)
def create_department(
    payload: DepartmentCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> DepartmentResponse:
    return MastersActions(db, hospital_id, actor).create_department(payload)


@router.put("/departments/{department_id}", response_model=DepartmentResponse)
def update_department(
    department_id: UUID,
    payload: DepartmentUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> DepartmentResponse:
    return MastersActions(db, hospital_id, actor).update_department(department_id, payload)


@router.delete("/departments/{department_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_department(
    department_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_department(department_id)


# ── Shift Types ────────────────────────────────────────────────────────────────
@router.get("/shift-types", response_model=list[ShiftTypeResponse])
def list_shift_types(
    department_id: UUID | None = Query(default=None),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[ShiftTypeResponse]:
    return MastersActions(db, hospital_id).list_shift_types(department_id)


@router.post("/shift-types", response_model=ShiftTypeResponse, status_code=status.HTTP_201_CREATED)
def create_shift_type(
    payload: ShiftTypeCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> ShiftTypeResponse:
    return MastersActions(db, hospital_id, actor).create_shift_type(payload)


@router.put("/shift-types/{shift_id}", response_model=ShiftTypeResponse)
def update_shift_type(
    shift_id: UUID,
    payload: ShiftTypeUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> ShiftTypeResponse:
    return MastersActions(db, hospital_id, actor).update_shift_type(shift_id, payload)


@router.delete("/shift-types/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shift_type(
    shift_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_shift_type(shift_id)


# ── Holidays ───────────────────────────────────────────────────────────────────
@router.get("/holidays", response_model=list[HolidayResponse])
def list_holidays(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[HolidayResponse]:
    return MastersActions(db, hospital_id).list_holidays()


@router.post("/holidays", response_model=HolidayResponse, status_code=status.HTTP_201_CREATED)
def create_holiday(
    payload: HolidayCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> HolidayResponse:
    return MastersActions(db, hospital_id, actor).create_holiday(payload)


@router.put("/holidays/{holiday_id}", response_model=HolidayResponse)
def update_holiday(
    holiday_id: UUID,
    payload: HolidayUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> HolidayResponse:
    return MastersActions(db, hospital_id, actor).update_holiday(holiday_id, payload)


@router.delete("/holidays/{holiday_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_holiday(
    holiday_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_holiday(holiday_id)


# ── Appointment Types ──────────────────────────────────────────────────────────
@router.get("/appointment-types", response_model=list[AppointmentTypeResponse])
def list_appointment_types(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[AppointmentTypeResponse]:
    return MastersActions(db, hospital_id).list_appointment_types()


@router.post("/appointment-types", response_model=AppointmentTypeResponse, status_code=status.HTTP_201_CREATED)
def create_appointment_type(
    payload: AppointmentTypeCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> AppointmentTypeResponse:
    return MastersActions(db, hospital_id, actor).create_appointment_type(payload)


@router.put("/appointment-types/{item_id}", response_model=AppointmentTypeResponse)
def update_appointment_type(
    item_id: UUID,
    payload: AppointmentTypeUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> AppointmentTypeResponse:
    return MastersActions(db, hospital_id, actor).update_appointment_type(item_id, payload)


@router.delete("/appointment-types/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_appointment_type(
    item_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_appointment_type(item_id)


# ── Wards ──────────────────────────────────────────────────────────────────────
@router.get("/wards", response_model=list[WardResponse])
def list_wards(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[WardResponse]:
    return MastersActions(db, hospital_id).list_wards()


@router.post("/wards", response_model=WardResponse, status_code=status.HTTP_201_CREATED)
def create_ward(
    payload: WardCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> WardResponse:
    return MastersActions(db, hospital_id, actor).create_ward(payload)


@router.put("/wards/{ward_id}", response_model=WardResponse)
def update_ward(
    ward_id: UUID,
    payload: WardUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> WardResponse:
    return MastersActions(db, hospital_id, actor).update_ward(ward_id, payload)


@router.delete("/wards/{ward_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ward(
    ward_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_ward(ward_id)


# ── Rooms ──────────────────────────────────────────────────────────────────────
@router.get("/rooms", response_model=list[RoomResponse])
def list_rooms(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[RoomResponse]:
    return MastersActions(db, hospital_id).list_rooms()


@router.post("/rooms", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
def create_room(
    payload: RoomCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> RoomResponse:
    return MastersActions(db, hospital_id, actor).create_room(payload)


@router.put("/rooms/{room_id}", response_model=RoomResponse)
def update_room(
    room_id: UUID,
    payload: RoomUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> RoomResponse:
    return MastersActions(db, hospital_id, actor).update_room(room_id, payload)


@router.delete("/rooms/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_room(
    room_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_room(room_id)


# ── OT Rooms ───────────────────────────────────────────────────────────────────
@router.get("/ot-rooms", response_model=list[OtRoomResponse])
def list_ot_rooms(
    department_id: UUID | None = Query(default=None),
    active_only: bool = False,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[OtRoomResponse]:
    return MastersActions(db, hospital_id).list_ot_rooms(department_id, active_only)


@router.post("/ot-rooms", response_model=OtRoomResponse, status_code=status.HTTP_201_CREATED)
def create_ot_room(
    payload: OtRoomCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> OtRoomResponse:
    return MastersActions(db, hospital_id, actor).create_ot_room(payload)


@router.put("/ot-rooms/{room_id}", response_model=OtRoomResponse)
def update_ot_room(
    room_id: UUID,
    payload: OtRoomUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> OtRoomResponse:
    return MastersActions(db, hospital_id, actor).update_ot_room(room_id, payload)


@router.delete("/ot-rooms/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ot_room(
    room_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_ot_room(room_id)


# ── Suppliers ──────────────────────────────────────────────────────────────────
@router.get("/suppliers", response_model=list[SupplierResponse])
def list_suppliers(
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[SupplierResponse]:
    return MastersActions(db, hospital_id).list_suppliers()


@router.post("/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> SupplierResponse:
    return MastersActions(db, hospital_id, actor).create_supplier(payload)


@router.put("/suppliers/{supplier_id}", response_model=SupplierResponse)
def update_supplier(
    supplier_id: UUID,
    payload: SupplierUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> SupplierResponse:
    return MastersActions(db, hospital_id, actor).update_supplier(supplier_id, payload)


@router.delete("/suppliers/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_supplier(
    supplier_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_supplier(supplier_id)


# ── Consultation Pricing ───────────────────────────────────────────────────────
@router.get("/consultation-pricing", response_model=list[ConsultationPricingResponse])
def list_consultation_pricing(
    wing_id: UUID | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    appointment_type_id: UUID | None = Query(default=None),
    active_only: bool = False,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[ConsultationPricingResponse]:
    return MastersActions(db, hospital_id).list_consultation_pricing(
        wing_id=wing_id,
        department_id=department_id,
        doctor_id=doctor_id,
        appointment_type_id=appointment_type_id,
        active_only=active_only,
    )


@router.post("/consultation-pricing", response_model=ConsultationPricingResponse, status_code=status.HTTP_201_CREATED)
def create_consultation_pricing(
    payload: ConsultationPricingCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> ConsultationPricingResponse:
    return MastersActions(db, hospital_id, actor).create_consultation_pricing(payload)


@router.put("/consultation-pricing/{item_id}", response_model=ConsultationPricingResponse)
def update_consultation_pricing(
    item_id: UUID,
    payload: ConsultationPricingUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> ConsultationPricingResponse:
    return MastersActions(db, hospital_id, actor).update_consultation_pricing(item_id, payload)


@router.delete("/consultation-pricing/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_consultation_pricing(
    item_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_consultation_pricing(item_id)


# ── Insurance & TPA Master ───────────────────────────────────────────────────
@router.get("/insurance", response_model=list[InsuranceProviderResponse])
def list_insurance_providers(
    search: str | None = Query(default=None),
    category: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    _: dict[str, Any] = Depends(require_hospital_user),
) -> list[InsuranceProviderResponse]:
    return MastersActions(db, hospital_id).list_insurance_providers(
        search=search,
        category=category,
        status_filter=status_filter,
    )


@router.post("/insurance", response_model=InsuranceProviderResponse, status_code=status.HTTP_201_CREATED)
def create_insurance_provider(
    payload: InsuranceProviderCreate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> InsuranceProviderResponse:
    return MastersActions(db, hospital_id, actor).create_insurance_provider(payload)


@router.put("/insurance/{item_id}", response_model=InsuranceProviderResponse)
def update_insurance_provider(
    item_id: UUID,
    payload: InsuranceProviderUpdate,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> InsuranceProviderResponse:
    return MastersActions(db, hospital_id, actor).update_insurance_provider(item_id, payload)


@router.delete("/insurance/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_insurance_provider(
    item_id: UUID,
    db: Session = Depends(get_transitional_sync_session),
    hospital_id: UUID = Depends(get_hospital_context),
    actor: dict[str, Any] = Depends(require_hospital_admin),
) -> None:
    MastersActions(db, hospital_id, actor).delete_insurance_provider(item_id)

