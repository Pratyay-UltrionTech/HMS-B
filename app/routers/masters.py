from datetime import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    AppointmentType,
    Department,
    Holiday,
    OtRoom,
    Room,
    ShiftType,
    Supplier,
    Ward,
    Wing,
)
from app.schemas_masters import (
    AppointmentTypeCreate,
    AppointmentTypeResponse,
    AppointmentTypeUpdate,
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
from app.utils.auth import get_hospital_context, get_hospital_uuid, require_hospital_admin
from app.utils.audit import write_audit

router = APIRouter(prefix="/masters", tags=["masters"])


def _parse_time(value: time | str) -> time:
    if isinstance(value, time):
        return value
    parts = value.split(":")
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


def _get_or_404(db: Session, model, item_id: UUID, hospital_id: UUID, label: str):
    item = db.query(model).filter(model.id == item_id, model.hospital_id == hospital_id).first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")
    return item


def _apply_updates(item, payload) -> None:
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(item, key, value)


def _audit(db: Session, hospital_id: UUID, actor: dict, action: str, entity_type: str, entity_id, summary: str) -> None:
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
    )


# ── Wings ──────────────────────────────────────────────────────────────────────
@router.get("/wings", response_model=list[WingResponse])
def list_wings(db: Session = Depends(get_db), hospital_id: UUID = Depends(get_hospital_uuid)):
    return db.query(Wing).filter(Wing.hospital_id == hospital_id).order_by(Wing.name).all()


@router.post("/wings", response_model=WingResponse, status_code=status.HTTP_201_CREATED)
def create_wing(
    payload: WingCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    wing = Wing(hospital_id=hospital_id, **payload.model_dump())
    db.add(wing)
    db.flush()
    _audit(db, hospital_id, actor, "create", "wing", wing.id, f"Created wing '{wing.name}'")
    db.commit()
    db.refresh(wing)
    return wing


@router.put("/wings/{wing_id}", response_model=WingResponse)
def update_wing(
    wing_id: UUID,
    payload: WingUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    wing = _get_or_404(db, Wing, wing_id, hospital_id, "Wing")
    _apply_updates(wing, payload)
    _audit(db, hospital_id, actor, "update", "wing", wing.id, f"Updated wing '{wing.name}'")
    db.commit()
    db.refresh(wing)
    return wing


@router.delete("/wings/{wing_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_wing(
    wing_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    wing = _get_or_404(db, Wing, wing_id, hospital_id, "Wing")
    name = wing.name
    db.delete(wing)
    _audit(db, hospital_id, actor, "delete", "wing", wing_id, f"Deleted wing '{name}'")
    db.commit()


# ── Departments ────────────────────────────────────────────────────────────────
@router.get("/departments", response_model=list[DepartmentResponse])
def list_departments(db: Session = Depends(get_db), hospital_id: UUID = Depends(get_hospital_uuid)):
    rows = (
        db.query(Department, Wing.name)
        .outerjoin(Wing, Department.wing_id == Wing.id)
        .filter(Department.hospital_id == hospital_id)
        .order_by(Wing.name, Department.name)
        .all()
    )
    result = []
    for dept, wing_name in rows:
        data = DepartmentResponse.model_validate(dept)
        data.wing_name = wing_name
        result.append(data)
    return result


@router.post("/departments", response_model=DepartmentResponse, status_code=status.HTTP_201_CREATED)
def create_department(
    payload: DepartmentCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    _get_or_404(db, Wing, payload.wing_id, hospital_id, "Wing")
    dept = Department(hospital_id=hospital_id, **payload.model_dump())
    db.add(dept)
    db.flush()
    _audit(db, hospital_id, actor, "create", "department", dept.id, f"Created department '{dept.name}'")
    db.commit()
    db.refresh(dept)
    wing = db.query(Wing).filter(Wing.id == dept.wing_id).first() if dept.wing_id else None
    data = DepartmentResponse.model_validate(dept)
    data.wing_name = wing.name if wing else None
    return data


@router.put("/departments/{department_id}", response_model=DepartmentResponse)
def update_department(
    department_id: UUID,
    payload: DepartmentUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    dept = _get_or_404(db, Department, department_id, hospital_id, "Department")
    updates = payload.model_dump(exclude_unset=True)
    if "wing_id" in updates and updates["wing_id"] is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wing is required for department")
    if "wing_id" in updates and updates["wing_id"] is not None:
        _get_or_404(db, Wing, updates["wing_id"], hospital_id, "Wing")
    for key, value in updates.items():
        setattr(dept, key, value)
    if dept.wing_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wing is required for department")
    _audit(db, hospital_id, actor, "update", "department", dept.id, f"Updated department '{dept.name}'")
    db.commit()
    db.refresh(dept)
    wing = db.query(Wing).filter(Wing.id == dept.wing_id).first() if dept.wing_id else None
    data = DepartmentResponse.model_validate(dept)
    data.wing_name = wing.name if wing else None
    return data


@router.delete("/departments/{department_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_department(
    department_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    dept = _get_or_404(db, Department, department_id, hospital_id, "Department")
    name = dept.name
    db.delete(dept)
    _audit(db, hospital_id, actor, "delete", "department", department_id, f"Deleted department '{name}'")
    db.commit()


# ── Shift types ────────────────────────────────────────────────────────────────
@router.get("/shift-types", response_model=list[ShiftTypeResponse])
def list_shift_types(
    department_id: UUID | None = None,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    query = (
        db.query(ShiftType, Department.name)
        .outerjoin(Department, ShiftType.department_id == Department.id)
        .filter(ShiftType.hospital_id == hospital_id)
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


@router.post("/shift-types", response_model=ShiftTypeResponse, status_code=status.HTTP_201_CREATED)
def create_shift_type(
    payload: ShiftTypeCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    _get_or_404(db, Department, payload.department_id, hospital_id, "Department")
    data = payload.model_dump()
    data["start_time"] = _parse_time(data["start_time"])
    data["end_time"] = _parse_time(data["end_time"])
    shift = ShiftType(hospital_id=hospital_id, **data)
    db.add(shift)
    db.commit()
    db.refresh(shift)
    dept = db.query(Department).filter(Department.id == shift.department_id).first()
    resp = ShiftTypeResponse.model_validate(shift)
    resp.department_name = dept.name if dept else None
    return resp


@router.put("/shift-types/{shift_id}", response_model=ShiftTypeResponse)
def update_shift_type(
    shift_id: UUID,
    payload: ShiftTypeUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    shift = _get_or_404(db, ShiftType, shift_id, hospital_id, "Shift type")
    if payload.department_id is not None:
        _get_or_404(db, Department, payload.department_id, hospital_id, "Department")
    data = payload.model_dump(exclude_unset=True)
    if "start_time" in data and data["start_time"] is not None:
        data["start_time"] = _parse_time(data["start_time"])
    if "end_time" in data and data["end_time"] is not None:
        data["end_time"] = _parse_time(data["end_time"])
    for key, value in data.items():
        setattr(shift, key, value)
    db.commit()
    db.refresh(shift)
    dept = db.query(Department).filter(Department.id == shift.department_id).first()
    resp = ShiftTypeResponse.model_validate(shift)
    resp.department_name = dept.name if dept else None
    return resp


@router.delete("/shift-types/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shift_type(
    shift_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    shift = _get_or_404(db, ShiftType, shift_id, hospital_id, "Shift type")
    db.delete(shift)
    db.commit()


# ── Holidays ───────────────────────────────────────────────────────────────────
@router.get("/holidays", response_model=list[HolidayResponse])
def list_holidays(db: Session = Depends(get_db), hospital_id: UUID = Depends(get_hospital_uuid)):
    return db.query(Holiday).filter(Holiday.hospital_id == hospital_id).order_by(Holiday.holiday_date).all()


@router.post("/holidays", response_model=HolidayResponse, status_code=status.HTTP_201_CREATED)
def create_holiday(
    payload: HolidayCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    holiday = Holiday(hospital_id=hospital_id, **payload.model_dump())
    db.add(holiday)
    db.commit()
    db.refresh(holiday)
    return holiday


@router.put("/holidays/{holiday_id}", response_model=HolidayResponse)
def update_holiday(
    holiday_id: UUID,
    payload: HolidayUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    holiday = _get_or_404(db, Holiday, holiday_id, hospital_id, "Holiday")
    _apply_updates(holiday, payload)
    db.commit()
    db.refresh(holiday)
    return holiday


@router.delete("/holidays/{holiday_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_holiday(
    holiday_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    holiday = _get_or_404(db, Holiday, holiday_id, hospital_id, "Holiday")
    db.delete(holiday)
    db.commit()


# ── Appointment types ──────────────────────────────────────────────────────────
@router.get("/appointment-types", response_model=list[AppointmentTypeResponse])
def list_appointment_types(db: Session = Depends(get_db), hospital_id: UUID = Depends(get_hospital_uuid)):
    return (
        db.query(AppointmentType)
        .filter(AppointmentType.hospital_id == hospital_id)
        .order_by(AppointmentType.name)
        .all()
    )


@router.post("/appointment-types", response_model=AppointmentTypeResponse, status_code=status.HTTP_201_CREATED)
def create_appointment_type(
    payload: AppointmentTypeCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    item = AppointmentType(hospital_id=hospital_id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/appointment-types/{item_id}", response_model=AppointmentTypeResponse)
def update_appointment_type(
    item_id: UUID,
    payload: AppointmentTypeUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    item = _get_or_404(db, AppointmentType, item_id, hospital_id, "Appointment type")
    _apply_updates(item, payload)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/appointment-types/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_appointment_type(
    item_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    item = _get_or_404(db, AppointmentType, item_id, hospital_id, "Appointment type")
    db.delete(item)
    db.commit()


# ── Wards ──────────────────────────────────────────────────────────────────────
@router.get("/wards", response_model=list[WardResponse])
def list_wards(db: Session = Depends(get_db), hospital_id: UUID = Depends(get_hospital_uuid)):
    rows = (
        db.query(Ward, Wing.name, Department.name)
        .outerjoin(Wing, Ward.wing_id == Wing.id)
        .outerjoin(Department, Ward.department_id == Department.id)
        .filter(Ward.hospital_id == hospital_id)
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


@router.post("/wards", response_model=WardResponse, status_code=status.HTTP_201_CREATED)
def create_ward(
    payload: WardCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    if payload.wing_id:
        _get_or_404(db, Wing, payload.wing_id, hospital_id, "Wing")
    if payload.department_id:
        _get_or_404(db, Department, payload.department_id, hospital_id, "Department")
    ward = Ward(hospital_id=hospital_id, **payload.model_dump())
    db.add(ward)
    db.commit()
    db.refresh(ward)
    data = WardResponse.model_validate(ward)
    if ward.wing_id:
        wing = db.query(Wing).filter(Wing.id == ward.wing_id).first()
        data.wing_name = wing.name if wing else None
    if ward.department_id:
        dept = db.query(Department).filter(Department.id == ward.department_id).first()
        data.department_name = dept.name if dept else None
    return data


@router.put("/wards/{ward_id}", response_model=WardResponse)
def update_ward(
    ward_id: UUID,
    payload: WardUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    ward = _get_or_404(db, Ward, ward_id, hospital_id, "Ward")
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("wing_id"):
        _get_or_404(db, Wing, updates["wing_id"], hospital_id, "Wing")
    if updates.get("department_id"):
        _get_or_404(db, Department, updates["department_id"], hospital_id, "Department")
    for key, value in updates.items():
        setattr(ward, key, value)
    db.commit()
    db.refresh(ward)
    data = WardResponse.model_validate(ward)
    if ward.wing_id:
        wing = db.query(Wing).filter(Wing.id == ward.wing_id).first()
        data.wing_name = wing.name if wing else None
    if ward.department_id:
        dept = db.query(Department).filter(Department.id == ward.department_id).first()
        data.department_name = dept.name if dept else None
    return data


@router.delete("/wards/{ward_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ward(
    ward_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    ward = _get_or_404(db, Ward, ward_id, hospital_id, "Ward")
    db.delete(ward)
    db.commit()


# ── Rooms ──────────────────────────────────────────────────────────────────────
@router.get("/rooms", response_model=list[RoomResponse])
def list_rooms(db: Session = Depends(get_db), hospital_id: UUID = Depends(get_hospital_uuid)):
    rows = (
        db.query(Room, Ward.name)
        .outerjoin(Ward, Room.ward_id == Ward.id)
        .filter(Room.hospital_id == hospital_id)
        .order_by(Room.room_code)
        .all()
    )
    result = []
    for room, ward_name in rows:
        data = RoomResponse.model_validate(room)
        data.ward_name = ward_name
        result.append(data)
    return result


@router.post("/rooms", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
def create_room(
    payload: RoomCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    _get_or_404(db, Ward, payload.ward_id, hospital_id, "Ward")
    room = Room(hospital_id=hospital_id, **payload.model_dump())
    db.add(room)
    db.commit()
    db.refresh(room)
    ward = db.query(Ward).filter(Ward.id == room.ward_id).first()
    data = RoomResponse.model_validate(room)
    data.ward_name = ward.name if ward else None
    return data


@router.put("/rooms/{room_id}", response_model=RoomResponse)
def update_room(
    room_id: UUID,
    payload: RoomUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    room = _get_or_404(db, Room, room_id, hospital_id, "Room")
    if payload.ward_id is not None:
        _get_or_404(db, Ward, payload.ward_id, hospital_id, "Ward")
    _apply_updates(room, payload)
    db.commit()
    db.refresh(room)
    ward = db.query(Ward).filter(Ward.id == room.ward_id).first()
    data = RoomResponse.model_validate(room)
    data.ward_name = ward.name if ward else None
    return data


@router.delete("/rooms/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_room(
    room_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    room = _get_or_404(db, Room, room_id, hospital_id, "Room")
    db.delete(room)
    db.commit()


def _ot_room_response(db: Session, room: OtRoom) -> OtRoomResponse:
    data = OtRoomResponse.model_validate(room)
    if room.wing_id:
        wing = db.query(Wing).filter(Wing.id == room.wing_id).first()
        data.wing_name = wing.name if wing else None
    dept = db.query(Department).filter(Department.id == room.department_id).first()
    data.department_name = dept.name if dept else None
    return data


# ── OT Rooms ───────────────────────────────────────────────────────────────────
@router.get("/ot-rooms", response_model=list[OtRoomResponse])
def list_ot_rooms(
    department_id: UUID | None = None,
    active_only: bool = False,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Readable by hospital admin and staff (used by OT booking)."""
    q = db.query(OtRoom).filter(OtRoom.hospital_id == hospital_id)
    if department_id is not None:
        q = q.filter(OtRoom.department_id == department_id)
    if active_only:
        q = q.filter(OtRoom.is_active.is_(True))
    rooms = q.order_by(OtRoom.code).all()
    return [_ot_room_response(db, r) for r in rooms]


@router.post("/ot-rooms", response_model=OtRoomResponse, status_code=status.HTTP_201_CREATED)
def create_ot_room(
    payload: OtRoomCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    if payload.wing_id:
        _get_or_404(db, Wing, payload.wing_id, hospital_id, "Wing")
    _get_or_404(db, Department, payload.department_id, hospital_id, "Department")
    code = payload.code.strip().upper()
    exists = (
        db.query(OtRoom.id)
        .filter(OtRoom.hospital_id == hospital_id, OtRoom.code == code)
        .first()
    )
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="OT room code already exists")
    room = OtRoom(
        hospital_id=hospital_id,
        wing_id=payload.wing_id,
        department_id=payload.department_id,
        code=code,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        is_active=payload.is_active,
    )
    db.add(room)
    db.flush()
    _audit(db, hospital_id, actor, "create", "ot_room", room.id, f"Created OT room '{room.code}'")
    db.commit()
    db.refresh(room)
    return _ot_room_response(db, room)


@router.put("/ot-rooms/{room_id}", response_model=OtRoomResponse)
def update_ot_room(
    room_id: UUID,
    payload: OtRoomUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    room = _get_or_404(db, OtRoom, room_id, hospital_id, "OT room")
    updates = payload.model_dump(exclude_unset=True)
    if "wing_id" in updates and updates["wing_id"] is not None:
        _get_or_404(db, Wing, updates["wing_id"], hospital_id, "Wing")
    if updates.get("department_id"):
        _get_or_404(db, Department, updates["department_id"], hospital_id, "Department")
    if "code" in updates and updates["code"] is not None:
        updates["code"] = str(updates["code"]).strip().upper()
        clash = (
            db.query(OtRoom.id)
            .filter(
                OtRoom.hospital_id == hospital_id,
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
    for key, value in updates.items():
        setattr(room, key, value)
    _audit(db, hospital_id, actor, "update", "ot_room", room.id, f"Updated OT room '{room.code}'")
    db.commit()
    db.refresh(room)
    return _ot_room_response(db, room)


@router.delete("/ot-rooms/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ot_room(
    room_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    room = _get_or_404(db, OtRoom, room_id, hospital_id, "OT room")
    code = room.code
    db.delete(room)
    _audit(db, hospital_id, actor, "delete", "ot_room", room_id, f"Deleted OT room '{code}'")
    db.commit()


# ── Suppliers ──────────────────────────────────────────────────────────────────
@router.get("/suppliers", response_model=list[SupplierResponse])
def list_suppliers(db: Session = Depends(get_db), hospital_id: UUID = Depends(get_hospital_uuid)):
    return db.query(Supplier).filter(Supplier.hospital_id == hospital_id).order_by(Supplier.name).all()


@router.post("/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    supplier = Supplier(hospital_id=hospital_id, **payload.model_dump())
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.put("/suppliers/{supplier_id}", response_model=SupplierResponse)
def update_supplier(
    supplier_id: UUID,
    payload: SupplierUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    supplier = _get_or_404(db, Supplier, supplier_id, hospital_id, "Supplier")
    _apply_updates(supplier, payload)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.delete("/suppliers/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_supplier(
    supplier_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    supplier = _get_or_404(db, Supplier, supplier_id, hospital_id, "Supplier")
    db.delete(supplier)
    db.commit()
