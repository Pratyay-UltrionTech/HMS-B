"""
Target-native action implementations for the Masters domain.

Handles:
- Wings
- Departments
- Shift Types
- Holidays
- Appointment Types
- Consultation Pricing
- Wards
- Rooms
- OT Rooms
- Suppliers
"""

from __future__ import annotations

from datetime import time
from functools import wraps
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from modules.appointments.entities.appointment_type import AppointmentType
from modules.beds.entities.bed import Room, Ward
from modules.billing.entities.billing_entities import ConsultationPricing
from modules.doctors.entities.doctor import Holiday, HospitalUser, ShiftType
from modules.masters.contracts.masters_contracts import (
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
from modules.masters.entities.organization_entities import Department, Supplier, Wing
from modules.masters.entities.insurance_entities import InsuranceProvider
from modules.masters.contracts.insurance_contracts import (
    InsuranceProviderCreate,
    InsuranceProviderResponse,
    InsuranceProviderUpdate,
)
from modules.ot.entities.ot_entities import OtRoom
from shared.audit import write_audit_log
from shared.sync.events import stage_sync_event
from modules.masters.actions.integrity import assert_unreferenced, reconcile_room_beds, delete_room_inventory
from modules.doctors.services.doctor_eligibility import is_eligible_doctor


def parse_time(value: time | str) -> time:
    if isinstance(value, time):
        return value
    parts = value.split(":")
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


def master_mutation(entity_type: str, action: str):
    """Stage notifications on commit hooks and roll back failed writes."""
    def decorate(method):
        @wraps(method)
        def mutate(self, *args, **kwargs):
            stage_sync_event(self.db, self.hospital_id, entity_type, action)
            if entity_type == 'room':
                stage_sync_event(self.db, self.hospital_id, 'bed', action)
            try:
                return method(self, *args, **kwargs)
            except IntegrityError:
                self.db.rollback()
                raise HTTPException(409, "Master change conflicts with an existing code or a dependent record.") from None
            except Exception:
                self.db.rollback()
                raise
        return mutate
    return decorate


class MastersActions:
    def __init__(self, db: Session, hospital_id: UUID, actor: dict[str, Any] | None = None) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.actor = actor or {}

    def _audit(self, action: str, entity_type: str, entity_id: Any, summary: str, details: dict | None = None) -> None:
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            details=details,
        )

    def _get_or_404(self, model: type, item_id: UUID, label: str):
        item = self.db.query(model).filter(model.id == item_id, model.hospital_id == self.hospital_id).populate_existing().with_for_update().first()
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")
        return item

    def _validate_location(self, wing_id: UUID | None, department_id: UUID | None, require_active: bool = False) -> None:
        if wing_id is not None:
            wing = self._get_or_404(Wing, wing_id, "Wing")
            if require_active and not wing.is_active:
                raise HTTPException(status_code=400, detail="Referenced Wing is inactive.")
        if department_id is not None:
            department = self._get_or_404(Department, department_id, "Department")
            if require_active and not department.is_active:
                raise HTTPException(status_code=400, detail="Referenced Department is inactive.")
            if wing_id is not None and department.wing_id != wing_id:
                raise HTTPException(422, "Department does not belong to selected Wing.")

    def _apply_updates(self, item, data: dict) -> None:
        for key, value in data.items():
            column = item.__table__.columns.get(key)
            if value is None and column is not None and not column.nullable:
                raise HTTPException(422, f"{key} cannot be null")
            setattr(item, key, value)

    # ── Wings ─────────────────────────────────────────────────────────────────
    def list_wings(self) -> list[WingResponse]:
        return self.db.query(Wing).filter(Wing.hospital_id == self.hospital_id).order_by(Wing.name).all()

    @master_mutation("wing", "create")
    def create_wing(self, payload: WingCreate) -> WingResponse:
        wing = Wing(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(wing)
        self.db.flush()
        self._audit("create", "wing", wing.id, f"Created wing '{wing.name}'")
        self.db.commit()
        self.db.refresh(wing)
        return wing

    @master_mutation("wing", "update")
    def update_wing(self, wing_id: UUID, payload: WingUpdate) -> WingResponse:
        wing = self._get_or_404(Wing, wing_id, "Wing")
        original_is_active = wing.is_active
        data = payload.model_dump(exclude_unset=True)
        target_is_active = data.get("is_active", wing.is_active)
        if original_is_active and not target_is_active:
            active_deps = self.db.query(Department.id).filter(Department.wing_id == wing.id, Department.is_active.is_(True)).count()
            active_wards = self.db.query(Ward.id).filter(Ward.wing_id == wing.id, Ward.is_active.is_(True)).count()
            active_ots = self.db.query(OtRoom.id).filter(OtRoom.wing_id == wing.id, OtRoom.is_active.is_(True)).count()
            active_pricing = self.db.query(ConsultationPricing.id).filter(ConsultationPricing.wing_id == wing.id, ConsultationPricing.is_active.is_(True)).count()
            if active_deps > 0 or active_wards > 0 or active_ots > 0 or active_pricing > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot deactivate Wing '{wing.name}' while active operational departments, wards, OT rooms, or pricing depend on it.",
                )
        self._apply_updates(wing, data)
        self.db.flush()
        self._audit("update", "wing", wing.id, f"Updated wing '{wing.name}'")
        self.db.commit()
        self.db.refresh(wing)
        return wing

    @master_mutation("wing", "delete")
    def delete_wing(self, wing_id: UUID) -> None:
        wing = self._get_or_404(Wing, wing_id, "Wing")
        name = wing.name
        assert_unreferenced(self.db, wing)
        self.db.delete(wing)
        self._audit("delete", "wing", wing_id, f"Deleted wing '{name}'")
        self.db.commit()

    # ── Departments ───────────────────────────────────────────────────────────
    def list_departments(self) -> list[DepartmentResponse]:
        rows = (
            self.db.query(Department, Wing.name)
            .outerjoin(Wing, Department.wing_id == Wing.id)
            .filter(Department.hospital_id == self.hospital_id)
            .order_by(Wing.name, Department.name)
            .all()
        )
        result = []
        for dept, wing_name in rows:
            data = DepartmentResponse.model_validate(dept)
            data.wing_name = wing_name
            result.append(data)
        return result

    @master_mutation("department", "create")
    def create_department(self, payload: DepartmentCreate) -> DepartmentResponse:
        wing = self._get_or_404(Wing, payload.wing_id, "Wing")
        if payload.is_active and not wing.is_active:
            raise HTTPException(status_code=400, detail="Cannot attach active Department to an inactive Wing.")
        dept = Department(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(dept)
        self.db.flush()
        self._audit("create", "department", dept.id, f"Created department '{dept.name}'")
        self.db.commit()
        self.db.refresh(dept)
        wing = self.db.query(Wing).filter(Wing.id == dept.wing_id).first()
        res = DepartmentResponse.model_validate(dept)
        res.wing_name = wing.name if wing else None
        return res

    @master_mutation("department", "update")
    def update_department(self, department_id: UUID, payload: DepartmentUpdate) -> DepartmentResponse:
        dept = self._get_or_404(Department, department_id, "Department")
        original_is_active = dept.is_active
        data = payload.model_dump(exclude_unset=True)
        target_is_active = data.get("is_active", dept.is_active)
        target_wing_id = data.get("wing_id", dept.wing_id)
        if target_is_active and target_wing_id:
            wing = self._get_or_404(Wing, target_wing_id, "Wing")
            if not wing.is_active:
                raise HTTPException(status_code=400, detail="Cannot attach active Department to an inactive Wing.")
        if "wing_id" in data and data["wing_id"] != dept.wing_id:
            for model in (Ward, OtRoom, ConsultationPricing):
                if self.db.query(model.id).filter(model.department_id == department_id, model.wing_id.is_not(None), model.wing_id != data["wing_id"] if data["wing_id"] is not None else model.wing_id.is_not(None)).first():
                    raise HTTPException(409, "Cannot move Department while linked wards, OT rooms or pricing use another Wing.")
        if original_is_active and not target_is_active:
            from modules.doctors.entities.doctor import ShiftType, HospitalUser
            from modules.admin.entities.admin_entities import HospitalUserDepartment
            active_shifts = self.db.query(ShiftType.id).filter(ShiftType.department_id == dept.id, ShiftType.is_active.is_(True)).count()
            active_wards = self.db.query(Ward.id).filter(Ward.department_id == dept.id, Ward.is_active.is_(True)).count()
            active_ots = self.db.query(OtRoom.id).filter(OtRoom.department_id == dept.id, OtRoom.is_active.is_(True)).count()
            active_pricing = self.db.query(ConsultationPricing.id).filter(ConsultationPricing.department_id == dept.id, ConsultationPricing.is_active.is_(True)).count()
            active_staff = self.db.query(HospitalUser.id).filter(HospitalUser.department_id == dept.id, HospitalUser.is_active.is_(True)).count()
            active_sec_staff = self.db.query(HospitalUserDepartment.user_id).join(HospitalUser, HospitalUserDepartment.user_id == HospitalUser.id).filter(HospitalUserDepartment.department_id == dept.id, HospitalUser.is_active.is_(True)).count()
            if active_shifts > 0 or active_wards > 0 or active_ots > 0 or active_pricing > 0 or active_staff > 0 or active_sec_staff > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot deactivate Department '{dept.name}' while active operational shifts, wards, OT rooms, pricing, or staff assignments depend on it.",
                )
        self._apply_updates(dept, data)
        self.db.flush()
        self._audit("update", "department", dept.id, f"Updated department '{dept.name}'")
        self.db.commit()
        self.db.refresh(dept)
        wing = self.db.query(Wing).filter(Wing.id == dept.wing_id).first() if dept.wing_id else None
        res = DepartmentResponse.model_validate(dept)
        res.wing_name = wing.name if wing else None
        return res

    @master_mutation("department", "delete")
    def delete_department(self, department_id: UUID) -> None:
        dept = self._get_or_404(Department, department_id, "Department")
        name = dept.name
        assert_unreferenced(self.db, dept)
        self.db.delete(dept)
        self._audit("delete", "department", department_id, f"Deleted department '{name}'")
        self.db.commit()

    # ── Shift Types ───────────────────────────────────────────────────────────
    def list_shift_types(self, department_id: UUID | None = None) -> list[ShiftTypeResponse]:
        query = (
            self.db.query(ShiftType, Department.name)
            .outerjoin(Department, ShiftType.department_id == Department.id)
            .filter(ShiftType.hospital_id == self.hospital_id)
        )
        if department_id is not None:
            query = query.filter(ShiftType.department_id == department_id)
        rows = query.order_by(ShiftType.start_time).all()
        result = []
        for shift, dept_name in rows:
            data = ShiftTypeResponse.model_validate(shift)
            data.department_name = dept_name
            result.append(data)
        return result

    @master_mutation("shift_type", "create")
    def create_shift_type(self, payload: ShiftTypeCreate) -> ShiftTypeResponse:
        dept = self._get_or_404(Department, payload.department_id, "Department")
        if payload.is_active and not dept.is_active:
            raise HTTPException(status_code=400, detail="Cannot attach active ShiftType to an inactive Department.")
        data = payload.model_dump()
        data["start_time"] = parse_time(data["start_time"])
        data["end_time"] = parse_time(data["end_time"])
        shift = ShiftType(hospital_id=self.hospital_id, **data)
        self.db.add(shift)
        self.db.flush()
        self._audit("create", "shift_type", shift.id, f"Created shift '{shift.name}'")
        self.db.commit()
        self.db.refresh(shift)
        dept = self.db.query(Department).filter(Department.id == shift.department_id).first()
        res = ShiftTypeResponse.model_validate(shift)
        res.department_name = dept.name if dept else None
        return res

    @master_mutation("shift_type", "update")
    def update_shift_type(self, shift_id: UUID, payload: ShiftTypeUpdate) -> ShiftTypeResponse:
        shift = self._get_or_404(ShiftType, shift_id, "Shift type")
        original_is_active = shift.is_active
        if payload.department_id is not None and payload.department_id != shift.department_id:
            assert_unreferenced(self.db, shift, action='move')
        if payload.department_id is not None:
            self._get_or_404(Department, payload.department_id, "Department")
        data = payload.model_dump(exclude_unset=True)
        target_is_active = data.get("is_active", shift.is_active)
        target_dept_id = data.get("department_id", shift.department_id)
        if target_is_active and target_dept_id:
            dept = self._get_or_404(Department, target_dept_id, "Department")
            if not dept.is_active:
                raise HTTPException(status_code=400, detail="Cannot attach active ShiftType to an inactive Department.")
        if original_is_active and not target_is_active:
            active_staff_count = self.db.query(HospitalUser.id).filter(
                HospitalUser.shift_id == shift.id,
                HospitalUser.hospital_id == self.hospital_id,
                HospitalUser.is_active.is_(True),
            ).count()
            if active_staff_count > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot deactivate this shift while it is the default shift for {active_staff_count} active staff member{'s' if active_staff_count > 1 else ''}. Reassign those staff first.",
                )
            from datetime import date, datetime, time as dt_time
            from modules.doctors.entities.doctor import StaffDailyShift, StaffRosterSegment, StaffWorkPatternSegment
            future_roster = self.db.query(StaffDailyShift.id).filter(
                StaffDailyShift.shift_id == shift.id,
                StaffDailyShift.hospital_id == self.hospital_id,
                StaffDailyShift.roster_date >= date.today(),
                StaffDailyShift.status == "on_duty",
            ).count()
            future_segments = self.db.query(StaffRosterSegment.id).filter(
                StaffRosterSegment.shift_type_id == shift.id,
                StaffRosterSegment.hospital_id == self.hospital_id,
                StaffRosterSegment.start_at >= datetime.combine(date.today(), dt_time.min),
            ).count()
            pattern_segments = self.db.query(StaffWorkPatternSegment.id).filter(
                StaffWorkPatternSegment.shift_type_id == shift.id,
                StaffWorkPatternSegment.hospital_id == self.hospital_id,
            ).count()
            total_refs = future_roster + future_segments + pattern_segments
            if total_refs > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot deactivate this shift while {total_refs} current or future roster assignment(s) or work pattern(s) reference it.",
                )
        if "start_time" in data and data["start_time"] is not None:
            data["start_time"] = parse_time(data["start_time"])
        if "end_time" in data and data["end_time"] is not None:
            data["end_time"] = parse_time(data["end_time"])
        self._apply_updates(shift, data)
        self.db.flush()
        self._audit("update", "shift_type", shift.id, f"Updated shift '{shift.name}'")
        self.db.commit()
        self.db.refresh(shift)
        dept = self.db.query(Department).filter(Department.id == shift.department_id).first()
        res = ShiftTypeResponse.model_validate(shift)
        res.department_name = dept.name if dept else None
        return res

    @master_mutation("shift_type", "delete")
    def delete_shift_type(self, shift_id: UUID) -> None:
        shift = self._get_or_404(ShiftType, shift_id, "Shift type")
        name = shift.name
        assert_unreferenced(self.db, shift)
        self.db.delete(shift)
        self._audit("delete", "shift_type", shift_id, f"Deleted shift '{name}'")
        self.db.commit()

    # ── Holidays ──────────────────────────────────────────────────────────────
    def list_holidays(self) -> list[HolidayResponse]:
        return self.db.query(Holiday).filter(Holiday.hospital_id == self.hospital_id).order_by(Holiday.holiday_date).all()

    @master_mutation("holiday", "create")
    def create_holiday(self, payload: HolidayCreate) -> HolidayResponse:
        holiday = Holiday(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(holiday)
        self.db.flush()
        self._audit("create", "holiday", holiday.id, f"Created holiday '{holiday.name}'")
        self.db.commit()
        self.db.refresh(holiday)
        return holiday

    @master_mutation("holiday", "update")
    def update_holiday(self, holiday_id: UUID, payload: HolidayUpdate) -> HolidayResponse:
        holiday = self._get_or_404(Holiday, holiday_id, "Holiday")
        self._apply_updates(holiday, payload.model_dump(exclude_unset=True))
        self.db.flush()
        self._audit("update", "holiday", holiday.id, f"Updated holiday '{holiday.name}'")
        self.db.commit()
        self.db.refresh(holiday)
        return holiday

    @master_mutation("holiday", "delete")
    def delete_holiday(self, holiday_id: UUID) -> None:
        holiday = self._get_or_404(Holiday, holiday_id, "Holiday")
        name = holiday.name
        assert_unreferenced(self.db, holiday)
        self.db.delete(holiday)
        self._audit("delete", "holiday", holiday_id, f"Deleted holiday '{name}'")
        self.db.commit()

    # ── Appointment Types ─────────────────────────────────────────────────────
    def list_appointment_types(self) -> list[AppointmentTypeResponse]:
        return (
            self.db.query(AppointmentType)
            .filter(AppointmentType.hospital_id == self.hospital_id)
            .order_by(AppointmentType.name)
            .all()
        )

    @master_mutation("appointment_type", "create")
    def create_appointment_type(self, payload: AppointmentTypeCreate) -> AppointmentTypeResponse:
        item = AppointmentType(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(item)
        self.db.flush()
        self._audit("create", "appointment_type", item.id, f"Created appointment type '{item.name}'")
        self.db.commit()
        self.db.refresh(item)
        return item

    @master_mutation("appointment_type", "update")
    def update_appointment_type(self, item_id: UUID, payload: AppointmentTypeUpdate) -> AppointmentTypeResponse:
        item = self._get_or_404(AppointmentType, item_id, "Appointment type")
        original_is_active = item.is_active
        data = payload.model_dump(exclude_unset=True)
        target_is_active = data.get("is_active", item.is_active)
        if original_is_active and not target_is_active:
            active_pricing = self.db.query(ConsultationPricing.id).filter(
                ConsultationPricing.appointment_type_id == item.id,
                ConsultationPricing.hospital_id == self.hospital_id,
                ConsultationPricing.is_active.is_(True),
            ).count()
            if active_pricing > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot deactivate Appointment Type '{item.name}' while {active_pricing} active consultation pricing record(s) depend on it.",
                )
        self._apply_updates(item, data)
        self.db.flush()
        self._audit("update", "appointment_type", item.id, f"Updated appointment type '{item.name}'")
        self.db.commit()
        self.db.refresh(item)
        return item

    @master_mutation("appointment_type", "delete")
    def delete_appointment_type(self, item_id: UUID) -> None:
        item = self._get_or_404(AppointmentType, item_id, "Appointment type")
        name = item.name
        assert_unreferenced(self.db, item)
        self.db.delete(item)
        self._audit("delete", "appointment_type", item_id, f"Deleted appointment type '{name}'")
        self.db.commit()

    # ── Wards ─────────────────────────────────────────────────────────────────
    def list_wards(self) -> list[WardResponse]:
        rows = (
            self.db.query(Ward, Wing.name, Department.name)
            .outerjoin(Wing, Ward.wing_id == Wing.id)
            .outerjoin(Department, Ward.department_id == Department.id)
            .filter(Ward.hospital_id == self.hospital_id)
            .order_by(Ward.name)
            .all()
        )
        result = []
        for ward, wing_name, dept_name in rows:
            data = WardResponse.model_validate(ward)
            data.wing_name = wing_name
            data.department_name = dept_name
            result.append(data)
        return result

    @master_mutation("ward", "create")
    def create_ward(self, payload: WardCreate) -> WardResponse:
        self._validate_location(payload.wing_id, payload.department_id, require_active=payload.is_active)
        ward = Ward(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(ward)
        self.db.flush()
        self._audit("create", "ward", ward.id, f"Created ward '{ward.name}'")
        self.db.commit()
        self.db.refresh(ward)
        data = WardResponse.model_validate(ward)
        if ward.wing_id:
            wing = self.db.query(Wing).filter(Wing.id == ward.wing_id).first()
            data.wing_name = wing.name if wing else None
        if ward.department_id:
            dept = self.db.query(Department).filter(Department.id == ward.department_id).first()
            data.department_name = dept.name if dept else None
        return data

    @master_mutation("ward", "update")
    def update_ward(self, ward_id: UUID, payload: WardUpdate) -> WardResponse:
        ward = self._get_or_404(Ward, ward_id, "Ward")
        original_is_active = ward.is_active
        updates = payload.model_dump(exclude_unset=True)
        target_is_active = updates.get("is_active", ward.is_active)
        target_wing = updates.get("wing_id", ward.wing_id)
        target_dept = updates.get("department_id", ward.department_id)
        self._validate_location(target_wing, target_dept, require_active=target_is_active)
        if updates.get("wing_id"):
            self._get_or_404(Wing, updates["wing_id"], "Wing")
        if updates.get("department_id"):
            self._get_or_404(Department, updates["department_id"], "Department")
        if original_is_active and not target_is_active:
            active_rooms = self.db.query(Room.id).filter(
                Room.ward_id == ward.id,
                Room.hospital_id == self.hospital_id,
                Room.is_active.is_(True),
            ).count()
            if active_rooms > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot deactivate Ward '{ward.name}' while it contains {active_rooms} active room(s). Deactivate rooms first.",
                )
            from modules.beds.entities.bed import Bed
            occupied_beds = self.db.query(Bed.id).filter(
                Bed.ward_id == ward.id,
                Bed.hospital_id == self.hospital_id,
                Bed.is_occupied.is_(True),
            ).count()
            if occupied_beds > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot deactivate Ward '{ward.name}' while {occupied_beds} bed(s) are occupied.",
                )
        self._apply_updates(ward, updates)
        self.db.flush()
        self._audit("update", "ward", ward.id, f"Updated ward '{ward.name}'")
        self.db.commit()
        self.db.refresh(ward)
        data = WardResponse.model_validate(ward)
        if ward.wing_id:
            wing = self.db.query(Wing).filter(Wing.id == ward.wing_id).first()
            data.wing_name = wing.name if wing else None
        if ward.department_id:
            dept = self.db.query(Department).filter(Department.id == ward.department_id).first()
            data.department_name = dept.name if dept else None
        return data

    @master_mutation("ward", "delete")
    def delete_ward(self, ward_id: UUID) -> None:
        ward = self._get_or_404(Ward, ward_id, "Ward")
        name = ward.name
        assert_unreferenced(self.db, ward)
        self.db.delete(ward)
        self._audit("delete", "ward", ward_id, f"Deleted ward '{name}'")
        self.db.commit()

    # ── Rooms ─────────────────────────────────────────────────────────────────
    def list_rooms(self) -> list[RoomResponse]:
        rows = (
            self.db.query(Room, Ward.name)
            .outerjoin(Ward, Room.ward_id == Ward.id)
            .filter(Room.hospital_id == self.hospital_id)
            .order_by(Room.room_code)
            .all()
        )
        result = []
        for room, ward_name in rows:
            data = RoomResponse.model_validate(room)
            data.ward_name = ward_name
            result.append(data)
        return result

    @master_mutation("room", "create")
    def create_room(self, payload: RoomCreate) -> RoomResponse:
        ward = self._get_or_404(Ward, payload.ward_id, "Ward")
        if payload.is_active and not ward.is_active:
            raise HTTPException(status_code=400, detail="Cannot activate Room under an inactive Ward.")
        room = Room(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(room)
        self.db.flush()
        reconcile_room_beds(self.db, room, original_is_active=False)
        self._audit("create", "room", room.id, f"Created room '{room.room_code}'")
        self.db.commit()
        self.db.refresh(room)
        ward = self.db.query(Ward).filter(Ward.id == room.ward_id).first()
        data = RoomResponse.model_validate(room)
        data.ward_name = ward.name if ward else None
        return data

    @master_mutation("room", "update")
    def update_room(self, room_id: UUID, payload: RoomUpdate) -> RoomResponse:
        room = self._get_or_404(Room, room_id, "Room")
        previous_ward_id = room.ward_id
        original_is_active = room.is_active
        if payload.ward_id is not None:
            ward = self._get_or_404(Ward, payload.ward_id, "Ward")
            if payload.is_active is not False and not ward.is_active:
                raise HTTPException(status_code=400, detail="Cannot attach Room to an inactive Ward.")
        self._apply_updates(room, payload.model_dump(exclude_unset=True))
        reconcile_room_beds(self.db, room, previous_ward_id=previous_ward_id, original_is_active=original_is_active)
        self.db.flush()
        self._audit("update", "room", room.id, f"Updated room '{room.room_code}'")
        self.db.commit()
        self.db.refresh(room)
        ward = self.db.query(Ward).filter(Ward.id == room.ward_id).first()
        data = RoomResponse.model_validate(room)
        data.ward_name = ward.name if ward else None
        return data

    @master_mutation("room", "delete")
    def delete_room(self, room_id: UUID) -> None:
        room = self._get_or_404(Room, room_id, "Room")
        code = room.room_code
        delete_room_inventory(self.db, room)
        self.db.delete(room)
        self._audit("delete", "room", room_id, f"Deleted room '{code}'")
        self.db.commit()

    # ── OT Rooms ──────────────────────────────────────────────────────────────
    def _ot_room_response(self, room: OtRoom, wing_name: str | None = None, dept_name: str | None = None) -> OtRoomResponse:
        data = OtRoomResponse.model_validate(room)
        if wing_name is not None:
            data.wing_name = wing_name
        elif room.wing_id:
            wing = self.db.query(Wing).filter(Wing.id == room.wing_id).first()
            data.wing_name = wing.name if wing else None
        if dept_name is not None:
            data.department_name = dept_name
        else:
            dept = self.db.query(Department).filter(Department.id == room.department_id).first()
            data.department_name = dept.name if dept else None
        return data

    def list_ot_rooms(self, department_id: UUID | None = None, active_only: bool = False) -> list[OtRoomResponse]:
        q = (
            self.db.query(OtRoom)
            .options(joinedload(OtRoom.wing), joinedload(OtRoom.department))
            .filter(OtRoom.hospital_id == self.hospital_id)
        )
        if department_id is not None:
            q = q.filter(OtRoom.department_id == department_id)
        if active_only:
            q = q.filter(OtRoom.is_active.is_(True))
        rooms = q.order_by(OtRoom.code).all()
        result = []
        for room in rooms:
            data = OtRoomResponse.model_validate(room)
            data.wing_name = room.wing.name if room.wing else None
            data.department_name = room.department.name if room.department else None
            result.append(data)
        return result

    @master_mutation("ot_room", "create")
    def create_ot_room(self, payload: OtRoomCreate) -> OtRoomResponse:
        self._validate_location(payload.wing_id, payload.department_id, require_active=payload.is_active)
        code = payload.code.strip().upper()
        exists = (
            self.db.query(OtRoom.id)
            .filter(OtRoom.hospital_id == self.hospital_id, OtRoom.code == code)
            .first()
        )
        if exists:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="OT room code already exists")
        room = OtRoom(
            hospital_id=self.hospital_id,
            wing_id=payload.wing_id,
            department_id=payload.department_id,
            code=code,
            name=payload.name.strip(),
            description=payload.description.strip() if payload.description else None,
            head_name=payload.head_name,
            desk_phone=payload.desk_phone,
            mobile=payload.mobile,
            base_ot_charge=payload.base_ot_charge,
            is_active=payload.is_active,
        )
        self.db.add(room)
        self.db.flush()
        self._audit("create", "ot_room", room.id, f"Created OT room '{room.code}'")
        self.db.commit()
        self.db.refresh(room)
        return self._ot_room_response(room)

    @master_mutation("ot_room", "update")
    def update_ot_room(self, room_id: UUID, payload: OtRoomUpdate) -> OtRoomResponse:
        room = self._get_or_404(OtRoom, room_id, "OT room")
        updates = payload.model_dump(exclude_unset=True)
        self._validate_location(updates.get("wing_id", room.wing_id), updates.get("department_id", room.department_id), require_active=updates.get("is_active", room.is_active))
        if "wing_id" in updates and updates["wing_id"] is not None:
            self._get_or_404(Wing, updates["wing_id"], "Wing")
        if updates.get("department_id"):
            self._get_or_404(Department, updates["department_id"], "Department")
        if "code" in updates and updates["code"] is not None:
            updates["code"] = str(updates["code"]).strip().upper()
            clash = (
                self.db.query(OtRoom.id)
                .filter(
                    OtRoom.hospital_id == self.hospital_id,
                    OtRoom.code == updates["code"],
                    OtRoom.id != room_id,
                )
                .first()
            )
            if clash:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="OT room code already exists")
        if "name" in updates and updates["name"] is not None:
            updates["name"] = str(updates["name"]).strip()
        if "description" in updates and isinstance(updates["description"], str):
            updates["description"] = updates["description"].strip() or None
        self._apply_updates(room, updates)
        self._audit("update", "ot_room", room.id, f"Updated OT room '{room.code}'")
        self.db.commit()
        self.db.refresh(room)
        return self._ot_room_response(room)

    @master_mutation("ot_room", "delete")
    def delete_ot_room(self, room_id: UUID) -> None:
        room = self._get_or_404(OtRoom, room_id, "OT room")
        code = room.code
        assert_unreferenced(self.db, room)
        self.db.delete(room)
        self._audit("delete", "ot_room", room_id, f"Deleted OT room '{code}'")
        self.db.commit()

    # ── Suppliers ─────────────────────────────────────────────────────────────
    def list_suppliers(self) -> list[SupplierResponse]:
        return self.db.query(Supplier).filter(Supplier.hospital_id == self.hospital_id).order_by(Supplier.name).all()

    @master_mutation("supplier", "create")
    def create_supplier(self, payload: SupplierCreate) -> SupplierResponse:
        supplier = Supplier(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(supplier)
        self.db.flush()
        self._audit("create", "supplier", supplier.id, f"Created supplier '{supplier.name}'")
        self.db.commit()
        self.db.refresh(supplier)
        return supplier

    @master_mutation("supplier", "update")
    def update_supplier(self, supplier_id: UUID, payload: SupplierUpdate) -> SupplierResponse:
        supplier = self._get_or_404(Supplier, supplier_id, "Supplier")
        self._apply_updates(supplier, payload.model_dump(exclude_unset=True))
        self.db.flush()
        self._audit("update", "supplier", supplier.id, f"Updated supplier '{supplier.name}'")
        self.db.commit()
        self.db.refresh(supplier)
        return supplier

    @master_mutation("supplier", "delete")
    def delete_supplier(self, supplier_id: UUID) -> None:
        supplier = self._get_or_404(Supplier, supplier_id, "Supplier")
        name = supplier.name
        assert_unreferenced(self.db, supplier)
        self.db.delete(supplier)
        self._audit("delete", "supplier", supplier_id, f"Deleted supplier '{name}'")
        self.db.commit()

    # ── Consultation Pricing ──────────────────────────────────────────────────
    def _validate_pricing_refs(
        self,
        wing_id: UUID,
        department_id: UUID,
        doctor_id: UUID,
        appointment_type_id: UUID,
        is_active: bool = True,
    ) -> None:
        self._validate_location(wing_id, department_id, require_active=is_active)
        doctor = self._get_or_404(HospitalUser, doctor_id, "Doctor")
        if is_active and not doctor.is_active:
            raise HTTPException(status_code=400, detail="Active pricing requires an active Doctor.")
        if not is_eligible_doctor(self.db, self.hospital_id, doctor_id):
            raise HTTPException(422, "Selected staff member is not an active eligible Doctor.")
        appt_type = self._get_or_404(AppointmentType, appointment_type_id, "Appointment type")
        if is_active and not appt_type.is_active:
            raise HTTPException(status_code=400, detail="Active pricing requires an active Appointment Type.")

    def _pricing_to_response(
        self,
        row: ConsultationPricing,
        wing_name: str | None = None,
        dept_name: str | None = None,
        doctor_name: str | None = None,
        type_name: str | None = None,
    ) -> ConsultationPricingResponse:
        data = ConsultationPricingResponse.model_validate(row)
        data.wing_name = wing_name
        data.department_name = dept_name
        data.doctor_name = doctor_name
        data.appointment_type_name = type_name
        return data

    def list_consultation_pricing(
        self,
        wing_id: UUID | None = None,
        department_id: UUID | None = None,
        doctor_id: UUID | None = None,
        appointment_type_id: UUID | None = None,
        active_only: bool = False,
    ) -> list[ConsultationPricingResponse]:
        q = (
            self.db.query(ConsultationPricing, Wing.name, Department.name, HospitalUser.name, AppointmentType.name)
            .join(Wing, ConsultationPricing.wing_id == Wing.id)
            .join(Department, ConsultationPricing.department_id == Department.id)
            .join(HospitalUser, ConsultationPricing.doctor_id == HospitalUser.id)
            .join(AppointmentType, ConsultationPricing.appointment_type_id == AppointmentType.id)
            .filter(ConsultationPricing.hospital_id == self.hospital_id)
        )
        if wing_id:
            q = q.filter(ConsultationPricing.wing_id == wing_id)
        if department_id:
            q = q.filter(ConsultationPricing.department_id == department_id)
        if doctor_id:
            q = q.filter(ConsultationPricing.doctor_id == doctor_id)
        if appointment_type_id:
            q = q.filter(ConsultationPricing.appointment_type_id == appointment_type_id)
        if active_only:
            q = q.filter(ConsultationPricing.is_active.is_(True))
        rows = q.order_by(HospitalUser.name, AppointmentType.name).all()
        return [
            self._pricing_to_response(row, wing_name, dept_name, doctor_name, type_name)
            for row, wing_name, dept_name, doctor_name, type_name in rows
        ]

    @master_mutation("consultation_pricing", "create")
    def create_consultation_pricing(self, payload: ConsultationPricingCreate) -> ConsultationPricingResponse:
        self._validate_pricing_refs(
            payload.wing_id,
            payload.department_id,
            payload.doctor_id,
            payload.appointment_type_id,
            is_active=payload.is_active,
        )
        existing = (
            self.db.query(ConsultationPricing)
            .filter(
                ConsultationPricing.hospital_id == self.hospital_id,
                ConsultationPricing.wing_id == payload.wing_id,
                ConsultationPricing.department_id == payload.department_id,
                ConsultationPricing.doctor_id == payload.doctor_id,
                ConsultationPricing.appointment_type_id == payload.appointment_type_id,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Pricing already exists for this wing, department, doctor, and appointment type",
            )
        item = ConsultationPricing(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(item)
        self.db.flush()
        self._audit("create", "consultation_pricing", item.id, f"Created consultation pricing fee={payload.consultation_fee}")
        self.db.commit()
        self.db.refresh(item)
        wing = self.db.query(Wing).filter(Wing.id == item.wing_id).first()
        dept = self.db.query(Department).filter(Department.id == item.department_id).first()
        doctor = self.db.query(HospitalUser).filter(HospitalUser.id == item.doctor_id).first()
        appt_type = self.db.query(AppointmentType).filter(AppointmentType.id == item.appointment_type_id).first()
        return self._pricing_to_response(
            item,
            wing.name if wing else None,
            dept.name if dept else None,
            doctor.name if doctor else None,
            appt_type.name if appt_type else None,
        )

    @master_mutation("consultation_pricing", "update")
    def update_consultation_pricing(self, item_id: UUID, payload: ConsultationPricingUpdate) -> ConsultationPricingResponse:
        item = self._get_or_404(ConsultationPricing, item_id, "Consultation pricing")
        data = payload.model_dump(exclude_unset=True)
        wing_id = data.get("wing_id", item.wing_id)
        department_id = data.get("department_id", item.department_id)
        doctor_id = data.get("doctor_id", item.doctor_id)
        appointment_type_id = data.get("appointment_type_id", item.appointment_type_id)
        target_is_active = data.get("is_active", item.is_active)
        self._validate_pricing_refs(wing_id, department_id, doctor_id, appointment_type_id, is_active=target_is_active)
        conflict = (
            self.db.query(ConsultationPricing)
            .filter(
                ConsultationPricing.hospital_id == self.hospital_id,
                ConsultationPricing.wing_id == wing_id,
                ConsultationPricing.department_id == department_id,
                ConsultationPricing.doctor_id == doctor_id,
                ConsultationPricing.appointment_type_id == appointment_type_id,
                ConsultationPricing.id != item_id,
            )
            .first()
        )
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Pricing already exists for this wing, department, doctor, and appointment type",
            )
        self._apply_updates(item, data)
        self.db.flush()
        self._audit("update", "consultation_pricing", item.id, f"Updated consultation pricing fee={item.consultation_fee}")
        self.db.commit()
        self.db.refresh(item)
        wing = self.db.query(Wing).filter(Wing.id == item.wing_id).first()
        dept = self.db.query(Department).filter(Department.id == item.department_id).first()
        doctor = self.db.query(HospitalUser).filter(HospitalUser.id == item.doctor_id).first()
        appt_type = self.db.query(AppointmentType).filter(AppointmentType.id == item.appointment_type_id).first()
        return self._pricing_to_response(
            item,
            wing.name if wing else None,
            dept.name if dept else None,
            doctor.name if doctor else None,
            appt_type.name if appt_type else None,
        )

    @master_mutation("consultation_pricing", "delete")
    def delete_consultation_pricing(self, item_id: UUID) -> None:
        item = self._get_or_404(ConsultationPricing, item_id, "Consultation pricing")
        self._audit("delete", "consultation_pricing", item.id, "Deleted consultation pricing")
        assert_unreferenced(self.db, item)
        self.db.delete(item)
        self.db.commit()

    # ── Insurance & TPA Master ────────────────────────────────────────────────
    def list_insurance_providers(
        self,
        search: str | None = None,
        category: str | None = None,
        status_filter: str | None = None,
        active: bool | None = None,
    ) -> list[InsuranceProviderResponse]:
        query = self.db.query(InsuranceProvider).filter(
            InsuranceProvider.hospital_id == self.hospital_id,
        )
        if active is not None:
            query = query.filter(InsuranceProvider.is_active.is_(active))
        if category:
            query = query.filter(InsuranceProvider.category == category)
        if status_filter:
            query = query.filter(InsuranceProvider.status == status_filter)
        if search:
            s = f"%{search.strip()}%"
            query = query.filter(
                (InsuranceProvider.name.ilike(s))
                | (InsuranceProvider.code.ilike(s))
                | (InsuranceProvider.irdai_reg_no.ilike(s))
                | (InsuranceProvider.contact_person.ilike(s))
            )
        rows = query.order_by(InsuranceProvider.name.asc()).all()
        return [InsuranceProviderResponse.model_validate(r) for r in rows]

    @master_mutation("insurance_provider", "create")
    def create_insurance_provider(self, payload: InsuranceProviderCreate) -> InsuranceProviderResponse:
        data = payload.model_dump()
        data["code"] = payload.code.strip().upper()
        if not data["code"]:
            raise HTTPException(422, "Insurance code cannot be blank")
        existing = (
            self.db.query(InsuranceProvider)
            .filter(
                InsuranceProvider.hospital_id == self.hospital_id,
                func.upper(func.trim(InsuranceProvider.code)) == data["code"],
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Insurance provider with code '{payload.code}' already exists",
            )
        item = InsuranceProvider(
            hospital_id=self.hospital_id,
            **data,
        )
        self.db.add(item)
        self.db.flush()
        self._audit("create", "insurance_provider", item.id, f"Created insurance provider {item.name} ({item.code})")
        self.db.commit()
        self.db.refresh(item)
        return InsuranceProviderResponse.model_validate(item)

    @master_mutation("insurance_provider", "update")
    def update_insurance_provider(self, item_id: UUID, payload: InsuranceProviderUpdate) -> InsuranceProviderResponse:
        item = self._get_or_404(InsuranceProvider, item_id, "Insurance provider")
        data = payload.model_dump(exclude_unset=True)
        if "code" in data:
            if data["code"] is None or not data["code"].strip():
                raise HTTPException(422, "Insurance code cannot be blank")
            data["code"] = data["code"].strip().upper()
        if "code" in data and data["code"] != item.code:
            existing = (
                self.db.query(InsuranceProvider)
                .filter(
                    InsuranceProvider.hospital_id == self.hospital_id,
                    func.upper(func.trim(InsuranceProvider.code)) == data["code"],
                    InsuranceProvider.id != item_id,
                )
                .first()
            )
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Insurance provider with code '{data['code']}' already exists",
                )
        self._apply_updates(item, data)
        self.db.flush()
        self._audit("update", "insurance_provider", item.id, f"Updated insurance provider {item.name}")
        self.db.commit()
        self.db.refresh(item)
        return InsuranceProviderResponse.model_validate(item)

    @master_mutation("insurance_provider", "delete")
    def delete_insurance_provider(self, item_id: UUID) -> None:
        item = self._get_or_404(InsuranceProvider, item_id, "Insurance provider")
        self._audit("delete", "insurance_provider", item.id, f"Deleted insurance provider {item.name}")
        assert_unreferenced(self.db, item)
        self.db.delete(item)
        self.db.commit()

