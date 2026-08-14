import re
from collections import defaultdict
from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    AuditLog,
    Department,
    Holiday,
    Hospital,
    HospitalUser,
    RoleCustomField,
    RolePermission,
    ShiftType,
    StaffDailyShift,
    StaffRole,
)
from app.schemas_admin import (
    BASIC_MODULE_KEYS,
    BASIC_MODULE_LABELS,
    HospitalUserCreate,
    HospitalUserResponse,
    HospitalUserUpdate,
    ModuleInfo,
    RoleCreate,
    RoleFieldResponse,
    RolePermissionResponse,
    RoleResponse,
    RoleUpdate,
    ShiftRosterAssign,
    ShiftRosterEntry,
    ShiftRosterResponse,
    ShiftRosterSeed,
    ShiftRosterSnapshot,
)
from app.utils.audit import write_audit
from app.utils.auth import get_current_user, get_hospital_uuid, require_hospital_admin
from app.utils.password import hash_password

router = APIRouter(prefix="/admin", tags=["admin"])


def _slug_key(label: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_") or "field"
    key = base[:60]
    i = 2
    while key in used:
        key = f"{base[:55]}_{i}"
        i += 1
    used.add(key)
    return key


def _role_to_response(role: StaffRole) -> RoleResponse:
    return RoleResponse(
        id=role.id,
        hospital_id=role.hospital_id,
        name=role.name,
        description=role.description,
        is_active=role.is_active,
        created_at=role.created_at,
        fields=[RoleFieldResponse.model_validate(f) for f in sorted(role.fields, key=lambda x: x.sort_order)],
        permissions=[RolePermissionResponse.model_validate(p) for p in role.permissions],
    )


def _get_role(db: Session, role_id: UUID, hospital_id: UUID) -> StaffRole:
    role = (
        db.query(StaffRole)
        .options(joinedload(StaffRole.fields), joinedload(StaffRole.permissions))
        .filter(StaffRole.id == role_id, StaffRole.hospital_id == hospital_id)
        .first()
    )
    if not role:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return role


def _sync_fields(db: Session, role: StaffRole, hospital_id: UUID, fields_in) -> None:
    role.fields.clear()
    db.flush()
    used: set[str] = set()
    for idx, field in enumerate(fields_in):
        role.fields.append(
            RoleCustomField(
                hospital_id=hospital_id,
                role_id=role.id,
                label=field.label.strip(),
                field_key=_slug_key(field.label, used),
                field_type=field.field_type,
                options=field.options.strip() if field.options else None,
                is_required=field.is_required,
                sort_order=field.sort_order if field.sort_order else idx,
            )
        )


def _sync_permissions(db: Session, role: StaffRole, hospital_id: UUID, perms_in) -> None:
    role.permissions.clear()
    db.flush()
    seen: set[str] = set()
    for perm in perms_in:
        key = perm.module_key.strip()
        if key not in BASIC_MODULE_KEYS or key in seen:
            continue
        seen.add(key)
        can_view = bool(perm.can_view or perm.can_edit)
        can_edit = bool(perm.can_edit)
        if not can_view and not can_edit:
            continue
        role.permissions.append(
            RolePermission(
                hospital_id=hospital_id,
                role_id=role.id,
                module_key=key,
                can_view=can_view,
                can_edit=can_edit,
            )
        )


@router.get("/modules", response_model=list[ModuleInfo])
def list_modules(_: dict = Depends(get_current_user)):
    """Return the modules implemented by this backend for any signed-in user."""
    return [ModuleInfo(key=k, label=BASIC_MODULE_LABELS[k]) for k in BASIC_MODULE_KEYS]


# ── Roles ──────────────────────────────────────────────────────────────────────
@router.get("/roles", response_model=list[RoleResponse])
def list_roles(db: Session = Depends(get_db), hospital_id: UUID = Depends(get_hospital_uuid)):
    roles = (
        db.query(StaffRole)
        .options(joinedload(StaffRole.fields), joinedload(StaffRole.permissions))
        .filter(StaffRole.hospital_id == hospital_id)
        .order_by(StaffRole.name)
        .all()
    )
    return [_role_to_response(r) for r in roles]


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    exists = (
        db.query(StaffRole)
        .filter(StaffRole.hospital_id == hospital_id, StaffRole.name == payload.name.strip())
        .first()
    )
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A role with this name already exists.")

    role = StaffRole(
        hospital_id=hospital_id,
        name=payload.name.strip(),
        description=payload.description,
        is_active=payload.is_active,
    )
    db.add(role)
    db.flush()
    _sync_fields(db, role, hospital_id, payload.fields)
    _sync_permissions(db, role, hospital_id, payload.permissions)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="create",
        entity_type="role",
        entity_id=role.id,
        summary=f"Created role '{role.name}'",
    )
    db.commit()
    return _role_to_response(_get_role(db, role.id, hospital_id))


@router.put("/roles/{role_id}", response_model=RoleResponse)
def update_role(
    role_id: UUID,
    payload: RoleUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    role = _get_role(db, role_id, hospital_id)
    data = payload.model_dump(exclude_unset=True, exclude={"fields", "permissions"})
    if "name" in data and data["name"]:
        data["name"] = data["name"].strip()
        clash = (
            db.query(StaffRole)
            .filter(
                StaffRole.hospital_id == hospital_id,
                StaffRole.name == data["name"],
                StaffRole.id != role_id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A role with this name already exists.")
    for key, value in data.items():
        setattr(role, key, value)
    if payload.fields is not None:
        _sync_fields(db, role, hospital_id, payload.fields)
    if payload.permissions is not None:
        _sync_permissions(db, role, hospital_id, payload.permissions)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="update",
        entity_type="role",
        entity_id=role.id,
        summary=f"Updated role '{role.name}'",
    )
    db.commit()
    return _role_to_response(_get_role(db, role_id, hospital_id))


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    role = _get_role(db, role_id, hospital_id)
    user_count = db.query(HospitalUser).filter(HospitalUser.role_id == role_id).count()
    if user_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete role while {user_count} user(s) are assigned to it.",
        )
    role_name = role.name
    db.delete(role)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="delete",
        entity_type="role",
        entity_id=role_id,
        summary=f"Deleted role '{role_name}'",
    )
    db.commit()


# ── Users ──────────────────────────────────────────────────────────────────────
def _fmt_shift_time(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:5]
    return value.strftime("%H:%M")


def _get_shift(
    db: Session,
    shift_id: UUID,
    hospital_id: UUID,
    *,
    require_active: bool = True,
) -> ShiftType:
    shift = (
        db.query(ShiftType)
        .options(joinedload(ShiftType.department))
        .filter(ShiftType.id == shift_id, ShiftType.hospital_id == hospital_id)
        .first()
    )
    if not shift:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shift not found")
    if require_active and not shift.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected shift is inactive")
    return shift


def _get_department(db: Session, department_id: UUID, hospital_id: UUID) -> Department:
    dept = (
        db.query(Department)
        .filter(Department.id == department_id, Department.hospital_id == hospital_id)
        .first()
    )
    if not dept:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
    return dept


def _validate_department_shift(
    db: Session,
    hospital_id: UUID,
    *,
    department_id: UUID | None,
    shift_id: UUID | None,
    require_active_shift: bool = True,
) -> ShiftType | None:
    """Ensure department and shift belong to this hospital and match each other when both are set."""
    if department_id is not None:
        _get_department(db, department_id, hospital_id)
    if not shift_id:
        return None
    shift = _get_shift(db, shift_id, hospital_id, require_active=require_active_shift)
    if department_id is not None and shift.department_id != department_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected shift does not belong to the selected department",
        )
    return shift


def _clean_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _user_to_response(user: HospitalUser) -> HospitalUserResponse:
    shift = user.shift
    return HospitalUserResponse(
        id=user.id,
        hospital_id=user.hospital_id,
        role_id=user.role_id,
        shift_id=user.shift_id,
        name=user.name,
        phone=user.phone,
        email=user.email,
        specialization=user.specialization,
        medical_registration_number=user.medical_registration_number,
        qualification=user.qualification,
        years_of_experience=user.years_of_experience,
        consultation_room=user.consultation_room,
        show_financial_details=bool(getattr(user, "show_financial_details", True)),
        custom_values=user.custom_values or {},
        is_active=user.is_active,
        created_at=user.created_at,
        role_name=user.role.name if user.role else None,
        shift_name=shift.name if shift else None,
        shift_department_id=shift.department_id if shift else None,
        shift_department_name=shift.department.name if shift and shift.department else None,
        shift_start_time=_fmt_shift_time(shift.start_time) if shift else None,
        shift_end_time=_fmt_shift_time(shift.end_time) if shift else None,
    )


def _load_user(db: Session, user_id: UUID, hospital_id: UUID | None = None) -> HospitalUser | None:
    q = (
        db.query(HospitalUser)
        .options(
            joinedload(HospitalUser.role),
            joinedload(HospitalUser.shift).joinedload(ShiftType.department),
        )
        .filter(HospitalUser.id == user_id)
    )
    if hospital_id is not None:
        q = q.filter(HospitalUser.hospital_id == hospital_id)
    return q.first()


@router.get("/users", response_model=list[HospitalUserResponse])
def list_users(db: Session = Depends(get_db), hospital_id: UUID = Depends(get_hospital_uuid)):
    users = (
        db.query(HospitalUser)
        .options(
            joinedload(HospitalUser.role),
            joinedload(HospitalUser.shift).joinedload(ShiftType.department),
        )
        .filter(HospitalUser.hospital_id == hospital_id)
        .order_by(HospitalUser.name)
        .all()
    )
    return [_user_to_response(u) for u in users]


@router.post("/users", response_model=HospitalUserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: HospitalUserCreate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    role = _get_role(db, payload.role_id, hospital_id)
    email = payload.email.strip().lower()
    exists = (
        db.query(HospitalUser)
        .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.email == email)
        .first()
    )
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists.")

    shift_id = None
    shift_label = None
    if payload.shift_id or payload.department_id:
        shift = _validate_department_shift(
            db,
            hospital_id,
            department_id=payload.department_id,
            shift_id=payload.shift_id,
        )
        if shift:
            shift_id = shift.id
            shift_label = shift.name

    user = HospitalUser(
        hospital_id=hospital_id,
        role_id=payload.role_id,
        shift_id=shift_id,
        name=payload.name.strip(),
        phone=payload.phone.strip(),
        email=email,
        password_hash=hash_password(payload.password),
        specialization=_clean_optional_str(payload.specialization),
        medical_registration_number=_clean_optional_str(payload.medical_registration_number),
        qualification=_clean_optional_str(payload.qualification),
        years_of_experience=payload.years_of_experience,
        consultation_room=_clean_optional_str(payload.consultation_room),
        show_financial_details=bool(payload.show_financial_details),
        custom_values=payload.custom_values or {},
        is_active=payload.is_active,
    )
    db.add(user)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="create",
        entity_type="user",
        entity_id=user.id,
        summary=f"Created user '{user.name}' with role '{role.name}'",
        details={"email": user.email, "role": role.name, "shift": shift_label},
    )
    db.commit()
    user = _load_user(db, user.id, hospital_id)
    return _user_to_response(user)


@router.put("/users/{user_id}", response_model=HospitalUserResponse)
def update_user(
    user_id: UUID,
    payload: HospitalUserUpdate,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    user = _load_user(db, user_id, hospital_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    data = payload.model_dump(exclude_unset=True, exclude={"password", "department_id"})
    raw = payload.model_dump(exclude_unset=True)
    if "role_id" in data and data["role_id"]:
        _get_role(db, data["role_id"], hospital_id)
    if "shift_id" in data or "department_id" in raw:
        effective_shift_id = data["shift_id"] if "shift_id" in data else user.shift_id
        if "shift_id" in data and not data["shift_id"]:
            effective_shift_id = None
            data["shift_id"] = None
        same_shift = effective_shift_id is not None and user.shift_id == effective_shift_id
        dept_for_check = raw.get("department_id") if "department_id" in raw else None
        if effective_shift_id is not None or "department_id" in raw:
            _validate_department_shift(
                db,
                hospital_id,
                department_id=dept_for_check,
                shift_id=effective_shift_id,
                require_active_shift=not same_shift,
            )
    if "email" in data and data["email"]:
        data["email"] = str(data["email"]).strip().lower()
        clash = (
            db.query(HospitalUser)
            .filter(
                HospitalUser.hospital_id == hospital_id,
                HospitalUser.email == data["email"],
                HospitalUser.id != user_id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists.")
    if "name" in data and data["name"]:
        data["name"] = data["name"].strip()
    if "phone" in data and data["phone"]:
        data["phone"] = data["phone"].strip()
    for key in (
        "specialization",
        "medical_registration_number",
        "qualification",
        "consultation_room",
    ):
        if key in data:
            data[key] = _clean_optional_str(data[key])
    for key, value in data.items():
        setattr(user, key, value)
    if payload.password:
        user.password_hash = hash_password(payload.password)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="update",
        entity_type="user",
        entity_id=user.id,
        summary=f"Updated user '{user.name}'",
        details={"email": user.email, "password_changed": bool(payload.password)},
    )
    db.commit()
    user = _load_user(db, user_id, hospital_id)
    return _user_to_response(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: UUID,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    actor: dict = Depends(require_hospital_admin),
):
    user = db.query(HospitalUser).filter(HospitalUser.id == user_id, HospitalUser.hospital_id == hospital_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user_name = user.name
    user_email = user.email
    db.delete(user)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=actor,
        action="delete",
        entity_type="user",
        entity_id=user_id,
        summary=f"Deleted user '{user_name}'",
        details={"email": user_email},
    )
    db.commit()


# ── Audit logs ─────────────────────────────────────────────────────────────────
class AuditLogResponse(BaseModel):
    id: UUID
    hospital_id: UUID
    actor_email: str
    actor_name: str
    actor_role: str
    actor_role_label: str | None
    action: str
    entity_type: str
    entity_id: str | None
    summary: str
    details: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/audit-logs", response_model=list[AuditLogResponse])
def list_audit_logs(
    search: str | None = Query(default=None),
    action: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    query = db.query(AuditLog).filter(AuditLog.hospital_id == hospital_id)
    if action:
        query = query.filter(AuditLog.action == action.strip().lower())
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type.strip().lower())
    if search:
        term = f"%{search.strip()}%"
        query = query.filter(
            (AuditLog.summary.ilike(term))
            | (AuditLog.actor_name.ilike(term))
            | (AuditLog.actor_email.ilike(term))
            | (AuditLog.actor_role_label.ilike(term))
        )
    return query.order_by(AuditLog.created_at.desc()).limit(limit).all()


# ── Daily shift roster ──────────────────────────────────────────────────────────
def _fmt_time(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    return str(value)[:5]


def _holiday_name_for(db: Session, hospital_id: UUID, roster_date: date) -> str | None:
    row = (
        db.query(Holiday)
        .filter(Holiday.hospital_id == hospital_id, Holiday.holiday_date == roster_date)
        .order_by(Holiday.name)
        .first()
    )
    return row.name if row else None


def _build_roster_entries(db: Session, hospital_id: UUID, roster_date: date) -> list[ShiftRosterEntry]:
    users = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role), joinedload(HospitalUser.shift).joinedload(ShiftType.department))
        .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .order_by(HospitalUser.name)
        .all()
    )
    overrides = {
        row.user_id: row
        for row in db.query(StaffDailyShift)
        .options(joinedload(StaffDailyShift.shift).joinedload(ShiftType.department))
        .filter(StaffDailyShift.hospital_id == hospital_id, StaffDailyShift.roster_date == roster_date)
        .all()
    }
    shift_ids = {o.shift_id for o in overrides.values() if o.shift_id}
    shift_ids |= {u.shift_id for u in users if u.shift_id}
    shifts_by_id: dict[UUID, ShiftType] = {}
    if shift_ids:
        for s in (
            db.query(ShiftType)
            .options(joinedload(ShiftType.department))
            .filter(ShiftType.id.in_(shift_ids))
            .all()
        ):
            shifts_by_id[s.id] = s

    entries: list[ShiftRosterEntry] = []
    for user in users:
        override = overrides.get(user.id)
        if override:
            status_val = override.status or "on_duty"
            shift = shifts_by_id.get(override.shift_id) if override.shift_id else None
            if status_val in ("off", "leave"):
                shift = None
            entries.append(
                ShiftRosterEntry(
                    user_id=user.id,
                    name=user.name,
                    phone=user.phone,
                    role_id=user.role_id,
                    role_name=user.role.name if user.role else None,
                    shift_id=shift.id if shift else None,
                    shift_name=shift.name if shift else None,
                    department_id=shift.department_id if shift else None,
                    department_name=shift.department.name if shift and shift.department else None,
                    start_time=_fmt_time(shift.start_time) if shift else None,
                    end_time=_fmt_time(shift.end_time) if shift else None,
                    status=status_val,
                    is_override=True,
                    notes=override.notes,
                    default_shift_id=user.shift_id,
                )
            )
        else:
            shift = shifts_by_id.get(user.shift_id) if user.shift_id else None
            entries.append(
                ShiftRosterEntry(
                    user_id=user.id,
                    name=user.name,
                    phone=user.phone,
                    role_id=user.role_id,
                    role_name=user.role.name if user.role else None,
                    shift_id=shift.id if shift else None,
                    shift_name=shift.name if shift else None,
                    department_id=shift.department_id if shift else None,
                    department_name=shift.department.name if shift and shift.department else None,
                    start_time=_fmt_time(shift.start_time) if shift else None,
                    end_time=_fmt_time(shift.end_time) if shift else None,
                    status="on_duty" if shift else "off",
                    is_override=False,
                    notes=None,
                    default_shift_id=user.shift_id,
                )
            )
    return entries


def _format_roster_snapshot(
    hospital_name: str,
    roster_date: date,
    holiday_name: str | None,
    entries: list[ShiftRosterEntry],
) -> str:
    date_label = roster_date.strftime("%d %b %Y")
    lines = [
        f"*{hospital_name}*",
        f"📅 Daily Shift Roster — {date_label}",
    ]
    if holiday_name:
        lines.append(f"🏖 Holiday: {holiday_name}")
    lines.append("")

    on_duty = [e for e in entries if e.status == "on_duty" and e.shift_id]
    off_leave = [e for e in entries if e.status in ("off", "leave") or not e.shift_id]

    by_dept: dict[str, list[ShiftRosterEntry]] = defaultdict(list)
    for e in on_duty:
        by_dept[e.department_name or "Unassigned"].append(e)

    for dept in sorted(by_dept.keys()):
        lines.append(f"*{dept}*")
        by_shift: dict[str, list[ShiftRosterEntry]] = defaultdict(list)
        for e in by_dept[dept]:
            time_bit = ""
            if e.start_time and e.end_time:
                time_bit = f" ({e.start_time}-{e.end_time})"
            key = f"{e.shift_name or 'Shift'}{time_bit}"
            by_shift[key].append(e)
        for shift_label in sorted(by_shift.keys()):
            lines.append(f"• {shift_label}")
            for e in sorted(by_shift[shift_label], key=lambda x: x.name.lower()):
                role = f" ({e.role_name})" if e.role_name else ""
                lines.append(f"  - {e.name}{role}")
        lines.append("")

    if off_leave:
        lines.append("*Off / Leave / Unassigned*")
        for e in sorted(off_leave, key=lambda x: x.name.lower()):
            status_label = {"leave": "Leave", "off": "Off"}.get(e.status, "Unassigned")
            if e.status == "on_duty" and not e.shift_id:
                status_label = "Unassigned"
            role = f" ({e.role_name})" if e.role_name else ""
            note = f" — {e.notes}" if e.notes else ""
            lines.append(f"- {e.name}{role} — {status_label}{note}")
        lines.append("")

    lines.append(f"_Total staff: {len(entries)}_")
    lines.append("_Shared from Ultrion HMS_")
    return "\n".join(lines).strip() + "\n"


@router.get("/shift-roster", response_model=ShiftRosterResponse)
def get_shift_roster(
    roster_date: date = Query(..., description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    entries = _build_roster_entries(db, hospital_id, roster_date)
    return ShiftRosterResponse(
        roster_date=roster_date,
        hospital_name=hospital.name if hospital else "Hospital",
        holiday_name=_holiday_name_for(db, hospital_id, roster_date),
        entries=entries,
    )


@router.put("/shift-roster", response_model=ShiftRosterEntry)
def assign_shift_roster(
    payload: ShiftRosterAssign,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    _: dict = Depends(require_hospital_admin),
):
    user = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.id == payload.user_id, HospitalUser.hospital_id == hospital_id)
        .first()
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff user not found")

    status_val = payload.status
    shift_id = payload.shift_id
    if status_val in ("off", "leave"):
        shift_id = None
    elif shift_id:
        shift = (
            db.query(ShiftType)
            .filter(ShiftType.id == shift_id, ShiftType.hospital_id == hospital_id)
            .first()
        )
        if not shift:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shift type not found")
        if not shift.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Shift type is inactive")
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="shift_id is required when status is on_duty",
        )

    row = (
        db.query(StaffDailyShift)
        .filter(
            StaffDailyShift.hospital_id == hospital_id,
            StaffDailyShift.user_id == payload.user_id,
            StaffDailyShift.roster_date == payload.roster_date,
        )
        .first()
    )
    if row:
        row.shift_id = shift_id
        row.status = status_val
        row.notes = payload.notes
    else:
        row = StaffDailyShift(
            hospital_id=hospital_id,
            user_id=payload.user_id,
            roster_date=payload.roster_date,
            shift_id=shift_id,
            status=status_val,
            notes=payload.notes,
        )
        db.add(row)
    db.commit()

    entries = _build_roster_entries(db, hospital_id, payload.roster_date)
    for e in entries:
        if e.user_id == payload.user_id:
            return e
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load assignment")


@router.post("/shift-roster/seed", response_model=ShiftRosterResponse)
def seed_shift_roster(
    payload: ShiftRosterSeed,
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    _: dict = Depends(require_hospital_admin),
):
    """Copy each staff member's default shift into the daily roster for the date."""
    users = (
        db.query(HospitalUser)
        .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .all()
    )
    existing = {
        row.user_id: row
        for row in db.query(StaffDailyShift)
        .filter(StaffDailyShift.hospital_id == hospital_id, StaffDailyShift.roster_date == payload.roster_date)
        .all()
    }
    for user in users:
        if user.id in existing and not payload.overwrite:
            continue
        status_val = "on_duty" if user.shift_id else "off"
        if user.id in existing:
            row = existing[user.id]
            row.shift_id = user.shift_id
            row.status = status_val
            row.notes = None
        else:
            db.add(
                StaffDailyShift(
                    hospital_id=hospital_id,
                    user_id=user.id,
                    roster_date=payload.roster_date,
                    shift_id=user.shift_id,
                    status=status_val,
                    notes=None,
                )
            )
    db.commit()
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    entries = _build_roster_entries(db, hospital_id, payload.roster_date)
    return ShiftRosterResponse(
        roster_date=payload.roster_date,
        hospital_name=hospital.name if hospital else "Hospital",
        holiday_name=_holiday_name_for(db, hospital_id, payload.roster_date),
        entries=entries,
    )


@router.delete("/shift-roster", status_code=status.HTTP_204_NO_CONTENT)
def clear_shift_roster_overrides(
    roster_date: date = Query(...),
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
    _: dict = Depends(require_hospital_admin),
):
    """Remove day-specific overrides so staff fall back to default shifts."""
    db.query(StaffDailyShift).filter(
        StaffDailyShift.hospital_id == hospital_id,
        StaffDailyShift.roster_date == roster_date,
    ).delete(synchronize_session=False)
    db.commit()


@router.get("/shift-roster/snapshot", response_model=ShiftRosterSnapshot)
def get_shift_roster_snapshot(
    roster_date: date = Query(...),
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_uuid),
):
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    hospital_name = hospital.name if hospital else "Hospital"
    holiday_name = _holiday_name_for(db, hospital_id, roster_date)
    entries = _build_roster_entries(db, hospital_id, roster_date)
    text = _format_roster_snapshot(hospital_name, roster_date, holiday_name, entries)
    return ShiftRosterSnapshot(
        roster_date=roster_date,
        hospital_name=hospital_name,
        holiday_name=holiday_name,
        text=text,
        entries_count=len(entries),
    )
