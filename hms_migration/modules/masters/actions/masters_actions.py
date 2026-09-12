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
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
from hms_migration.modules.beds.entities.bed import Room, Ward
from hms_migration.modules.billing.entities.billing_entities import ConsultationPricing
from hms_migration.modules.doctors.entities.doctor import Holiday, HospitalUser, ShiftType
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
from hms_migration.modules.masters.entities.organization_entities import Department, Supplier, Wing
from hms_migration.modules.masters.entities.insurance_entities import InsuranceProvider
from hms_migration.modules.masters.contracts.insurance_contracts import (
    InsuranceProviderCreate,
    InsuranceProviderResponse,
    InsuranceProviderUpdate,
)
from hms_migration.modules.ot.entities.ot_entities import OtRoom
from hms_migration.shared.audit import write_audit_log


def parse_time(value: time | str) -> time:
    if isinstance(value, time):
        return value
    parts = value.split(":")
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


class MastersActions:
    def __init__(self, db: Session, hospital_id: UUID, actor: dict[str, Any] | None = None) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.actor = actor or {}

    def _audit(self, action: str, entity_type: str, entity_id: Any, summary: str, details: dict | None = None) -> None:
        try:
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
        except Exception:
            pass

    def _get_or_404(self, model: type, item_id: UUID, label: str):
        item = self.db.query(model).filter(model.id == item_id, model.hospital_id == self.hospital_id).first()
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")
        return item

    # ── Wings ─────────────────────────────────────────────────────────────────
    def list_wings(self) -> list[WingResponse]:
        return self.db.query(Wing).filter(Wing.hospital_id == self.hospital_id).order_by(Wing.name).all()

    def create_wing(self, payload: WingCreate) -> WingResponse:
        wing = Wing(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(wing)
        self.db.flush()
        self._audit("create", "wing", wing.id, f"Created wing '{wing.name}'")
        self.db.commit()
        self.db.refresh(wing)
        return wing

    def update_wing(self, wing_id: UUID, payload: WingUpdate) -> WingResponse:
        wing = self._get_or_404(Wing, wing_id, "Wing")
        for k, v in payload.model_dump(exclude_unset=True).items():
            setattr(wing, k, v)
        self._audit("update", "wing", wing.id, f"Updated wing '{wing.name}'")
        self.db.commit()
        self.db.refresh(wing)
        return wing

    def delete_wing(self, wing_id: UUID) -> None:
        wing = self._get_or_404(Wing, wing_id, "Wing")
        name = wing.name
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

    def create_department(self, payload: DepartmentCreate) -> DepartmentResponse:
        self._get_or_404(Wing, payload.wing_id, "Wing")
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

    def update_department(self, department_id: UUID, payload: DepartmentUpdate) -> DepartmentResponse:
        dept = self._get_or_404(Department, department_id, "Department")
        data = payload.model_dump(exclude_unset=True)
        if "wing_id" in data and data["wing_id"] is not None:
            self._get_or_404(Wing, data["wing_id"], "Wing")
        for k, v in data.items():
            setattr(dept, k, v)
        self._audit("update", "department", dept.id, f"Updated department '{dept.name}'")
        self.db.commit()
        self.db.refresh(dept)
        wing = self.db.query(Wing).filter(Wing.id == dept.wing_id).first() if dept.wing_id else None
        res = DepartmentResponse.model_validate(dept)
        res.wing_name = wing.name if wing else None
        return res

    def delete_department(self, department_id: UUID) -> None:
        dept = self._get_or_404(Department, department_id, "Department")
        name = dept.name
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

    def create_shift_type(self, payload: ShiftTypeCreate) -> ShiftTypeResponse:
        self._get_or_404(Department, payload.department_id, "Department")
        data = payload.model_dump()
        data["start_time"] = parse_time(data["start_time"])
        data["end_time"] = parse_time(data["end_time"])
        shift = ShiftType(hospital_id=self.hospital_id, **data)
        self.db.add(shift)
        self.db.commit()
        self.db.refresh(shift)
        dept = self.db.query(Department).filter(Department.id == shift.department_id).first()
        res = ShiftTypeResponse.model_validate(shift)
        res.department_name = dept.name if dept else None
        return res

    def update_shift_type(self, shift_id: UUID, payload: ShiftTypeUpdate) -> ShiftTypeResponse:
        shift = self._get_or_404(ShiftType, shift_id, "Shift type")
        if payload.department_id is not None:
            self._get_or_404(Department, payload.department_id, "Department")
        data = payload.model_dump(exclude_unset=True)
        if "start_time" in data and data["start_time"] is not None:
            data["start_time"] = parse_time(data["start_time"])
        if "end_time" in data and data["end_time"] is not None:
            data["end_time"] = parse_time(data["end_time"])
        for k, v in data.items():
            setattr(shift, k, v)
        self.db.commit()
        self.db.refresh(shift)
        dept = self.db.query(Department).filter(Department.id == shift.department_id).first()
        res = ShiftTypeResponse.model_validate(shift)
        res.department_name = dept.name if dept else None
        return res

    def delete_shift_type(self, shift_id: UUID) -> None:
        shift = self._get_or_404(ShiftType, shift_id, "Shift type")
        self.db.delete(shift)
        self.db.commit()

    # ── Holidays ──────────────────────────────────────────────────────────────
    def list_holidays(self) -> list[HolidayResponse]:
        return self.db.query(Holiday).filter(Holiday.hospital_id == self.hospital_id).order_by(Holiday.holiday_date).all()

    def create_holiday(self, payload: HolidayCreate) -> HolidayResponse:
        holiday = Holiday(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(holiday)
        self.db.commit()
        self.db.refresh(holiday)
        return holiday

    def update_holiday(self, holiday_id: UUID, payload: HolidayUpdate) -> HolidayResponse:
        holiday = self._get_or_404(Holiday, holiday_id, "Holiday")
        for k, v in payload.model_dump(exclude_unset=True).items():
            setattr(holiday, k, v)
        self.db.commit()
        self.db.refresh(holiday)
        return holiday

    def delete_holiday(self, holiday_id: UUID) -> None:
        holiday = self._get_or_404(Holiday, holiday_id, "Holiday")
        self.db.delete(holiday)
        self.db.commit()

    # ── Appointment Types ─────────────────────────────────────────────────────
    def list_appointment_types(self) -> list[AppointmentTypeResponse]:
        return (
            self.db.query(AppointmentType)
            .filter(AppointmentType.hospital_id == self.hospital_id)
            .order_by(AppointmentType.name)
            .all()
        )

    def create_appointment_type(self, payload: AppointmentTypeCreate) -> AppointmentTypeResponse:
        item = AppointmentType(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def update_appointment_type(self, item_id: UUID, payload: AppointmentTypeUpdate) -> AppointmentTypeResponse:
        item = self._get_or_404(AppointmentType, item_id, "Appointment type")
        for k, v in payload.model_dump(exclude_unset=True).items():
            setattr(item, k, v)
        self.db.commit()
        self.db.refresh(item)
        return item

    def delete_appointment_type(self, item_id: UUID) -> None:
        item = self._get_or_404(AppointmentType, item_id, "Appointment type")
        self.db.delete(item)
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

    def create_ward(self, payload: WardCreate) -> WardResponse:
        if payload.wing_id:
            self._get_or_404(Wing, payload.wing_id, "Wing")
        if payload.department_id:
            self._get_or_404(Department, payload.department_id, "Department")
        ward = Ward(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(ward)
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

    def update_ward(self, ward_id: UUID, payload: WardUpdate) -> WardResponse:
        ward = self._get_or_404(Ward, ward_id, "Ward")
        updates = payload.model_dump(exclude_unset=True)
        if updates.get("wing_id"):
            self._get_or_404(Wing, updates["wing_id"], "Wing")
        if updates.get("department_id"):
            self._get_or_404(Department, updates["department_id"], "Department")
        for k, v in updates.items():
            setattr(ward, k, v)
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

    def delete_ward(self, ward_id: UUID) -> None:
        ward = self._get_or_404(Ward, ward_id, "Ward")
        self.db.delete(ward)
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

    def create_room(self, payload: RoomCreate) -> RoomResponse:
        self._get_or_404(Ward, payload.ward_id, "Ward")
        room = Room(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(room)
        self.db.commit()
        self.db.refresh(room)
        ward = self.db.query(Ward).filter(Ward.id == room.ward_id).first()
        data = RoomResponse.model_validate(room)
        data.ward_name = ward.name if ward else None
        return data

    def update_room(self, room_id: UUID, payload: RoomUpdate) -> RoomResponse:
        room = self._get_or_404(Room, room_id, "Room")
        if payload.ward_id is not None:
            self._get_or_404(Ward, payload.ward_id, "Ward")
        for k, v in payload.model_dump(exclude_unset=True).items():
            setattr(room, k, v)
        self.db.commit()
        self.db.refresh(room)
        ward = self.db.query(Ward).filter(Ward.id == room.ward_id).first()
        data = RoomResponse.model_validate(room)
        data.ward_name = ward.name if ward else None
        return data

    def delete_room(self, room_id: UUID) -> None:
        room = self._get_or_404(Room, room_id, "Room")
        self.db.delete(room)
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
        return [
            self._ot_room_response(
                r,
                wing_name=r.wing.name if r.wing else None,
                dept_name=r.department.name if r.department else None,
            )
            for r in rooms
        ]

    def create_ot_room(self, payload: OtRoomCreate) -> OtRoomResponse:
        if payload.wing_id:
            self._get_or_404(Wing, payload.wing_id, "Wing")
        self._get_or_404(Department, payload.department_id, "Department")
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
            is_active=payload.is_active,
        )
        self.db.add(room)
        self.db.flush()
        self._audit("create", "ot_room", room.id, f"Created OT room '{room.code}'")
        self.db.commit()
        self.db.refresh(room)
        return self._ot_room_response(room)

    def update_ot_room(self, room_id: UUID, payload: OtRoomUpdate) -> OtRoomResponse:
        room = self._get_or_404(OtRoom, room_id, "OT room")
        updates = payload.model_dump(exclude_unset=True)
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
        for k, v in updates.items():
            setattr(room, k, v)
        self._audit("update", "ot_room", room.id, f"Updated OT room '{room.code}'")
        self.db.commit()
        self.db.refresh(room)
        return self._ot_room_response(room)

    def delete_ot_room(self, room_id: UUID) -> None:
        room = self._get_or_404(OtRoom, room_id, "OT room")
        code = room.code
        self.db.delete(room)
        self._audit("delete", "ot_room", room_id, f"Deleted OT room '{code}'")
        self.db.commit()

    # ── Suppliers ─────────────────────────────────────────────────────────────
    def list_suppliers(self) -> list[SupplierResponse]:
        return self.db.query(Supplier).filter(Supplier.hospital_id == self.hospital_id).order_by(Supplier.name).all()

    def create_supplier(self, payload: SupplierCreate) -> SupplierResponse:
        supplier = Supplier(hospital_id=self.hospital_id, **payload.model_dump())
        self.db.add(supplier)
        self.db.commit()
        self.db.refresh(supplier)
        return supplier

    def update_supplier(self, supplier_id: UUID, payload: SupplierUpdate) -> SupplierResponse:
        supplier = self._get_or_404(Supplier, supplier_id, "Supplier")
        for k, v in payload.model_dump(exclude_unset=True).items():
            setattr(supplier, k, v)
        self.db.commit()
        self.db.refresh(supplier)
        return supplier

    def delete_supplier(self, supplier_id: UUID) -> None:
        supplier = self._get_or_404(Supplier, supplier_id, "Supplier")
        self.db.delete(supplier)
        self.db.commit()

    # ── Consultation Pricing ──────────────────────────────────────────────────
    def _validate_pricing_refs(
        self,
        wing_id: UUID,
        department_id: UUID,
        doctor_id: UUID,
        appointment_type_id: UUID,
    ) -> None:
        self._get_or_404(Wing, wing_id, "Wing")
        self._get_or_404(Department, department_id, "Department")
        self._get_or_404(HospitalUser, doctor_id, "Doctor")
        self._get_or_404(AppointmentType, appointment_type_id, "Appointment type")

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

    def create_consultation_pricing(self, payload: ConsultationPricingCreate) -> ConsultationPricingResponse:
        self._validate_pricing_refs(
            payload.wing_id,
            payload.department_id,
            payload.doctor_id,
            payload.appointment_type_id,
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

    def update_consultation_pricing(self, item_id: UUID, payload: ConsultationPricingUpdate) -> ConsultationPricingResponse:
        item = self._get_or_404(ConsultationPricing, item_id, "Consultation pricing")
        data = payload.model_dump(exclude_unset=True)
        wing_id = data.get("wing_id", item.wing_id)
        department_id = data.get("department_id", item.department_id)
        doctor_id = data.get("doctor_id", item.doctor_id)
        appointment_type_id = data.get("appointment_type_id", item.appointment_type_id)
        self._validate_pricing_refs(wing_id, department_id, doctor_id, appointment_type_id)
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
        for k, v in data.items():
            setattr(item, k, v)
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

    def delete_consultation_pricing(self, item_id: UUID) -> None:
        item = self._get_or_404(ConsultationPricing, item_id, "Consultation pricing")
        self._audit("delete", "consultation_pricing", item.id, "Deleted consultation pricing")
        self.db.delete(item)
        self.db.commit()

    # ── Insurance & TPA Master ────────────────────────────────────────────────
    def _seed_insurance_if_empty(self) -> None:
        count = self.db.query(InsuranceProvider).filter(InsuranceProvider.hospital_id == self.hospital_id).count()
        if count > 0:
            return
        from datetime import date
        seed_data = [
            {
                "code": "TPA-MA-001",
                "name": "Medi Assist Insurance TPA Pvt. Ltd.",
                "category": "Private TPA",
                "irdai_reg_no": "IRDAI: #003",
                "pre_auth_sla_hours": 2,
                "tariff_discount_percent": 15.0,
                "tariff_notes": "15% on IPD • 10% Labs",
                "contact_person": "Alok Tandon",
                "phone": "+91 98111 22334",
                "email": "claims@mediassist.in",
                "status": "active",
                "mou_valid_till": date(2027, 11, 30),
                "active_claims_count": 24,
                "active_receivables_amount": 3420000.0,
            },
            {
                "code": "INS-SH-002",
                "name": "Star Health & Allied Insurance Co. Ltd.",
                "category": "Direct Insurer",
                "irdai_reg_no": "IRDAI: #129",
                "pre_auth_sla_hours": 2,
                "tariff_discount_percent": 12.0,
                "tariff_notes": "12% on All Packages",
                "contact_person": "Priya Sharma",
                "phone": "+91 98222 33445",
                "email": "hospitaldesk@starhealth.in",
                "status": "active",
                "mou_valid_till": date(2027, 6, 30),
                "active_claims_count": 18,
                "active_receivables_amount": 2650000.0,
            },
            {
                "code": "INS-IL-003",
                "name": "ICICI Lombard General Insurance",
                "category": "Direct Insurer",
                "irdai_reg_no": "IRDAI: #115",
                "pre_auth_sla_hours": 2,
                "tariff_discount_percent": 10.0,
                "tariff_notes": "10% Standard Rate",
                "contact_person": "Vikram Seth",
                "phone": "+91 98333 44556",
                "email": "cashless@icicilombard.com",
                "status": "active",
                "mou_valid_till": date(2028, 3, 31),
                "active_claims_count": 15,
                "active_receivables_amount": 2180000.0,
            },
            {
                "code": "INS-HE-004",
                "name": "HDFC ERGO General Insurance Co.",
                "category": "Direct Insurer",
                "irdai_reg_no": "IRDAI: #146",
                "pre_auth_sla_hours": 2,
                "tariff_discount_percent": 10.0,
                "tariff_notes": "10% on IPD Packages",
                "contact_person": "Rajesh Nair",
                "phone": "+91 98444 55667",
                "email": "claims@hdfcergo.com",
                "status": "active",
                "mou_valid_till": date(2027, 9, 30),
                "active_claims_count": 12,
                "active_receivables_amount": 1740000.0,
            },
            {
                "code": "TPA-VH-005",
                "name": "Vidal Health Insurance TPA",
                "category": "Private TPA",
                "irdai_reg_no": "IRDAI: #016",
                "pre_auth_sla_hours": 2,
                "tariff_discount_percent": 15.0,
                "tariff_notes": "15% on IPD • 5% Pharmacy",
                "contact_person": "Sunita Patel",
                "phone": "+91 98555 66778",
                "email": "preauth@vidalhealthtpa.com",
                "status": "active",
                "mou_valid_till": date(2026, 12, 31),
                "active_claims_count": 9,
                "active_receivables_amount": 1120000.0,
            },
            {
                "code": "GOV-PM-006",
                "name": "PM-JAY Ayushman Bharat Scheme",
                "category": "Government Scheme",
                "irdai_reg_no": "NHA-PMJAY",
                "pre_auth_sla_hours": 4,
                "tariff_discount_percent": 20.0,
                "tariff_notes": "Pre-fixed Package Rates (TMS 2.0)",
                "contact_person": "Nodal Officer Health",
                "phone": "+91 98666 77889",
                "email": "pmjay@hospital.gov.in",
                "status": "active",
                "mou_valid_till": date(2029, 12, 31),
                "active_claims_count": 32,
                "active_receivables_amount": 4250000.0,
            },
            {
                "code": "TPA-MD-007",
                "name": "MDIndia Health Insurance TPA",
                "category": "Private TPA",
                "irdai_reg_no": "IRDAI: #005",
                "pre_auth_sla_hours": 2,
                "tariff_discount_percent": 14.0,
                "tariff_notes": "14% on Ward Tariffs",
                "contact_person": "Amit Joshi",
                "phone": "+91 98777 88990",
                "email": "customercare@mdindia.com",
                "status": "under_renewal",
                "mou_valid_till": date(2026, 10, 15),
                "active_claims_count": 5,
                "active_receivables_amount": 680000.0,
            },
        ]
        for item in seed_data:
            prov = InsuranceProvider(hospital_id=self.hospital_id, **item)
            self.db.add(prov)
        self.db.commit()

    def list_insurance_providers(
        self,
        search: str | None = None,
        category: str | None = None,
        status_filter: str | None = None,
    ) -> list[InsuranceProviderResponse]:
        self._seed_insurance_if_empty()
        query = self.db.query(InsuranceProvider).filter(
            InsuranceProvider.hospital_id == self.hospital_id,
            InsuranceProvider.is_active.is_(True),
        )
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

    def create_insurance_provider(self, payload: InsuranceProviderCreate) -> InsuranceProviderResponse:
        existing = (
            self.db.query(InsuranceProvider)
            .filter(
                InsuranceProvider.hospital_id == self.hospital_id,
                InsuranceProvider.code == payload.code.strip(),
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
            **payload.model_dump(),
        )
        self.db.add(item)
        self.db.flush()
        self._audit("create", "insurance_provider", item.id, f"Created insurance provider {item.name} ({item.code})")
        self.db.commit()
        self.db.refresh(item)
        return InsuranceProviderResponse.model_validate(item)

    def update_insurance_provider(self, item_id: UUID, payload: InsuranceProviderUpdate) -> InsuranceProviderResponse:
        item = self._get_or_404(InsuranceProvider, item_id, "Insurance provider")
        data = payload.model_dump(exclude_unset=True)
        if "code" in data and data["code"] != item.code:
            existing = (
                self.db.query(InsuranceProvider)
                .filter(
                    InsuranceProvider.hospital_id == self.hospital_id,
                    InsuranceProvider.code == data["code"],
                    InsuranceProvider.id != item_id,
                )
                .first()
            )
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Insurance provider with code '{data['code']}' already exists",
                )
        for k, v in data.items():
            setattr(item, k, v)
        self.db.flush()
        self._audit("update", "insurance_provider", item.id, f"Updated insurance provider {item.name}")
        self.db.commit()
        self.db.refresh(item)
        return InsuranceProviderResponse.model_validate(item)

    def delete_insurance_provider(self, item_id: UUID) -> None:
        item = self._get_or_404(InsuranceProvider, item_id, "Insurance provider")
        self._audit("delete", "insurance_provider", item.id, f"Deleted insurance provider {item.name}")
        self.db.delete(item)
        self.db.commit()

