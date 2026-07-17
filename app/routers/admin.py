import re
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import AuditLog, HospitalUser, RoleCustomField, RolePermission, ShiftType, StaffRole
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
        custom_values=user.custom_values or {},
        is_active=user.is_active,
        created_at=user.created_at,
        role_name=user.role.name if user.role else None,
        shift_name=shift.name if shift else None,
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
    if payload.shift_id:
        shift = _get_shift(db, payload.shift_id, hospital_id)
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

    data = payload.model_dump(exclude_unset=True, exclude={"password"})
    if "role_id" in data and data["role_id"]:
        _get_role(db, data["role_id"], hospital_id)
    if "shift_id" in data:
        if data["shift_id"]:
            same = user.shift_id == data["shift_id"]
            _get_shift(db, data["shift_id"], hospital_id, require_active=not same)
        else:
            data["shift_id"] = None
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
