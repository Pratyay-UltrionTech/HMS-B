from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    EquipmentAssignment,
    EquipmentAssignTarget,
    EquipmentCategory,
    EquipmentItem,
    EquipmentMaintenance,
    EquipmentRequest,
    EquipmentRequestStatus,
    EquipmentServiceLog,
    EquipmentStatus,
    MaintenanceStatus,
)
from app.schemas_equipment import (
    AmcUpdate,
    AssignmentCreate,
    AssignmentResponse,
    EquipCategoryCreate,
    EquipCategoryResponse,
    EquipCategoryUpdate,
    EquipDashboardResponse,
    EquipmentCreate,
    EquipmentResponse,
    EquipmentUpdate,
    MaintenanceComplete,
    MaintenanceCreate,
    MaintenanceResponse,
    RequestAction,
    RequestCreate,
    RequestResponse,
    ServiceLogCreate,
    ServiceLogResponse,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/equipment", tags=["equipment"])

DEFAULT_CATEGORIES = [
    "Diagnostic",
    "Surgical",
    "Monitoring",
    "Laboratory",
    "Radiology",
    "ICU",
    "General",
    "Furniture",
]


def _actor_name(user: dict) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _next_asset_id(db: Session, hospital_id: UUID) -> str:
    count = db.query(func.count(EquipmentItem.id)).filter(EquipmentItem.hospital_id == hospital_id).scalar() or 0
    return f"EQ{int(count) + 1:03d}"


def _next_request_no(db: Session, hospital_id: UUID) -> str:
    count = (
        db.query(func.count(EquipmentRequest.id)).filter(EquipmentRequest.hospital_id == hospital_id).scalar() or 0
    )
    return f"ER{int(count) + 1:04d}"


def _ensure_default_categories(db: Session, hospital_id: UUID) -> None:
    existing = {
        r.name.lower()
        for r in db.query(EquipmentCategory.name).filter(EquipmentCategory.hospital_id == hospital_id).all()
    }
    added = False
    for name in DEFAULT_CATEGORIES:
        if name.lower() not in existing:
            db.add(EquipmentCategory(hospital_id=hospital_id, name=name, is_active=True))
            added = True
    if added:
        db.commit()


def _equip_to_response(item: EquipmentItem) -> EquipmentResponse:
    active = next((a for a in (item.assignments or []) if a.is_active), None)
    return EquipmentResponse(
        id=item.id,
        hospital_id=item.hospital_id,
        asset_id=item.asset_id,
        name=item.name,
        category_id=item.category_id,
        category_name=item.category.name if item.category else None,
        manufacturer=item.manufacturer,
        model=item.model,
        serial_number=item.serial_number,
        purchase_date=item.purchase_date,
        purchase_cost=item.purchase_cost,
        department=item.department,
        current_location=item.current_location,
        status=item.status,
        vendor=item.vendor,
        warranty_start=item.warranty_start,
        warranty_end=item.warranty_end,
        amc_start=item.amc_start,
        amc_end=item.amc_end,
        vendor_contact=item.vendor_contact,
        notes=item.notes,
        created_at=item.created_at,
        active_assignment=f"{active.target_type.value}: {active.target_name}" if active else None,
    )


def _get_equipment(db: Session, item_id: UUID, hospital_id: UUID) -> EquipmentItem:
    item = (
        db.query(EquipmentItem)
        .options(
            joinedload(EquipmentItem.category),
            joinedload(EquipmentItem.assignments),
        )
        .filter(EquipmentItem.id == item_id, EquipmentItem.hospital_id == hospital_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipment not found")
    return item


def _refresh_maintenance_status(row: EquipmentMaintenance) -> None:
    if row.status in (MaintenanceStatus.completed,):
        return
    today = date.today()
    if row.next_service_date < today:
        row.status = MaintenanceStatus.overdue
    elif row.next_service_date <= today:
        row.status = MaintenanceStatus.due
    else:
        row.status = MaintenanceStatus.ok if row.last_service_date else MaintenanceStatus.scheduled


@router.get("/dashboard", response_model=EquipDashboardResponse)
def dashboard(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(EquipmentItem).filter(EquipmentItem.hospital_id == hospital_id)
    return EquipDashboardResponse(
        total=q.count(),
        available=q.filter(EquipmentItem.status == EquipmentStatus.available).count(),
        in_use=q.filter(EquipmentItem.status == EquipmentStatus.in_use).count(),
        under_maintenance=q.filter(EquipmentItem.status == EquipmentStatus.under_maintenance).count(),
        out_of_service=q.filter(EquipmentItem.status == EquipmentStatus.out_of_service).count(),
    )


# ── Categories ────────────────────────────────────────────────────────────────

@router.get("/categories", response_model=list[EquipCategoryResponse])
def list_categories(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    _ensure_default_categories(db, hospital_id)
    rows = (
        db.query(EquipmentCategory)
        .filter(EquipmentCategory.hospital_id == hospital_id)
        .order_by(EquipmentCategory.name.asc())
        .all()
    )
    out: list[EquipCategoryResponse] = []
    for r in rows:
        count = (
            db.query(func.count(EquipmentItem.id))
            .filter(EquipmentItem.hospital_id == hospital_id, EquipmentItem.category_id == r.id)
            .scalar()
            or 0
        )
        out.append(
            EquipCategoryResponse(
                id=r.id,
                hospital_id=r.hospital_id,
                name=r.name,
                description=r.description,
                is_active=r.is_active,
                created_at=r.created_at,
                equipment_count=int(count),
            )
        )
    return out


@router.post("/categories", response_model=EquipCategoryResponse, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: EquipCategoryCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    name = payload.name.strip()
    exists = (
        db.query(EquipmentCategory.id)
        .filter(EquipmentCategory.hospital_id == hospital_id, EquipmentCategory.name.ilike(name))
        .first()
    )
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Category already exists")
    row = EquipmentCategory(
        hospital_id=hospital_id,
        name=name,
        description=payload.description,
        is_active=payload.is_active,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="equipment_category",
        entity_id=row.id,
        summary=f"Added equipment category {name}",
    )
    db.commit()
    db.refresh(row)
    return EquipCategoryResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        name=row.name,
        description=row.description,
        is_active=row.is_active,
        created_at=row.created_at,
        equipment_count=0,
    )


@router.put("/categories/{category_id}", response_model=EquipCategoryResponse)
def update_category(
    category_id: UUID,
    payload: EquipCategoryUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(EquipmentCategory)
        .filter(EquipmentCategory.id == category_id, EquipmentCategory.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if isinstance(v, str):
            v = v.strip()
        setattr(row, k, v)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="equipment_category",
        entity_id=row.id,
        summary=f"Updated equipment category {row.name}",
    )
    db.commit()
    count = (
        db.query(func.count(EquipmentItem.id))
        .filter(EquipmentItem.hospital_id == hospital_id, EquipmentItem.category_id == row.id)
        .scalar()
        or 0
    )
    return EquipCategoryResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        name=row.name,
        description=row.description,
        is_active=row.is_active,
        created_at=row.created_at,
        equipment_count=int(count),
    )


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(EquipmentCategory)
        .filter(EquipmentCategory.id == category_id, EquipmentCategory.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    name = row.name
    db.delete(row)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="delete",
        entity_type="equipment_category",
        entity_id=category_id,
        summary=f"Deleted equipment category {name}",
    )
    db.commit()
    return None


# ── Inventory ─────────────────────────────────────────────────────────────────

@router.get("/items", response_model=list[EquipmentResponse])
def list_items(
    search: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    category_id: UUID | None = None,
    amc_only: bool | None = Query(None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(EquipmentItem)
        .options(joinedload(EquipmentItem.category), joinedload(EquipmentItem.assignments))
        .filter(EquipmentItem.hospital_id == hospital_id)
    )
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(
            or_(
                EquipmentItem.asset_id.ilike(like),
                EquipmentItem.name.ilike(like),
                EquipmentItem.department.ilike(like),
                EquipmentItem.serial_number.ilike(like),
                EquipmentItem.vendor.ilike(like),
            )
        )
    if status_filter:
        try:
            q = q.filter(EquipmentItem.status == EquipmentStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
    if category_id:
        q = q.filter(EquipmentItem.category_id == category_id)
    if amc_only:
        q = q.filter(or_(EquipmentItem.vendor.isnot(None), EquipmentItem.warranty_end.isnot(None), EquipmentItem.amc_end.isnot(None)))
    rows = q.order_by(EquipmentItem.asset_id.asc()).limit(500).all()
    return [_equip_to_response(r) for r in rows]


@router.post("/items", response_model=EquipmentResponse, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: EquipmentCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    asset_id = (payload.asset_id or "").strip().upper() or _next_asset_id(db, hospital_id)
    exists = (
        db.query(EquipmentItem.id)
        .filter(EquipmentItem.hospital_id == hospital_id, EquipmentItem.asset_id == asset_id)
        .first()
    )
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Asset ID already exists")
    if payload.category_id:
        cat = (
            db.query(EquipmentCategory.id)
            .filter(EquipmentCategory.id == payload.category_id, EquipmentCategory.hospital_id == hospital_id)
            .first()
        )
        if not cat:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    item = EquipmentItem(
        hospital_id=hospital_id,
        asset_id=asset_id,
        name=payload.name.strip(),
        category_id=payload.category_id,
        manufacturer=payload.manufacturer.strip() if payload.manufacturer else None,
        model=payload.model.strip() if payload.model else None,
        serial_number=payload.serial_number.strip() if payload.serial_number else None,
        purchase_date=payload.purchase_date,
        purchase_cost=payload.purchase_cost,
        department=payload.department.strip() if payload.department else None,
        current_location=payload.current_location.strip() if payload.current_location else None,
        status=payload.status,
        vendor=payload.vendor.strip() if payload.vendor else None,
        warranty_start=payload.warranty_start,
        warranty_end=payload.warranty_end,
        amc_start=payload.amc_start,
        amc_end=payload.amc_end,
        vendor_contact=payload.vendor_contact.strip() if payload.vendor_contact else None,
        notes=payload.notes.strip() if payload.notes else None,
    )
    db.add(item)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="equipment",
        entity_id=item.id,
        summary=f"Added equipment {item.asset_id} — {item.name}",
    )
    db.commit()
    return _equip_to_response(_get_equipment(db, item.id, hospital_id))


@router.put("/items/{item_id}", response_model=EquipmentResponse)
def update_item(
    item_id: UUID,
    payload: EquipmentUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_equipment(db, item_id, hospital_id)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if isinstance(v, str):
            v = v.strip() or None
        setattr(item, k, v)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="equipment",
        entity_id=item.id,
        summary=f"Updated equipment {item.asset_id}",
    )
    db.commit()
    return _equip_to_response(_get_equipment(db, item_id, hospital_id))


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(
    item_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_equipment(db, item_id, hospital_id)
    code = item.asset_id
    db.delete(item)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="delete",
        entity_type="equipment",
        entity_id=item_id,
        summary=f"Deleted equipment {code}",
    )
    db.commit()
    return None


@router.put("/items/{item_id}/amc", response_model=EquipmentResponse)
def update_amc(
    item_id: UUID,
    payload: AmcUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_equipment(db, item_id, hospital_id)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if isinstance(v, str):
            v = v.strip() or None
        setattr(item, k, v)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="equipment_amc",
        entity_id=item.id,
        summary=f"Updated AMC/warranty for {item.asset_id}",
    )
    db.commit()
    return _equip_to_response(_get_equipment(db, item_id, hospital_id))


# ── Assignments ───────────────────────────────────────────────────────────────

@router.get("/assignments", response_model=list[AssignmentResponse])
def list_assignments(
    active_only: bool | None = Query(True),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(EquipmentAssignment)
        .options(joinedload(EquipmentAssignment.equipment))
        .filter(EquipmentAssignment.hospital_id == hospital_id)
    )
    if active_only:
        q = q.filter(EquipmentAssignment.is_active.is_(True))
    rows = q.order_by(EquipmentAssignment.assigned_at.desc()).limit(300).all()
    return [
        AssignmentResponse(
            id=r.id,
            equipment_id=r.equipment_id,
            equipment_name=r.equipment.name if r.equipment else None,
            asset_id=r.equipment.asset_id if r.equipment else None,
            target_type=r.target_type,
            target_name=r.target_name,
            assigned_by_name=r.assigned_by_name,
            assigned_at=r.assigned_at,
            returned_at=r.returned_at,
            is_active=r.is_active,
            remarks=r.remarks,
        )
        for r in rows
    ]


@router.post("/assignments", response_model=AssignmentResponse, status_code=status.HTTP_201_CREATED)
def create_assignment(
    payload: AssignmentCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_equipment(db, payload.equipment_id, hospital_id)
    # close previous active assignments
    for prev in item.assignments:
        if prev.is_active:
            prev.is_active = False
            prev.returned_at = datetime.now(timezone.utc)
    row = EquipmentAssignment(
        hospital_id=hospital_id,
        equipment_id=item.id,
        target_type=payload.target_type,
        target_name=payload.target_name.strip(),
        assigned_by_name=_actor_name(user),
        remarks=payload.remarks.strip() if payload.remarks else None,
        is_active=True,
    )
    item.status = EquipmentStatus.in_use
    item.current_location = payload.target_name.strip()
    if payload.target_type == EquipmentAssignTarget.department:
        item.department = payload.target_name.strip()
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="equipment_assignment",
        entity_id=row.id,
        summary=f"Assigned {item.asset_id} to {payload.target_type.value}: {payload.target_name}",
    )
    db.commit()
    db.refresh(row)
    return AssignmentResponse(
        id=row.id,
        equipment_id=row.equipment_id,
        equipment_name=item.name,
        asset_id=item.asset_id,
        target_type=row.target_type,
        target_name=row.target_name,
        assigned_by_name=row.assigned_by_name,
        assigned_at=row.assigned_at,
        returned_at=row.returned_at,
        is_active=row.is_active,
        remarks=row.remarks,
    )


@router.post("/assignments/{assignment_id}/return", response_model=AssignmentResponse)
def return_assignment(
    assignment_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(EquipmentAssignment)
        .options(joinedload(EquipmentAssignment.equipment))
        .filter(EquipmentAssignment.id == assignment_id, EquipmentAssignment.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    row.is_active = False
    row.returned_at = datetime.now(timezone.utc)
    if row.equipment and row.equipment.status == EquipmentStatus.in_use:
        row.equipment.status = EquipmentStatus.available
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="equipment_assignment",
        entity_id=row.id,
        summary=f"Returned equipment assignment {assignment_id}",
    )
    db.commit()
    return AssignmentResponse(
        id=row.id,
        equipment_id=row.equipment_id,
        equipment_name=row.equipment.name if row.equipment else None,
        asset_id=row.equipment.asset_id if row.equipment else None,
        target_type=row.target_type,
        target_name=row.target_name,
        assigned_by_name=row.assigned_by_name,
        assigned_at=row.assigned_at,
        returned_at=row.returned_at,
        is_active=row.is_active,
        remarks=row.remarks,
    )


# ── Maintenance ───────────────────────────────────────────────────────────────

@router.get("/maintenance", response_model=list[MaintenanceResponse])
def list_maintenance(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    rows = (
        db.query(EquipmentMaintenance)
        .options(joinedload(EquipmentMaintenance.equipment))
        .filter(EquipmentMaintenance.hospital_id == hospital_id)
        .order_by(EquipmentMaintenance.next_service_date.asc())
        .limit(300)
        .all()
    )
    out: list[MaintenanceResponse] = []
    for r in rows:
        if r.status != MaintenanceStatus.completed:
            _refresh_maintenance_status(r)
        out.append(
            MaintenanceResponse(
                id=r.id,
                equipment_id=r.equipment_id,
                equipment_name=r.equipment.name if r.equipment else None,
                asset_id=r.equipment.asset_id if r.equipment else None,
                last_service_date=r.last_service_date,
                next_service_date=r.next_service_date,
                status=r.status,
                remarks=r.remarks,
                created_at=r.created_at,
                completed_at=r.completed_at,
            )
        )
    db.commit()
    return out


@router.post("/maintenance", response_model=MaintenanceResponse, status_code=status.HTTP_201_CREATED)
def schedule_maintenance(
    payload: MaintenanceCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_equipment(db, payload.equipment_id, hospital_id)
    row = EquipmentMaintenance(
        hospital_id=hospital_id,
        equipment_id=item.id,
        last_service_date=payload.last_service_date,
        next_service_date=payload.next_service_date,
        remarks=payload.remarks.strip() if payload.remarks else None,
        status=MaintenanceStatus.scheduled,
    )
    _refresh_maintenance_status(row)
    item.status = EquipmentStatus.under_maintenance if row.status in (
        MaintenanceStatus.due,
        MaintenanceStatus.overdue,
    ) else item.status
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="equipment_maintenance",
        entity_id=row.id,
        summary=f"Scheduled maintenance for {item.asset_id}",
    )
    db.commit()
    return MaintenanceResponse(
        id=row.id,
        equipment_id=row.equipment_id,
        equipment_name=item.name,
        asset_id=item.asset_id,
        last_service_date=row.last_service_date,
        next_service_date=row.next_service_date,
        status=row.status,
        remarks=row.remarks,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


@router.post("/maintenance/{maintenance_id}/complete", response_model=MaintenanceResponse)
def complete_maintenance(
    maintenance_id: UUID,
    payload: MaintenanceComplete,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(EquipmentMaintenance)
        .options(joinedload(EquipmentMaintenance.equipment))
        .filter(EquipmentMaintenance.id == maintenance_id, EquipmentMaintenance.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Maintenance record not found")
    today = date.today()
    row.status = MaintenanceStatus.completed
    row.completed_at = datetime.now(timezone.utc)
    row.last_service_date = today
    if payload.remarks:
        row.remarks = ((row.remarks or "") + "\n" + payload.remarks.strip()).strip()
    if payload.next_service_date:
        row.next_service_date = payload.next_service_date
        # create follow-up schedule as new row optional - update same next date for display

    log = EquipmentServiceLog(
        hospital_id=hospital_id,
        equipment_id=row.equipment_id,
        service_date=today,
        work_done=payload.work_done.strip(),
        engineer=payload.engineer.strip() if payload.engineer else None,
        cost=payload.cost,
        remarks=payload.remarks.strip() if payload.remarks else None,
    )
    db.add(log)
    if row.equipment and row.equipment.status == EquipmentStatus.under_maintenance:
        row.equipment.status = EquipmentStatus.available

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="equipment_maintenance",
        entity_id=row.id,
        summary=f"Completed maintenance for {row.equipment.asset_id if row.equipment else maintenance_id}",
    )
    db.commit()
    return MaintenanceResponse(
        id=row.id,
        equipment_id=row.equipment_id,
        equipment_name=row.equipment.name if row.equipment else None,
        asset_id=row.equipment.asset_id if row.equipment else None,
        last_service_date=row.last_service_date,
        next_service_date=row.next_service_date,
        status=row.status,
        remarks=row.remarks,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


# ── Service History ───────────────────────────────────────────────────────────

@router.get("/service-logs", response_model=list[ServiceLogResponse])
def list_service_logs(
    equipment_id: UUID | None = None,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(EquipmentServiceLog)
        .options(joinedload(EquipmentServiceLog.equipment))
        .filter(EquipmentServiceLog.hospital_id == hospital_id)
    )
    if equipment_id:
        q = q.filter(EquipmentServiceLog.equipment_id == equipment_id)
    rows = q.order_by(EquipmentServiceLog.service_date.desc()).limit(300).all()
    return [
        ServiceLogResponse(
            id=r.id,
            equipment_id=r.equipment_id,
            equipment_name=r.equipment.name if r.equipment else None,
            asset_id=r.equipment.asset_id if r.equipment else None,
            service_date=r.service_date,
            work_done=r.work_done,
            engineer=r.engineer,
            cost=r.cost,
            remarks=r.remarks,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/service-logs", response_model=ServiceLogResponse, status_code=status.HTTP_201_CREATED)
def create_service_log(
    payload: ServiceLogCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_equipment(db, payload.equipment_id, hospital_id)
    row = EquipmentServiceLog(
        hospital_id=hospital_id,
        equipment_id=item.id,
        service_date=payload.service_date,
        work_done=payload.work_done.strip(),
        engineer=payload.engineer.strip() if payload.engineer else None,
        cost=payload.cost,
        remarks=payload.remarks.strip() if payload.remarks else None,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="equipment_service",
        entity_id=row.id,
        summary=f"Service log for {item.asset_id}: {payload.work_done[:80]}",
    )
    db.commit()
    db.refresh(row)
    return ServiceLogResponse(
        id=row.id,
        equipment_id=row.equipment_id,
        equipment_name=item.name,
        asset_id=item.asset_id,
        service_date=row.service_date,
        work_done=row.work_done,
        engineer=row.engineer,
        cost=row.cost,
        remarks=row.remarks,
        created_at=row.created_at,
    )


# ── Requests ──────────────────────────────────────────────────────────────────

@router.get("/requests", response_model=list[RequestResponse])
def list_requests(
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(EquipmentRequest).filter(EquipmentRequest.hospital_id == hospital_id)
    if status_filter:
        try:
            q = q.filter(EquipmentRequest.status == EquipmentRequestStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
    return q.order_by(EquipmentRequest.created_at.desc()).limit(300).all()


@router.post("/requests", response_model=RequestResponse, status_code=status.HTTP_201_CREATED)
def create_request(
    payload: RequestCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = EquipmentRequest(
        hospital_id=hospital_id,
        request_no=_next_request_no(db, hospital_id),
        department=payload.department.strip(),
        equipment_name=payload.equipment_name.strip(),
        quantity=payload.quantity,
        remarks=payload.remarks.strip() if payload.remarks else None,
        requested_by_name=_actor_name(user),
        status=EquipmentRequestStatus.pending,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="equipment_request",
        entity_id=row.id,
        summary=f"Equipment request {row.request_no}: {row.equipment_name} x{row.quantity}",
    )
    db.commit()
    db.refresh(row)
    return row


@router.post("/requests/{request_id}/approve", response_model=RequestResponse)
def approve_request(
    request_id: UUID,
    payload: RequestAction | None = None,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(EquipmentRequest)
        .filter(EquipmentRequest.id == request_id, EquipmentRequest.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if row.status != EquipmentRequestStatus.pending:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending requests can be approved")
    row.status = EquipmentRequestStatus.approved
    row.resolved_at = datetime.now(timezone.utc)
    if payload and payload.admin_remarks:
        row.admin_remarks = payload.admin_remarks.strip()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="equipment_request",
        entity_id=row.id,
        summary=f"Approved request {row.request_no}",
    )
    db.commit()
    db.refresh(row)
    return row


@router.post("/requests/{request_id}/reject", response_model=RequestResponse)
def reject_request(
    request_id: UUID,
    payload: RequestAction | None = None,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(EquipmentRequest)
        .filter(EquipmentRequest.id == request_id, EquipmentRequest.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if row.status not in (EquipmentRequestStatus.pending, EquipmentRequestStatus.approved):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request cannot be rejected")
    row.status = EquipmentRequestStatus.rejected
    row.resolved_at = datetime.now(timezone.utc)
    if payload and payload.admin_remarks:
        row.admin_remarks = payload.admin_remarks.strip()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="equipment_request",
        entity_id=row.id,
        summary=f"Rejected request {row.request_no}",
    )
    db.commit()
    db.refresh(row)
    return row


@router.post("/requests/{request_id}/assign", response_model=RequestResponse)
def assign_request(
    request_id: UUID,
    payload: RequestAction,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = (
        db.query(EquipmentRequest)
        .filter(EquipmentRequest.id == request_id, EquipmentRequest.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if row.status not in (EquipmentRequestStatus.pending, EquipmentRequestStatus.approved):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request cannot be assigned")
    if not payload.equipment_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="equipment_id is required")
    item = _get_equipment(db, payload.equipment_id, hospital_id)
    if item.status not in (EquipmentStatus.available, EquipmentStatus.in_use):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Equipment not available to assign")

    for prev in item.assignments:
        if prev.is_active:
            prev.is_active = False
            prev.returned_at = datetime.now(timezone.utc)

    assignment = EquipmentAssignment(
        hospital_id=hospital_id,
        equipment_id=item.id,
        target_type=EquipmentAssignTarget.department,
        target_name=row.department,
        assigned_by_name=_actor_name(user),
        remarks=f"Via request {row.request_no}",
        is_active=True,
    )
    db.add(assignment)
    item.status = EquipmentStatus.in_use
    item.department = row.department
    item.current_location = row.department
    row.status = EquipmentRequestStatus.assigned
    row.assigned_equipment_id = item.id
    row.resolved_at = datetime.now(timezone.utc)
    if payload.admin_remarks:
        row.admin_remarks = payload.admin_remarks.strip()

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="equipment_request",
        entity_id=row.id,
        summary=f"Assigned {item.asset_id} for request {row.request_no}",
    )
    db.commit()
    db.refresh(row)
    return row
