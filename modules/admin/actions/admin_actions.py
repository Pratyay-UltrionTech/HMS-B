"""
Target-native implementation of Admin domain actions.

Handles:
- Modules catalog
- Staff roles, custom fields, and permissions
- Hospital users CRUD with password hashing and audit logging
- System audit log queries with filtering and pagination
- Daily staff shift roster assignment, seeding, clearing, and snapshot generation
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload, load_only

from shared.sync import stage_sync_event

from modules.admin.contracts.admin_contracts import (
    BASIC_MODULE_KEYS,
    BASIC_MODULE_LABELS,
    AuditLogResponse,
    HospitalUserCreate,
    HospitalUserListItem,
    HospitalUserPage,
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
    StaffRosterSegmentItem,
    RosterDutySegmentCreate,
    RosterDutySegmentUpdate,
    StaffRosterDayUpdate,
    HospitalFacilitySettings,
    HospitalFacilitySettingsUpdate,
    WorkPatternSegmentItem,
    StaffWorkPatternResponse,
    StaffWorkPatternUpdate,
    WorkPatternSegmentCreate,
    WorkPatternSegmentUpdate,
    UserRoleAssignmentResponse,
    UserDepartmentAssignmentResponse,
    UserLocationAssignmentResponse,
)
from modules.doctors.services.effective_availability_service import intervals_overlap
from modules.doctors.entities.doctor import (
    Holiday,
    HospitalUser,
    HospitalUserDepartment,
    HospitalUserLocation,
    HospitalUserRole,
    Practitioner,
    RoleCustomField,
    RolePermission,
    ShiftType,
    StaffDailyShift,
    StaffRole,
    StaffRosterDay,
    StaffRosterSegment,
    StaffWorkPattern,
    StaffWorkPatternSegment,
)
from modules.beds.entities.bed import Ward
from modules.masters.actions.integrity import assert_unreferenced
from modules.masters.entities.organization_entities import Department, Wing
from modules.tenancy.entities.hospital import Hospital
from shared.audit import write_audit_log
from shared.audit.entities.audit_log import AuditLog
from shared.auth.dependencies import invalidate_role_permission_cache
from shared.auth.security import hash_password
from shared.holidays import get_effective_holiday


def slug_key(label: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_") or "field"
    key = base[:60]
    i = 2
    while key in used:
        key = f"{base[:55]}_{i}"
        i += 1
    used.add(key)
    return key


def fmt_shift_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:5]
    return value.strftime("%H:%M")


def fmt_time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    return str(value)[:5]


def clean_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def parse_time_str(val: str) -> time:
    parts = val.split(":")
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


def compute_segment_interval(base_date: date, start_time_str: str, end_time_str: str) -> tuple[datetime, datetime]:
    st = parse_time_str(start_time_str)
    et = parse_time_str(end_time_str)
    start_at = datetime.combine(base_date, st)
    end_at = datetime.combine(base_date, et)
    if et <= st:
        end_at += timedelta(days=1)
    return start_at, end_at


def check_segment_overlap(
    db: Session,
    hospital_id: UUID,
    user_id: UUID,
    start_at: datetime,
    end_at: datetime,
    presence_type: str = "ONSITE",
    exclude_segment_id: UUID | None = None,
) -> None:
    if presence_type != "ONSITE":
        return

    query = (
        db.query(StaffRosterSegment)
        .join(StaffRosterDay, StaffRosterSegment.roster_day_id == StaffRosterDay.id)
        .filter(
            StaffRosterSegment.hospital_id == hospital_id,
            StaffRosterSegment.user_id == user_id,
            StaffRosterSegment.presence_type == "ONSITE",
            StaffRosterDay.status.in_(["working", "on_duty"]),
            StaffRosterSegment.start_at < end_at,
            StaffRosterSegment.end_at > start_at,
        )
    )
    if exclude_segment_id:
        query = query.filter(StaffRosterSegment.id != exclude_segment_id)

    conflict = query.first()
    if conflict:
        c_start = conflict.start_at.strftime("%H:%M")
        c_end = conflict.end_at.strftime("%H:%M")
        if conflict.start_at.date() != conflict.end_at.date():
            c_end += " (+1d)"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Duty segment overlaps with existing assignment ({conflict.duty_kind} {c_start}–{c_end}).",
        )



class AdminActions:
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

    # ── Modules ───────────────────────────────────────────────────────────────
    def list_modules(self) -> list[ModuleInfo]:
        return [ModuleInfo(key=k, label=BASIC_MODULE_LABELS[k]) for k in BASIC_MODULE_KEYS]

    # ── Roles ─────────────────────────────────────────────────────────────────
    def _role_to_response(self, role: StaffRole) -> RoleResponse:
        fields = getattr(role, "custom_fields", None) or getattr(role, "fields", [])
        return RoleResponse(
            id=role.id,
            hospital_id=role.hospital_id,
            name=role.name,
            description=role.description,
            is_active=role.is_active,
            created_at=role.created_at,
            fields=[RoleFieldResponse.model_validate(f) for f in sorted(fields, key=lambda x: x.sort_order)],
            permissions=[RolePermissionResponse.model_validate(p) for p in role.permissions],
        )

    def _get_role(self, role_id: UUID) -> StaffRole:
        role = (
            self.db.query(StaffRole)
            .options(selectinload(StaffRole.custom_fields), selectinload(StaffRole.permissions))
            .filter(StaffRole.id == role_id, StaffRole.hospital_id == self.hospital_id)
            .first()
        )
        if not role:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
        return role

    def list_roles(self) -> list[RoleResponse]:
        roles = (
            self.db.query(StaffRole)
            .options(selectinload(StaffRole.custom_fields), selectinload(StaffRole.permissions))
            .filter(StaffRole.hospital_id == self.hospital_id)
            .order_by(StaffRole.name)
            .all()
        )
        return [self._role_to_response(r) for r in roles]

    def create_role(self, payload: RoleCreate) -> RoleResponse:
        name = payload.name.strip()
        exists = (
            self.db.query(StaffRole)
            .filter(StaffRole.hospital_id == self.hospital_id, StaffRole.name.ilike(name))
            .first()
        )
        if exists:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A role with this name already exists.")

        role = StaffRole(
            hospital_id=self.hospital_id,
            name=name,
            description=payload.description.strip() if payload.description else None,
            is_active=payload.is_active,
        )
        self.db.add(role)
        self.db.flush()

        used: set[str] = set()
        for idx, field in enumerate(payload.fields):
            role.custom_fields.append(
                RoleCustomField(
                    hospital_id=self.hospital_id,
                    role_id=role.id,
                    label=field.label.strip(),
                    field_key=slug_key(field.label, used),
                    field_type=field.field_type,
                    options=field.options.strip() if field.options else None,
                    is_required=field.is_required,
                    sort_order=field.sort_order if field.sort_order else idx,
                )
            )

        seen: set[str] = set()
        for perm in payload.permissions:
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
                    hospital_id=self.hospital_id,
                    role_id=role.id,
                    module_key=key,
                    can_view=can_view,
                    can_edit=can_edit,
                    can_create=bool(getattr(perm, "can_create", False)),
                    can_delete=bool(getattr(perm, "can_delete", False)),
                    can_approve=bool(getattr(perm, "can_approve", False)),
                    can_validate=bool(getattr(perm, "can_validate", False)),
                    can_release=bool(getattr(perm, "can_release", False)),
                    can_dispense=bool(getattr(perm, "can_dispense", False)),
                    can_refund=bool(getattr(perm, "can_refund", False)),
                    can_cancel=bool(getattr(perm, "can_cancel", False)),
                    can_administer=bool(getattr(perm, "can_administer", False)),
                )
            )

        stage_sync_event(self.db, self.hospital_id, "staff_role", "create", role.id)
        self._audit("create", "role", role.id, f"Created role '{role.name}'")
        self.db.commit()
        return self._role_to_response(self._get_role(role.id))

    def update_role(self, role_id: UUID, payload: RoleUpdate) -> RoleResponse:
        role = self._get_role(role_id)
        if payload.name is not None:
            name = payload.name.strip()
            clash = (
                self.db.query(StaffRole)
                .filter(
                    StaffRole.hospital_id == self.hospital_id,
                    StaffRole.name.ilike(name),
                    StaffRole.id != role_id,
                )
                .first()
            )
            if clash:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A role with this name already exists.")
            role.name = name

        if payload.description is not None:
            role.description = payload.description.strip() if payload.description else None
        if payload.is_active is False:
            assigned = self.db.query(HospitalUser.id).filter(HospitalUser.hospital_id == self.hospital_id,
                or_(HospitalUser.role_id == role_id, HospitalUser.assigned_roles.any(HospitalUserRole.role_id == role_id))).count()
            if assigned:
                raise HTTPException(status_code=400, detail=f"Cannot deactivate role because it is assigned to {assigned} staff members.")
        if payload.is_active is not None:
            role.is_active = payload.is_active

        if payload.fields is not None:
            role.custom_fields.clear()
            self.db.flush()
            used: set[str] = set()
            for idx, field in enumerate(payload.fields):
                role.custom_fields.append(
                    RoleCustomField(
                        hospital_id=self.hospital_id,
                        role_id=role.id,
                        label=field.label.strip(),
                        field_key=slug_key(field.label, used),
                        field_type=field.field_type,
                        options=field.options.strip() if field.options else None,
                        is_required=field.is_required,
                        sort_order=field.sort_order if field.sort_order else idx,
                    )
                )

        if payload.permissions is not None:
            role.permissions.clear()
            self.db.flush()
            seen: set[str] = set()
            for perm in payload.permissions:
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
                        hospital_id=self.hospital_id,
                        role_id=role.id,
                        module_key=key,
                        can_view=can_view,
                        can_edit=can_edit,
                        can_create=bool(getattr(perm, "can_create", False)),
                        can_delete=bool(getattr(perm, "can_delete", False)),
                        can_approve=bool(getattr(perm, "can_approve", False)),
                        can_validate=bool(getattr(perm, "can_validate", False)),
                        can_release=bool(getattr(perm, "can_release", False)),
                        can_dispense=bool(getattr(perm, "can_dispense", False)),
                        can_refund=bool(getattr(perm, "can_refund", False)),
                        can_cancel=bool(getattr(perm, "can_cancel", False)),
                        can_administer=bool(getattr(perm, "can_administer", False)),
                    )
                )

        # FLAW-008/024: a change to a role's permissions or active state invalidates
        # the in-memory permission cache and the sessions of every user holding the
        # role, so stale JWTs / cached permissions are rejected promptly.
        if payload.permissions is not None or (payload.is_active is not None and not payload.is_active):
            members = (
                self.db.query(HospitalUser)
                .filter(HospitalUser.hospital_id == self.hospital_id, or_(HospitalUser.role_id == role_id, HospitalUser.assigned_roles.any(HospitalUserRole.role_id == role_id)))
                .all()
            )
            for m in members:
                m.token_version = (m.token_version or 1) + 1
            invalidate_role_permission_cache(self.hospital_id, role_id)

        stage_sync_event(self.db, self.hospital_id, "staff_role", "update", role.id)
        self._audit("update", "role", role.id, f"Updated role '{role.name}'")
        self.db.commit()
        return self._role_to_response(self._get_role(role.id))

    def delete_role(self, role_id: UUID) -> None:
        role = self._get_role(role_id)
        assigned = (
            self.db.query(HospitalUser)
            .filter(HospitalUser.hospital_id == self.hospital_id, or_(HospitalUser.role_id == role_id, HospitalUser.assigned_roles.any(HospitalUserRole.role_id == role_id)))
            .count()
        )
        if assigned > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete role: {assigned} active staff user(s) are assigned to it.",
            )
        name = role.name
        invalidate_role_permission_cache(self.hospital_id, role_id)
        self.db.delete(role)
        stage_sync_event(self.db, self.hospital_id, "staff_role", "delete", role_id)
        self._audit("delete", "role", role_id, f"Deleted role '{name}'")
        self.db.commit()

    # ── Users ─────────────────────────────────────────────────────────────────
    def _validate_department_shift(
        self,
        department_id: UUID | None,
        shift_id: UUID | None,
        allowed_department_ids: set[UUID] | None = None,
        require_active_shift: bool = True,
    ) -> ShiftType | None:
        if department_id is not None:
            dept = self.db.query(Department).filter(Department.id == department_id, Department.hospital_id == self.hospital_id).first()
            if not dept:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
        if not shift_id:
            return None
        shift = (
            self.db.query(ShiftType)
            .filter(ShiftType.id == shift_id, ShiftType.hospital_id == self.hospital_id)
            .first()
        )
        if not shift:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shift not found")
        if require_active_shift and not shift.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected shift is inactive")
        allowed = set(allowed_department_ids or ())
        if department_id:
            allowed.add(department_id)
        if allowed and shift.department_id and shift.department_id not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selected shift does not belong to the selected department",
            )
        return shift

    def _user_to_response(
        self, user: HospitalUser, dept_by_id: dict[UUID, Department] | None = None
    ) -> HospitalUserResponse:
        shift = user.shift
        shift_dept = None
        if shift and shift.department_id:
            if dept_by_id is not None:
                shift_dept = dept_by_id.get(shift.department_id)
            else:
                shift_dept = self.db.query(Department).filter(Department.id == shift.department_id).first()

        primary_dept = None
        if user.department_id:
            if dept_by_id is not None:
                primary_dept = dept_by_id.get(user.department_id)
            else:
                primary_dept = self.db.query(Department).filter(Department.id == user.department_id).first()

        # Assigned roles
        role_assignments: list[UserRoleAssignmentResponse] = []
        if getattr(user, "assigned_roles", None):
            for ar in user.assigned_roles:
                role_name = ar.role.name if ar.role else (user.role.name if user.role and user.role.id == ar.role_id else "Staff")
                role_assignments.append(
                    UserRoleAssignmentResponse(
                        role_id=ar.role_id,
                        role_name=role_name,
                        is_primary=bool(ar.is_primary),
                        assigned_at=ar.assigned_at,
                    )
                )
        elif user.role_id and user.role:
            role_assignments.append(
                UserRoleAssignmentResponse(
                    role_id=user.role_id,
                    role_name=user.role.name,
                    is_primary=True,
                    assigned_at=user.created_at,
                )
            )

        # Assigned departments
        dept_assignments: list[UserDepartmentAssignmentResponse] = []
        if getattr(user, "assigned_departments", None):
            for ad in user.assigned_departments:
                d_obj = dept_by_id.get(ad.department_id) if dept_by_id else self.db.query(Department).filter(Department.id == ad.department_id).first()
                dept_assignments.append(
                    UserDepartmentAssignmentResponse(
                        department_id=ad.department_id,
                        department_name=d_obj.name if d_obj else "Department",
                        is_primary=bool(ad.is_primary),
                        can_supervise=bool(ad.can_supervise),
                    )
                )
        elif user.department_id:
            dept_assignments.append(
                UserDepartmentAssignmentResponse(
                    department_id=user.department_id,
                    department_name=primary_dept.name if primary_dept else "Department",
                    is_primary=True,
                    can_supervise=False,
                )
            )

        # Assigned locations
        loc_assignments: list[UserLocationAssignmentResponse] = []
        if getattr(user, "assigned_locations", None):
            for al in user.assigned_locations:
                loc_assignments.append(
                    UserLocationAssignmentResponse(
                        ward_id=al.ward_id,
                        wing_id=al.wing_id,
                    )
                )

        # Effective permissions: union of permissions across all assigned roles
        perms_map: dict[str, dict[str, Any]] = {}
        candidate_roles = []
        if getattr(user, "assigned_roles", None):
            candidate_roles = [ar.role for ar in user.assigned_roles if ar.role and (ar.expires_at is None or ar.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc))]
        if user.role and all(r.id != user.role_id for r in candidate_roles):
            candidate_roles.append(user.role)

        for r in candidate_roles:
            if not r.is_active or r.hospital_id != self.hospital_id:
                continue
            for p in getattr(r, "permissions", []):
                m = perms_map.setdefault(
                    p.module_key,
                    {
                        "module_key": p.module_key,
                        "can_view": False,
                        "can_edit": False,
                        "can_create": False,
                        "can_delete": False,
                        "can_approve": False,
                        "can_validate": False,
                        "can_release": False,
                        "can_dispense": False,
                        "can_refund": False,
                        "can_cancel": False,
                        "can_administer": False,
                    },
                )
                for act in [
                    "can_view", "can_edit", "can_create", "can_delete", "can_approve",
                    "can_validate", "can_release", "can_dispense", "can_refund", "can_cancel", "can_administer"
                ]:
                    if getattr(p, act, False):
                        m[act] = True

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
            shift_department_name=shift_dept.name if shift_dept else None,
            shift_start_time=fmt_shift_time(shift.start_time) if shift else None,
            shift_end_time=fmt_shift_time(shift.end_time) if shift else None,
            department_id=user.department_id,
            department_name=primary_dept.name if primary_dept else None,
            roles=role_assignments,
            departments=dept_assignments,
            locations=loc_assignments,
            effective_permissions=list(perms_map.values()),
        )

    def _user_query(self):
        return (
            self.db.query(HospitalUser)
            .options(
                joinedload(HospitalUser.role).selectinload(StaffRole.permissions),
                joinedload(HospitalUser.shift),
                selectinload(HospitalUser.assigned_roles).joinedload(HospitalUserRole.role).selectinload(StaffRole.permissions),
                selectinload(HospitalUser.assigned_departments),
                selectinload(HospitalUser.assigned_locations),
            )
            .filter(HospitalUser.hospital_id == self.hospital_id)
            .order_by(HospitalUser.name)
        )

    def _serialize_users(self, users):
        dept_ids = {u.shift.department_id for u in users if u.shift and u.shift.department_id}
        dept_ids.update({u.department_id for u in users if u.department_id})
        for u in users:
            if getattr(u, "assigned_departments", None):
                dept_ids.update({ad.department_id for ad in u.assigned_departments})
        dept_ids.discard(None)
        dept_by_id: dict[UUID, Department] = {}
        if dept_ids:
            for d in self.db.query(Department).filter(Department.id.in_(dept_ids), Department.hospital_id == self.hospital_id).all():
                dept_by_id[d.id] = d
        return [self._user_to_response(u, dept_by_id) for u in users]

    def list_users(self):
        return self._serialize_users(self._user_query().all())

    def list_users_page(self, page=1, page_size=25, search=None, is_active=None, role_id=None, department_id=None):
        q = self.db.query(HospitalUser).filter(HospitalUser.hospital_id == self.hospital_id)
        if search and search.strip():
            term = f"%{search.strip()}%"
            q = q.filter(or_(HospitalUser.name.ilike(term), HospitalUser.email.ilike(term), HospitalUser.phone.ilike(term)))
        if is_active is not None:
            q = q.filter(HospitalUser.is_active.is_(is_active))
        if role_id:
            q = q.filter(or_(HospitalUser.role_id == role_id, HospitalUser.assigned_roles.any(HospitalUserRole.role_id == role_id)))
        if department_id:
            q = q.filter(or_(HospitalUser.department_id == department_id, HospitalUser.assigned_departments.any(HospitalUserDepartment.department_id == department_id)))
        total = q.count()
        users = q.options(load_only(HospitalUser.id, HospitalUser.hospital_id, HospitalUser.role_id,
                          HospitalUser.department_id, HospitalUser.shift_id, HospitalUser.name,
                          HospitalUser.phone, HospitalUser.email, HospitalUser.is_active, HospitalUser.created_at),
                          joinedload(HospitalUser.role), joinedload(HospitalUser.shift),
                          selectinload(HospitalUser.assigned_roles).joinedload(HospitalUserRole.role),
                          selectinload(HospitalUser.assigned_departments)).order_by(HospitalUser.name, HospitalUser.id).offset((page-1)*page_size).limit(page_size).all()
        dept_ids = {u.department_id for u in users if u.department_id}
        dept_ids.update(d.department_id for u in users for d in u.assigned_departments)
        names = dict(self.db.query(Department.id, Department.name).filter(Department.hospital_id == self.hospital_id, Department.id.in_(dept_ids), Department.hospital_id == self.hospital_id).all()) if dept_ids else {}
        items = [self._user_list_item(user, names) for user in users]
        return HospitalUserPage(
            items=items, total=total, page=page, page_size=page_size,
            total_pages=(total + page_size - 1) // page_size,
        )

    def _user_list_item(self, user: HospitalUser, department_names: dict) -> HospitalUserListItem:
        return HospitalUserListItem(
            id=user.id, hospital_id=user.hospital_id, role_id=user.role_id,
            name=user.name, phone=user.phone, email=user.email,
            role_name=user.role.name if user.role else None,
            department_id=user.department_id,
            department_name=department_names.get(user.department_id),
            shift_id=user.shift_id, shift_name=user.shift.name if user.shift else None,
            is_active=user.is_active, created_at=user.created_at,
            roles=[UserRoleAssignmentResponse(
                role_id=assignment.role_id,
                role_name=assignment.role.name if assignment.role else "Staff",
                is_primary=assignment.is_primary, assigned_at=assignment.assigned_at,
            ) for assignment in user.assigned_roles],
            departments=[UserDepartmentAssignmentResponse(
                department_id=assignment.department_id,
                department_name=department_names.get(assignment.department_id, "Department"),
                is_primary=assignment.is_primary, can_supervise=assignment.can_supervise,
            ) for assignment in user.assigned_departments],
        )

    def _is_hospital_admin(self, user: HospitalUser) -> bool:
        if user.role and (getattr(user.role, "is_admin_role", False) or getattr(user.role, "code", None) == "HOSPITAL_ADMIN"):
            return True
        for ur in getattr(user, "assigned_roles", []):
            if ur.role and (getattr(ur.role, "is_admin_role", False) or getattr(ur.role, "code", None) == "HOSPITAL_ADMIN") and ur.role.is_active:
                return True
        return False

    def _ensure_active_admin_remains(self, target_user_id: UUID) -> None:
        admin_roles = self.db.query(StaffRole.id).filter(
            StaffRole.hospital_id == self.hospital_id,
            StaffRole.is_active.is_(True),
            or_(StaffRole.is_admin_role.is_(True), StaffRole.code == "HOSPITAL_ADMIN"),
        ).all()
        admin_role_ids = {r[0] for r in admin_roles}
        if not admin_role_ids:
            return

        other_admins = (
            self.db.query(HospitalUser.id)
            .filter(
                HospitalUser.hospital_id == self.hospital_id,
                HospitalUser.id != target_user_id,
                HospitalUser.is_active.is_(True),
                or_(
                    HospitalUser.role_id.in_(admin_role_ids),
                    HospitalUser.assigned_roles.any(
                        HospitalUserRole.role_id.in_(admin_role_ids)
                    ),
                ),
            )
            .count()
        )
        if other_admins == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="At least one active Hospital Administrator must remain.",
            )

    def _validate_assignments(self, payload, existing_user: HospitalUser | None = None):
        existing_roles = {r.role_id for r in existing_user.assigned_roles} | ({existing_user.role_id} if existing_user and existing_user.role_id else set()) if existing_user else set()
        existing_depts = {d.department_id for d in existing_user.assigned_departments} | ({existing_user.department_id} if existing_user and existing_user.department_id else set()) if existing_user else set()
        existing_wards = {l.ward_id for l in existing_user.assigned_locations if l.ward_id} if existing_user else set()

        for model, ids, label, existing_set in (
            (StaffRole, [*(payload.role_ids or []), *([payload.role_id] if getattr(payload, "role_id", None) else [])], "role", existing_roles),
            (Department, [*(payload.department_ids or []), *([payload.department_id] if getattr(payload, "department_id", None) else [])], "department", existing_depts),
            (Ward, getattr(payload, "ward_ids", None) or [], "ward", existing_wards),
        ):
            requested = set(ids or ())
            if requested:
                rows = self.db.query(model.id, model.is_active).filter(model.hospital_id == self.hospital_id, model.id.in_(requested)).all()
                found = {r[0] for r in rows}
                if requested != found:
                    raise HTTPException(status_code=400, detail=f"Selected {label} does not belong to this hospital.")
                new_ids = requested - existing_set
                inactive_new = [r[0] for r in rows if r[0] in new_ids and not r[1]]
                if inactive_new:
                    raise HTTPException(status_code=400, detail=f"Cannot assign inactive {label}.")

        if getattr(payload, "shift_id", None):
            existing_shift_id = existing_user.shift_id if existing_user else None
            if payload.shift_id != existing_shift_id:
                shift_row = self.db.query(ShiftType.id, ShiftType.is_active).filter(
                    ShiftType.hospital_id == self.hospital_id,
                    ShiftType.id == payload.shift_id,
                ).first()
                if not shift_row:
                    raise HTTPException(status_code=404, detail="Shift not found")
                if not shift_row[1]:
                    raise HTTPException(status_code=400, detail="Cannot assign inactive shift.")

    def get_user(self, user_id):
        users = self._user_query().populate_existing().filter(HospitalUser.id == user_id).all()
        if not users:
            raise HTTPException(status_code=404, detail="User not found")
        return self._serialize_users(users)[0]

    def create_user(self, payload: HospitalUserCreate) -> HospitalUserResponse:
        self._validate_assignments(payload)
        role = self._get_role(payload.role_id)
        email = payload.email.strip().lower()
        exists = (
            self.db.query(HospitalUser)
            .filter(HospitalUser.hospital_id == self.hospital_id, HospitalUser.email == email)
            .first()
        )
        if exists:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists.")

        # Multi-role custom field validation
        effective_rids = set(payload.role_ids or [])
        if payload.role_id:
            effective_rids.add(payload.role_id)
        if effective_rids:
            required_custom_fields = (
                self.db.query(RoleCustomField)
                .filter(
                    RoleCustomField.hospital_id == self.hospital_id,
                    RoleCustomField.role_id.in_(effective_rids),
                    RoleCustomField.is_required.is_(True),
                )
                .all()
            )
            cv = payload.custom_values or {}
            for req_field in required_custom_fields:
                val = cv.get(req_field.field_key)
                if val is None or (isinstance(val, str) and not str(val).strip()):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Required custom field '{req_field.label}' ({req_field.field_key}) is missing.",
                    )

        all_dept_ids = set(payload.department_ids or [])
        if payload.department_id:
            all_dept_ids.add(payload.department_id)

        shift_id = None
        shift_label = None
        if payload.shift_id or payload.department_id:
            shift = self._validate_department_shift(
                department_id=payload.department_id,
                shift_id=payload.shift_id,
                allowed_department_ids=all_dept_ids,
            )
            if shift:
                shift_id = shift.id
                shift_label = shift.name

        user = HospitalUser(
            hospital_id=self.hospital_id,
            role_id=payload.role_id,
            department_id=payload.department_id,
            shift_id=shift_id,
            name=payload.name.strip(),
            phone=payload.phone.strip(),
            email=email,
            password_hash=hash_password(payload.password),
            specialization=clean_optional_str(payload.specialization),
            medical_registration_number=clean_optional_str(payload.medical_registration_number),
            qualification=clean_optional_str(payload.qualification),
            years_of_experience=payload.years_of_experience,
            consultation_room=clean_optional_str(payload.consultation_room),
            show_financial_details=bool(payload.show_financial_details),
            custom_values=payload.custom_values or {},
            is_active=payload.is_active,
        )
        self.db.add(user)
        self.db.flush()

        # Multi-role bindings
        all_role_ids = set(payload.role_ids or [])
        all_role_ids.add(payload.role_id)
        for rid in all_role_ids:
            self.db.add(
                HospitalUserRole(
                    hospital_id=self.hospital_id,
                    user_id=user.id,
                    role_id=rid,
                    is_primary=(rid == payload.role_id),
                )
            )

        # Department bindings
        for did in all_dept_ids:
            self.db.add(
                HospitalUserDepartment(
                    hospital_id=self.hospital_id,
                    user_id=user.id,
                    department_id=did,
                    is_primary=(did == payload.department_id),
                )
            )

        # Location / ward bindings
        for wid in set(payload.ward_ids or []):
            self.db.add(
                HospitalUserLocation(
                    hospital_id=self.hospital_id,
                    user_id=user.id,
                    ward_id=wid,
                )
            )

        # Practitioner binding
        if payload.specialization or payload.medical_registration_number:
            self.db.add(
                Practitioner(
                    hospital_id=self.hospital_id,
                    user_id=user.id,
                    designation=payload.specialization or "Physician",
                    medical_registration_number=clean_optional_str(payload.medical_registration_number),
                    qualification=clean_optional_str(payload.qualification),
                    years_of_experience=payload.years_of_experience,
                    is_active=payload.is_active,
                )
            )

        stage_sync_event(self.db, self.hospital_id, "hospital_user", "create", user.id)
        self._audit(
            "create",
            "user",
            user.id,
            f"Created user '{user.name}' with role '{role.name}'",
            details={"email": user.email, "role": role.name, "shift": shift_label},
        )
        response_user_id = user.id
        self.db.commit()
        return self.get_user(response_user_id)

    def update_user(self, user_id: UUID, payload: HospitalUserUpdate) -> HospitalUserResponse:
        user = (
            self.db.query(HospitalUser)
            .options(
                joinedload(HospitalUser.role).selectinload(StaffRole.permissions),
                joinedload(HospitalUser.shift),
                selectinload(HospitalUser.assigned_roles).joinedload(HospitalUserRole.role).selectinload(StaffRole.permissions),
                selectinload(HospitalUser.assigned_departments),
                selectinload(HospitalUser.assigned_locations),
            )
            .filter(HospitalUser.id == user_id, HospitalUser.hospital_id == self.hospital_id)
            .first()
        )
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        self._validate_assignments(payload, existing_user=user)
        if "role_id" in payload.model_fields_set and payload.role_id is None:
            raise HTTPException(status_code=400, detail="Primary role cannot be cleared.")
        original_role_id = user.role_id
        original_is_active = user.is_active

        data = payload.model_dump(exclude_unset=True, exclude={"password", "role_ids", "department_ids", "ward_ids"})
        raw = payload.model_dump(exclude_unset=True)
        if "role_id" in data and data["role_id"]:
            self._get_role(data["role_id"])

        primary_role_id = payload.role_id or user.role_id
        all_rids = set(payload.role_ids or []) if "role_ids" in raw else {r.role_id for r in user.assigned_roles if not r.is_primary}
        all_rids.add(primary_role_id)

        # Multi-role custom field validation
        effective_rids = all_rids
        if effective_rids:
            required_custom_fields = (
                self.db.query(RoleCustomField)
                .filter(
                    RoleCustomField.hospital_id == self.hospital_id,
                    RoleCustomField.role_id.in_(effective_rids),
                    RoleCustomField.is_required.is_(True),
                )
                .all()
            )
            eff_cv = dict(user.custom_values or {})
            if payload.custom_values:
                eff_cv.update(payload.custom_values)
            for req_field in required_custom_fields:
                val = eff_cv.get(req_field.field_key)
                if val is None or (isinstance(val, str) and not str(val).strip()):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Required custom field '{req_field.label}' ({req_field.field_key}) is missing.",
                    )

        # Protect Last Active Hospital Administrator
        if self._is_hospital_admin(user):
            will_be_active = payload.is_active if payload.is_active is not None else user.is_active
            admin_roles = self.db.query(StaffRole.id).filter(
                StaffRole.hospital_id == self.hospital_id,
                StaffRole.is_active.is_(True),
                or_(StaffRole.is_admin_role.is_(True), StaffRole.code == "HOSPITAL_ADMIN"),
            ).all()
            admin_role_ids = {r[0] for r in admin_roles}
            will_have_admin = (primary_role_id in admin_role_ids) or any(rid in admin_role_ids for rid in all_rids)
            if not will_be_active or not will_have_admin:
                self._ensure_active_admin_remains(user.id)

        # Compute effective assigned departments before shift validation
        raw_dept_ids = set(payload.department_ids or []) if "department_ids" in raw else {d.department_id for d in user.assigned_departments}
        raw_primary_dept = raw.get("department_id", user.department_id)
        if raw_primary_dept:
            raw_dept_ids.add(raw_primary_dept)

        if "shift_id" in data or "department_id" in raw or "department_ids" in raw:
            effective_shift_id = data["shift_id"] if "shift_id" in data else user.shift_id
            if "shift_id" in data and not data["shift_id"]:
                effective_shift_id = None
                data["shift_id"] = None
            same_shift = effective_shift_id is not None and user.shift_id == effective_shift_id
            if effective_shift_id is not None and user.shift and user.shift.id == effective_shift_id:
                if user.shift.department_id and user.shift.department_id not in raw_dept_ids:
                    effective_shift_id = None
                    data["shift_id"] = None
            if effective_shift_id is not None or "department_id" in raw or "department_ids" in raw:
                self._validate_department_shift(
                    department_id=raw_primary_dept,
                    shift_id=effective_shift_id,
                    allowed_department_ids=raw_dept_ids,
                    require_active_shift=not same_shift,
                )

        if "email" in data and data["email"]:
            data["email"] = str(data["email"]).strip().lower()
            clash = (
                self.db.query(HospitalUser)
                .filter(
                    HospitalUser.hospital_id == self.hospital_id,
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

        # Merge custom values to preserve secondary role custom fields
        if "custom_values" in data and data["custom_values"] is not None:
            merged_cv = dict(user.custom_values or {})
            merged_cv.update(data["custom_values"])
            data["custom_values"] = merged_cv

        for k, v in data.items():
            setattr(user, k, v)
        if payload.password:
            user.password_hash = hash_password(payload.password)

        # Multi-role updates
        if "role_ids" in raw or "role_id" in raw:
            existing_roles = {assignment.role_id: assignment for assignment in user.assigned_roles}
            for rid, assignment in existing_roles.items():
                if rid not in all_rids:
                    self.db.delete(assignment)
                else:
                    assignment.is_primary = rid == primary_role_id
            for rid in all_rids - existing_roles.keys():
                self.db.add(HospitalUserRole(hospital_id=self.hospital_id, user_id=user.id,
                                            role_id=rid, is_primary=rid == primary_role_id))

        # Department updates
        if "department_ids" in raw or "department_id" in raw:
            all_dids = raw_dept_ids
            if raw_primary_dept:
                user.department_id = raw_primary_dept
            existing_departments = {assignment.department_id: assignment for assignment in user.assigned_departments}
            for did, assignment in existing_departments.items():
                if did not in all_dids:
                    self.db.delete(assignment)
                else:
                    assignment.is_primary = did == raw_primary_dept
            for did in all_dids - existing_departments.keys():
                self.db.add(HospitalUserDepartment(hospital_id=self.hospital_id, user_id=user.id,
                                                  department_id=did, is_primary=did == raw_primary_dept))

        # Location updates
        if "ward_ids" in raw:
            self.db.query(HospitalUserLocation).filter(HospitalUserLocation.user_id == user.id).delete()
            for wid in set(payload.ward_ids or []):
                self.db.add(
                    HospitalUserLocation(
                        hospital_id=self.hospital_id,
                        user_id=user.id,
                        ward_id=wid,
                    )
                )

        # Practitioner updates
        if payload.specialization or payload.medical_registration_number or user.is_active != original_is_active:
            practitioner = self.db.query(Practitioner).filter(Practitioner.user_id == user.id).first()
            if not practitioner:
                if payload.specialization or payload.medical_registration_number or user.specialization or user.medical_registration_number:
                    practitioner = Practitioner(
                        hospital_id=self.hospital_id,
                        user_id=user.id,
                        designation=payload.specialization or user.specialization or "Physician",
                        medical_registration_number=clean_optional_str(payload.medical_registration_number) or user.medical_registration_number,
                        qualification=clean_optional_str(payload.qualification) or user.qualification,
                        years_of_experience=payload.years_of_experience or user.years_of_experience,
                        is_active=user.is_active,
                    )
                    self.db.add(practitioner)
            else:
                if payload.specialization:
                    practitioner.designation = payload.specialization
                if payload.medical_registration_number:
                    practitioner.medical_registration_number = clean_optional_str(payload.medical_registration_number)
                if payload.qualification:
                    practitioner.qualification = clean_optional_str(payload.qualification)
                if payload.years_of_experience is not None:
                    practitioner.years_of_experience = payload.years_of_experience
                practitioner.is_active = user.is_active

        auth_relevant_change = (
            ("role_id" in data and data["role_id"] != original_role_id)
            or ("is_active" in data and data["is_active"] != original_is_active)
            or "role_ids" in raw or "department_id" in raw or "department_ids" in raw or "ward_ids" in raw or bool(payload.password)
        )
        if auth_relevant_change:
            user.token_version = (user.token_version or 1) + 1

        stage_sync_event(self.db, self.hospital_id, "hospital_user", "update", user.id)
        self._audit(
            "update",
            "user",
            user.id,
            f"Updated user '{user.name}'",
            details={"email": user.email, "password_changed": bool(payload.password), "is_active": user.is_active},
        )
        response_user_id = user.id
        self.db.commit()
        return self.get_user(response_user_id)

    def delete_user(self, user_id: UUID) -> None:
        user = self.db.query(HospitalUser).filter(HospitalUser.id == user_id, HospitalUser.hospital_id == self.hospital_id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if self._is_hospital_admin(user):
            self._ensure_active_admin_remains(user.id)
        assert_unreferenced(
            self.db,
            user,
            ignore_tables=("hospital_user_roles", "hospital_user_departments", "hospital_user_locations", "practitioners"),
        )
        for r in list(getattr(user, "assigned_roles", [])):
            self.db.delete(r)
        for d in list(getattr(user, "assigned_departments", [])):
            self.db.delete(d)
        for loc in list(getattr(user, "assigned_locations", [])):
            self.db.delete(loc)
        practitioner = self.db.query(Practitioner).filter(Practitioner.user_id == user.id).first()
        if practitioner:
            self.db.delete(practitioner)
        name = user.name
        email = user.email
        self.db.delete(user)
        stage_sync_event(self.db, self.hospital_id, "hospital_user", "delete", user_id)
        self._audit(
            "delete",
            "user",
            user_id,
            f"Deleted user '{name}'",
            details={"email": email},
        )
        self.db.commit()

    # ── Audit Logs ─────────────────────────────────────────────────────────────
    def list_audit_logs(
        self,
        search: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        limit: int = 100,
    ) -> list[AuditLogResponse]:
        query = self.db.query(AuditLog).filter(AuditLog.hospital_id == self.hospital_id)
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

    # ── Daily Shift Roster ─────────────────────────────────────────────────────
    def _holiday_name_for(self, roster_date: date) -> str | None:
        holiday = get_effective_holiday(self.db, self.hospital_id, roster_date)
        return holiday.name if holiday else None

    def _build_roster_entries(self, roster_date: date, user_id: UUID | None = None) -> list[ShiftRosterEntry]:
        import uuid as _uuid
        user_query = (
            self.db.query(HospitalUser)
            .options(
                joinedload(HospitalUser.role),
                joinedload(HospitalUser.shift),
                selectinload(HospitalUser.assigned_departments),
            )
            .filter(HospitalUser.hospital_id == self.hospital_id, HospitalUser.is_active.is_(True))
        )
        if user_id:
            user_query = user_query.filter(HospitalUser.id == user_id)
        users = user_query.order_by(HospitalUser.name).all()
        if not users:
            return []

        user_ids = [u.id for u in users]

        # 1. Date-specific roster days and segments
        roster_days_query = (
            self.db.query(StaffRosterDay)
            .options(selectinload(StaffRosterDay.segments).joinedload(StaffRosterSegment.shift_type))
            .filter(
                StaffRosterDay.hospital_id == self.hospital_id,
                StaffRosterDay.roster_date == roster_date,
                StaffRosterDay.user_id.in_(user_ids),
            )
        )
        roster_days = {rd.user_id: rd for rd in roster_days_query.all()}

        # 2. Legacy StaffDailyShift overrides
        legacy_query = (
            self.db.query(StaffDailyShift)
            .options(joinedload(StaffDailyShift.shift))
            .filter(
                StaffDailyShift.hospital_id == self.hospital_id,
                StaffDailyShift.roster_date == roster_date,
                StaffDailyShift.user_id.in_(user_ids),
            )
        )
        legacy_overrides = {row.user_id: row for row in legacy_query.all()}

        # 3. Recurring work patterns
        work_patterns_query = (
            self.db.query(StaffWorkPattern)
            .options(selectinload(StaffWorkPattern.segments).joinedload(StaffWorkPatternSegment.shift_type))
            .filter(
                StaffWorkPattern.hospital_id == self.hospital_id,
                StaffWorkPattern.user_id.in_(user_ids),
                StaffWorkPattern.is_active.is_(True),
            )
        )
        work_patterns = {wp.user_id: wp for wp in work_patterns_query.all()}

        # Collect all referenced shift IDs and department IDs
        shift_ids: set[UUID] = set()
        dept_ids: set[UUID] = set()

        for u in users:
            if u.shift_id:
                shift_ids.add(u.shift_id)
            if u.department_id:
                dept_ids.add(u.department_id)
            for ad in getattr(u, "assigned_departments", []):
                if ad.department_id:
                    dept_ids.add(ad.department_id)

        for rd in roster_days.values():
            for seg in rd.segments:
                if seg.shift_type_id:
                    shift_ids.add(seg.shift_type_id)
                if seg.department_id:
                    dept_ids.add(seg.department_id)

        for wp in work_patterns.values():
            for wps in wp.segments:
                if wps.shift_type_id:
                    shift_ids.add(wps.shift_type_id)
                if wps.department_id:
                    dept_ids.add(wps.department_id)

        for leg in legacy_overrides.values():
            if leg.shift_id:
                shift_ids.add(leg.shift_id)

        shifts_by_id: dict[UUID, ShiftType] = {}
        if shift_ids:
            for s in self.db.query(ShiftType).filter(ShiftType.id.in_(shift_ids)).all():
                shifts_by_id[s.id] = s
                if s.department_id:
                    dept_ids.add(s.department_id)

        depts_by_id: dict[UUID, Department] = {}
        if dept_ids:
            for d in self.db.query(Department).filter(
                Department.id.in_(dept_ids), Department.hospital_id == self.hospital_id
            ).all():
                depts_by_id[d.id] = d

        weekday = roster_date.weekday()
        entries: list[ShiftRosterEntry] = []

        for user in users:
            eligible_dept_ids = []
            if user.department_id:
                eligible_dept_ids.append(user.department_id)
            for ad in getattr(user, "assigned_departments", []):
                if ad.department_id not in eligible_dept_ids:
                    eligible_dept_ids.append(ad.department_id)

            primary_dept = depts_by_id.get(user.department_id) if user.department_id else None
            primary_dept_name = primary_dept.name if primary_dept else None

            segments_items: list[StaffRosterSegmentItem] = []
            status_val = "off"
            is_override = False
            notes = None
            primary_shift: ShiftType | None = None
            primary_dept_id = user.department_id

            if user.id in roster_days:
                rd = roster_days[user.id]
                status_val = "on_duty" if rd.status in ("working", "on_duty") else rd.status
                is_override = True
                notes = rd.notes
                if status_val == "on_duty":
                    for seg in sorted(rd.segments, key=lambda s: s.start_at):
                        shift_obj = shifts_by_id.get(seg.shift_type_id) if seg.shift_type_id else None
                        d_name = depts_by_id[seg.department_id].name if seg.department_id and seg.department_id in depts_by_id else None
                        s_name = shift_obj.name if shift_obj else None
                        is_cross = seg.start_at.date() != seg.end_at.date()
                        segments_items.append(
                            StaffRosterSegmentItem(
                                id=seg.id,
                                roster_day_id=rd.id,
                                shift_type_id=seg.shift_type_id,
                                shift_name=s_name,
                                start_at=seg.start_at,
                                end_at=seg.end_at,
                                start_time=fmt_time(seg.start_at),
                                end_time=fmt_time(seg.end_at),
                                duty_kind=seg.duty_kind,
                                presence_type=seg.presence_type,
                                is_bookable=seg.is_bookable,
                                department_id=seg.department_id,
                                department_name=d_name,
                                notes=seg.notes,
                                is_cross_midnight=is_cross,
                            )
                        )
                    if not segments_items:
                        status_val = rd.status if rd.status != "working" else "off"
                    else:
                        primary_shift = shifts_by_id.get(segments_items[0].shift_type_id) if segments_items[0].shift_type_id else None
                        primary_dept_id = segments_items[0].department_id or user.department_id
                else:
                    segments_items = []

            elif user.id in legacy_overrides:
                leg = legacy_overrides[user.id]
                status_val = leg.status or "on_duty"
                is_override = True
                notes = leg.notes
                if status_val == "on_duty" and leg.shift_id:
                    shift_obj = shifts_by_id.get(leg.shift_id)
                    if shift_obj:
                        primary_shift = shift_obj
                        st, et = compute_segment_interval(roster_date, str(shift_obj.start_time), str(shift_obj.end_time))
                        d_name = depts_by_id[shift_obj.department_id].name if shift_obj.department_id and shift_obj.department_id in depts_by_id else None
                        primary_dept_id = shift_obj.department_id or user.department_id
                        segments_items.append(
                            StaffRosterSegmentItem(
                                id=_uuid.uuid4(),
                                roster_day_id=None,
                                shift_type_id=shift_obj.id,
                                shift_name=shift_obj.name,
                                start_at=st,
                                end_at=et,
                                start_time=fmt_time(st),
                                end_time=fmt_time(et),
                                duty_kind="OPD",
                                presence_type="ONSITE",
                                is_bookable=True,
                                department_id=shift_obj.department_id,
                                department_name=d_name,
                                notes=None,
                                is_cross_midnight=st.date() != et.date(),
                            )
                        )
                elif status_val in ("off", "leave"):
                    segments_items = []

            elif user.id in work_patterns:
                wp = work_patterns[user.id]
                day_segs = [s for s in wp.segments if s.day_of_week == weekday]
                if day_segs:
                    status_val = "on_duty"
                    is_override = False
                    for p_seg in sorted(day_segs, key=lambda s: s.start_time):
                        shift_obj = shifts_by_id.get(p_seg.shift_type_id) if p_seg.shift_type_id else None
                        st, et = compute_segment_interval(roster_date, str(p_seg.start_time), str(p_seg.end_time))
                        d_name = depts_by_id[p_seg.department_id].name if p_seg.department_id and p_seg.department_id in depts_by_id else None
                        segments_items.append(
                            StaffRosterSegmentItem(
                                id=_uuid.uuid4(),
                                roster_day_id=None,
                                shift_type_id=p_seg.shift_type_id,
                                shift_name=shift_obj.name if shift_obj else None,
                                start_at=st,
                                end_at=et,
                                start_time=fmt_time(st),
                                end_time=fmt_time(et),
                                duty_kind=p_seg.duty_kind,
                                presence_type=p_seg.presence_type,
                                is_bookable=p_seg.is_bookable,
                                department_id=p_seg.department_id,
                                department_name=d_name,
                                notes=None,
                                is_cross_midnight=st.date() != et.date(),
                            )
                        )
                    if segments_items:
                        primary_shift = shifts_by_id.get(segments_items[0].shift_type_id) if segments_items[0].shift_type_id else None
                        primary_dept_id = segments_items[0].department_id or user.department_id
                else:
                    status_val = "off"
                    is_override = False
                    segments_items = []

            else:
                shift = shifts_by_id.get(user.shift_id) if user.shift_id else None
                if shift and shift.is_active:
                    status_val = "on_duty"
                    is_override = False
                    primary_shift = shift
                    st, et = compute_segment_interval(roster_date, str(shift.start_time), str(shift.end_time))
                    d_name = depts_by_id[shift.department_id].name if shift.department_id and shift.department_id in depts_by_id else None
                    primary_dept_id = shift.department_id or user.department_id
                    segments_items.append(
                        StaffRosterSegmentItem(
                            id=_uuid.uuid4(),
                            roster_day_id=None,
                            shift_type_id=shift.id,
                            shift_name=shift.name,
                            start_at=st,
                            end_at=et,
                            start_time=fmt_time(st),
                            end_time=fmt_time(et),
                            duty_kind="OPD",
                            presence_type="ONSITE",
                            is_bookable=True,
                            department_id=shift.department_id,
                            department_name=d_name,
                            notes=None,
                            is_cross_midnight=st.date() != et.date(),
                        )
                    )
                else:
                    status_val = "off"
                    is_override = False
                    segments_items = []

            eff_dept = depts_by_id.get(primary_dept_id) if primary_dept_id else None
            start_t = segments_items[0].start_time if segments_items else (fmt_time(primary_shift.start_time) if primary_shift else None)
            end_t = segments_items[0].end_time if segments_items else (fmt_time(primary_shift.end_time) if primary_shift else None)

            entries.append(
                ShiftRosterEntry(
                    user_id=user.id,
                    name=user.name,
                    phone=user.phone,
                    role_id=user.role_id,
                    role_name=user.role.name if user.role else None,
                    shift_id=primary_shift.id if primary_shift else None,
                    shift_name=primary_shift.name if primary_shift else None,
                    department_id=primary_dept_id,
                    department_name=eff_dept.name if eff_dept else None,
                    start_time=start_t,
                    end_time=end_t,
                    status=status_val,
                    is_override=is_override,
                    notes=notes,
                    default_shift_id=user.shift_id,
                    eligible_department_ids=eligible_dept_ids,
                    primary_department_id=user.department_id,
                    primary_department_name=primary_dept_name,
                    segments=segments_items,
                )
            )

        return entries

    def get_shift_roster(self, roster_date: date) -> ShiftRosterResponse:
        hospital = self.db.query(Hospital).filter(Hospital.id == self.hospital_id).first()
        entries = self._build_roster_entries(roster_date)
        unique_staff_working = sum(1 for e in entries if e.status == "on_duty" and (e.segments or e.shift_id))
        total_duty_segments = sum(len(e.segments) for e in entries)
        total_staff = len(entries)
        off_count = sum(1 for e in entries if e.status == "off")
        leave_count = sum(1 for e in entries if e.status == "leave")
        return ShiftRosterResponse(
            roster_date=roster_date,
            hospital_name=hospital.name if hospital else "Hospital",
            holiday_name=self._holiday_name_for(roster_date),
            entries=entries,
            unique_staff_working=unique_staff_working,
            total_duty_segments=total_duty_segments,
            total_staff=total_staff,
            off_count=off_count,
            leave_count=leave_count,
        )

    def assign_shift_roster(self, payload: ShiftRosterAssign) -> ShiftRosterEntry:
        user = (
            self.db.query(HospitalUser)
            .options(
                joinedload(HospitalUser.role),
                selectinload(HospitalUser.assigned_departments),
            )
            .filter(HospitalUser.id == payload.user_id, HospitalUser.hospital_id == self.hospital_id)
            .first()
        )
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff user not found")
        if not user.is_active:
            raise HTTPException(status_code=400, detail="Cannot assign a roster to inactive staff.")

        status_val = payload.status
        shift_id = payload.shift_id
        shift = None
        if status_val in ("off", "leave"):
            shift_id = None
        elif shift_id:
            shift = (
                self.db.query(ShiftType)
                .filter(ShiftType.id == shift_id, ShiftType.hospital_id == self.hospital_id)
                .first()
            )
            if not shift:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shift type not found")
            if not shift.is_active:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Shift type is inactive")
            
            allowed_dept_ids = {d.department_id for d in user.assigned_departments}
            if user.department_id:
                allowed_dept_ids.add(user.department_id)
            if allowed_dept_ids and shift.department_id and shift.department_id not in allowed_dept_ids:
                raise HTTPException(status_code=400, detail="Selected shift does not belong to the employee's department.")
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="shift_id is required when status is on_duty",
            )

        # 1. Update/create legacy StaffDailyShift
        row = (
            self.db.query(StaffDailyShift)
            .filter(
                StaffDailyShift.hospital_id == self.hospital_id,
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
                hospital_id=self.hospital_id,
                user_id=payload.user_id,
                roster_date=payload.roster_date,
                shift_id=shift_id,
                status=status_val,
                notes=payload.notes,
            )
            self.db.add(row)

        # 2. Update/create StaffRosterDay and StaffRosterSegment
        roster_day = (
            self.db.query(StaffRosterDay)
            .filter(
                StaffRosterDay.hospital_id == self.hospital_id,
                StaffRosterDay.user_id == payload.user_id,
                StaffRosterDay.roster_date == payload.roster_date,
            )
            .first()
        )
        if not roster_day:
            roster_day = StaffRosterDay(
                hospital_id=self.hospital_id,
                user_id=payload.user_id,
                roster_date=payload.roster_date,
                status="working" if status_val in ("on_duty", "working") else status_val,
                notes=payload.notes,
            )
            self.db.add(roster_day)
            self.db.flush()
        else:
            roster_day.status = "working" if status_val in ("on_duty", "working") else status_val
            roster_day.notes = payload.notes

        self.db.query(StaffRosterSegment).filter(
            StaffRosterSegment.roster_day_id == roster_day.id
        ).delete(synchronize_session=False)

        if status_val in ("on_duty", "working") and shift:
            start_at, end_at = compute_segment_interval(payload.roster_date, str(shift.start_time), str(shift.end_time))
            check_segment_overlap(
                self.db, self.hospital_id, payload.user_id, start_at, end_at, "ONSITE"
            )
            is_doctor = (
                (user.role and "doctor" in user.role.name.lower())
                or bool(user.specialization)
                or bool(user.medical_registration_number)
            )
            is_opd = is_doctor or "OPD" in (shift.name or "").upper()
            new_seg = StaffRosterSegment(
                hospital_id=self.hospital_id,
                roster_day_id=roster_day.id,
                user_id=payload.user_id,
                shift_type_id=shift.id,
                start_at=start_at,
                end_at=end_at,
                duty_kind="OPD" if is_opd else "OTHER",
                presence_type="ONSITE",
                is_bookable=is_opd,
                department_id=shift.department_id or user.department_id,
                notes=payload.notes,
            )
            self.db.add(new_seg)

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="assign",
            entity_id=payload.user_id,
            details={
                "roster_date": payload.roster_date.isoformat(),
                "user_id": str(payload.user_id),
                "status": status_val,
                "shift_id": str(shift_id) if shift_id else None,
            },
        )
        self.db.commit()

        entries = self._build_roster_entries(payload.roster_date, payload.user_id)
        for e in entries:
            if e.user_id == payload.user_id:
                return e
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load assignment")

    def add_duty_segment(self, payload: RosterDutySegmentCreate) -> StaffRosterSegmentItem:
        user = self.db.query(HospitalUser).filter(
            HospitalUser.id == payload.user_id,
            HospitalUser.hospital_id == self.hospital_id,
        ).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff user not found")
        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot assign duty to inactive staff.")

        shift_obj = None
        if payload.shift_type_id:
            shift_obj = self.db.query(ShiftType).filter(
                ShiftType.id == payload.shift_type_id,
                ShiftType.hospital_id == self.hospital_id,
            ).first()
            if not shift_obj:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shift type not found")
            if not shift_obj.is_active:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Shift type is inactive")

        start_at, end_at = compute_segment_interval(payload.roster_date, payload.start_time, payload.end_time)

        check_segment_overlap(
            self.db, self.hospital_id, payload.user_id, start_at, end_at, payload.presence_type
        )

        roster_day = (
            self.db.query(StaffRosterDay)
            .filter(
                StaffRosterDay.hospital_id == self.hospital_id,
                StaffRosterDay.user_id == payload.user_id,
                StaffRosterDay.roster_date == payload.roster_date,
            )
            .first()
        )
        if not roster_day:
            roster_day = StaffRosterDay(
                hospital_id=self.hospital_id,
                user_id=payload.user_id,
                roster_date=payload.roster_date,
                status="working",
            )
            self.db.add(roster_day)
            self.db.flush()
        else:
            roster_day.status = "working"

        dept_id = payload.department_id or (shift_obj.department_id if shift_obj else None) or user.department_id

        seg = StaffRosterSegment(
            hospital_id=self.hospital_id,
            roster_day_id=roster_day.id,
            user_id=payload.user_id,
            shift_type_id=payload.shift_type_id,
            start_at=start_at,
            end_at=end_at,
            duty_kind=payload.duty_kind,
            presence_type=payload.presence_type,
            is_bookable=payload.is_bookable,
            department_id=dept_id,
            notes=payload.notes,
        )
        self.db.add(seg)
        self.db.flush()

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="add_segment",
            entity_id=payload.user_id,
            details={
                "roster_date": payload.roster_date.isoformat(),
                "user_id": str(payload.user_id),
                "segment_id": str(seg.id),
            },
        )
        self.db.commit()

        dept = self.db.query(Department).filter(Department.id == dept_id).first() if dept_id else None
        return StaffRosterSegmentItem(
            id=seg.id,
            roster_day_id=roster_day.id,
            shift_type_id=seg.shift_type_id,
            shift_name=shift_obj.name if shift_obj else None,
            start_at=seg.start_at,
            end_at=seg.end_at,
            start_time=fmt_time(seg.start_at),
            end_time=fmt_time(seg.end_at),
            duty_kind=seg.duty_kind,
            presence_type=seg.presence_type,
            is_bookable=seg.is_bookable,
            department_id=dept_id,
            department_name=dept.name if dept else None,
            notes=seg.notes,
            is_cross_midnight=seg.start_at.date() != seg.end_at.date(),
        )

    def update_duty_segment(self, segment_id: UUID, payload: RosterDutySegmentUpdate) -> StaffRosterSegmentItem:
        seg = (
            self.db.query(StaffRosterSegment)
            .filter(StaffRosterSegment.id == segment_id, StaffRosterSegment.hospital_id == self.hospital_id)
            .first()
        )
        if not seg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Duty segment not found")

        roster_day = self.db.query(StaffRosterDay).filter(StaffRosterDay.id == seg.roster_day_id).first()
        base_date = roster_day.roster_date if roster_day else seg.start_at.date()

        st_str = payload.start_time or fmt_time(seg.start_at)
        et_str = payload.end_time or fmt_time(seg.end_at)
        start_at, end_at = compute_segment_interval(base_date, st_str, et_str)

        presence = payload.presence_type or seg.presence_type
        check_segment_overlap(
            self.db, self.hospital_id, seg.user_id, start_at, end_at, presence, exclude_segment_id=seg.id
        )

        shift_obj = None
        if payload.shift_type_id is not None:
            if payload.shift_type_id:
                shift_obj = self.db.query(ShiftType).filter(
                    ShiftType.id == payload.shift_type_id, ShiftType.hospital_id == self.hospital_id
                ).first()
                if not shift_obj:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shift type not found")
            seg.shift_type_id = payload.shift_type_id
        elif seg.shift_type_id:
            shift_obj = self.db.query(ShiftType).filter(ShiftType.id == seg.shift_type_id).first()

        seg.start_at = start_at
        seg.end_at = end_at
        if payload.duty_kind is not None:
            seg.duty_kind = payload.duty_kind
        if payload.presence_type is not None:
            seg.presence_type = payload.presence_type
        if payload.is_bookable is not None:
            seg.is_bookable = payload.is_bookable
        if payload.department_id is not None:
            seg.department_id = payload.department_id
        if payload.notes is not None:
            seg.notes = payload.notes

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="update_segment",
            entity_id=seg.user_id,
            details={
                "segment_id": str(seg.id),
            },
        )
        self.db.commit()

        dept = self.db.query(Department).filter(Department.id == seg.department_id).first() if seg.department_id else None
        return StaffRosterSegmentItem(
            id=seg.id,
            roster_day_id=seg.roster_day_id,
            shift_type_id=seg.shift_type_id,
            shift_name=shift_obj.name if shift_obj else None,
            start_at=seg.start_at,
            end_at=seg.end_at,
            start_time=fmt_time(seg.start_at),
            end_time=fmt_time(seg.end_at),
            duty_kind=seg.duty_kind,
            presence_type=seg.presence_type,
            is_bookable=seg.is_bookable,
            department_id=seg.department_id,
            department_name=dept.name if dept else None,
            notes=seg.notes,
            is_cross_midnight=seg.start_at.date() != seg.end_at.date(),
        )

    def delete_duty_segment(self, segment_id: UUID) -> None:
        seg = (
            self.db.query(StaffRosterSegment)
            .filter(StaffRosterSegment.id == segment_id, StaffRosterSegment.hospital_id == self.hospital_id)
            .first()
        )
        if not seg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Duty segment not found")

        roster_day_id = seg.roster_day_id
        user_id = seg.user_id
        self.db.delete(seg)
        self.db.flush()

        remaining_count = self.db.query(StaffRosterSegment).filter(
            StaffRosterSegment.roster_day_id == roster_day_id
        ).count()
        if remaining_count == 0:
            roster_day = self.db.query(StaffRosterDay).filter(StaffRosterDay.id == roster_day_id).first()
            if roster_day:
                roster_day.status = "off"

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="delete_segment",
            entity_id=user_id,
            details={
                "segment_id": str(segment_id),
                "roster_day_id": str(roster_day_id),
            },
        )
        self.db.commit()

    def update_roster_day(self, payload: StaffRosterDayUpdate) -> ShiftRosterEntry:
        user = self.db.query(HospitalUser).filter(
            HospitalUser.id == payload.user_id, HospitalUser.hospital_id == self.hospital_id
        ).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff user not found")

        roster_day = (
            self.db.query(StaffRosterDay)
            .filter(
                StaffRosterDay.hospital_id == self.hospital_id,
                StaffRosterDay.user_id == payload.user_id,
                StaffRosterDay.roster_date == payload.roster_date,
            )
            .first()
        )
        if not roster_day:
            roster_day = StaffRosterDay(
                hospital_id=self.hospital_id,
                user_id=payload.user_id,
                roster_date=payload.roster_date,
                status=payload.status,
                notes=payload.notes,
            )
            self.db.add(roster_day)
            self.db.flush()
        else:
            roster_day.status = payload.status
            roster_day.notes = payload.notes

        if payload.status in ("off", "leave"):
            self.db.query(StaffRosterSegment).filter(
                StaffRosterSegment.roster_day_id == roster_day.id
            ).delete(synchronize_session=False)
        elif payload.segments is not None:
            self.db.query(StaffRosterSegment).filter(
                StaffRosterSegment.roster_day_id == roster_day.id
            ).delete(synchronize_session=False)

            intervals: list[tuple[datetime, datetime]] = []
            for item in payload.segments:
                st, et = compute_segment_interval(payload.roster_date, item.start_time, item.end_time)
                if item.presence_type == "ONSITE":
                    for prev_st, prev_et in intervals:
                        if st < prev_et and et > prev_st:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Segments on {payload.roster_date} overlap with each other ({item.start_time} - {item.end_time}).",
                            )
                    intervals.append((st, et))

                check_segment_overlap(
                    self.db, self.hospital_id, payload.user_id, st, et, item.presence_type
                )

                shift_obj = self.db.query(ShiftType).filter(
                    ShiftType.id == item.shift_type_id, ShiftType.hospital_id == self.hospital_id
                ).first() if item.shift_type_id else None

                dept_id = item.department_id or (shift_obj.department_id if shift_obj else None) or user.department_id
                new_seg = StaffRosterSegment(
                    hospital_id=self.hospital_id,
                    roster_day_id=roster_day.id,
                    user_id=payload.user_id,
                    shift_type_id=item.shift_type_id,
                    start_at=st,
                    end_at=et,
                    duty_kind=item.duty_kind,
                    presence_type=item.presence_type,
                    is_bookable=item.is_bookable,
                    department_id=dept_id,
                    notes=item.notes,
                )
                self.db.add(new_seg)

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="update_day",
            entity_id=payload.user_id,
            details={
                "roster_date": payload.roster_date.isoformat(),
                "status": payload.status,
            },
        )
        self.db.commit()

        entries = self._build_roster_entries(payload.roster_date, payload.user_id)
        for e in entries:
            if e.user_id == payload.user_id:
                return e
        raise HTTPException(status_code=500, detail="Failed to load updated roster day")

    def seed_shift_roster(self, payload: ShiftRosterSeed) -> ShiftRosterResponse:
        users = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.shift))
            .filter(HospitalUser.hospital_id == self.hospital_id, HospitalUser.is_active.is_(True))
            .all()
        )
        existing_days = {
            row.user_id: row
            for row in self.db.query(StaffRosterDay)
            .filter(StaffRosterDay.hospital_id == self.hospital_id, StaffRosterDay.roster_date == payload.roster_date)
            .all()
        }
        user_ids = [u.id for u in users]
        work_patterns = {
            wp.user_id: wp
            for wp in self.db.query(StaffWorkPattern)
            .options(selectinload(StaffWorkPattern.segments))
            .filter(
                StaffWorkPattern.hospital_id == self.hospital_id,
                StaffWorkPattern.user_id.in_(user_ids),
                StaffWorkPattern.is_active.is_(True),
            )
            .all()
        }

        weekday = payload.roster_date.weekday()

        for user in users:
            if user.id in existing_days and not payload.overwrite:
                continue

            pattern = work_patterns.get(user.id)
            pattern_segs = [s for s in pattern.segments if s.day_of_week == weekday] if pattern else []

            is_working = bool(pattern_segs) if pattern else bool(user.shift and user.shift.is_active)
            status_calc = "working" if is_working else "off"

            roster_day = existing_days.get(user.id)
            if not roster_day:
                roster_day = StaffRosterDay(
                    hospital_id=self.hospital_id,
                    user_id=user.id,
                    roster_date=payload.roster_date,
                    status=status_calc,
                )
                self.db.add(roster_day)
                self.db.flush()
            else:
                roster_day.status = status_calc
                roster_day.notes = None
                self.db.query(StaffRosterSegment).filter(
                    StaffRosterSegment.roster_day_id == roster_day.id
                ).delete(synchronize_session=False)

            if pattern:
                for p_seg in pattern_segs:
                    st, et = compute_segment_interval(payload.roster_date, str(p_seg.start_time), str(p_seg.end_time))
                    self.db.add(
                        StaffRosterSegment(
                            hospital_id=self.hospital_id,
                            roster_day_id=roster_day.id,
                            user_id=user.id,
                            shift_type_id=p_seg.shift_type_id,
                            start_at=st,
                            end_at=et,
                            duty_kind=p_seg.duty_kind or "OTHER",
                            presence_type=p_seg.presence_type or "ONSITE",
                            is_bookable=p_seg.is_bookable,
                            department_id=p_seg.department_id or user.department_id,
                            notes=p_seg.notes,
                        )
                    )
            elif user.shift and user.shift.is_active:
                st, et = compute_segment_interval(payload.roster_date, str(user.shift.start_time), str(user.shift.end_time))
                is_doctor = (
                    (user.role and "doctor" in user.role.name.lower())
                    or bool(user.specialization)
                    or bool(user.medical_registration_number)
                )
                is_opd = is_doctor or "OPD" in (user.shift.name or "").upper()
                self.db.add(
                    StaffRosterSegment(
                        hospital_id=self.hospital_id,
                        roster_day_id=roster_day.id,
                        user_id=user.id,
                        shift_type_id=user.shift.id,
                        start_at=st,
                        end_at=et,
                        duty_kind="OPD" if is_opd else "OTHER",
                        presence_type="ONSITE",
                        is_bookable=is_opd,
                        department_id=user.shift.department_id or user.department_id,
                        notes=None,
                    )
                )

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="seed",
            details={
                "roster_date": payload.roster_date.isoformat(),
                "overwrite": payload.overwrite,
            },
        )
        self.db.commit()
        return self.get_shift_roster(payload.roster_date)

    def clear_shift_roster_overrides(self, roster_date: date) -> None:
        self.db.query(StaffRosterDay).filter(
            StaffRosterDay.hospital_id == self.hospital_id,
            StaffRosterDay.roster_date == roster_date,
        ).delete(synchronize_session=False)

        self.db.query(StaffDailyShift).filter(
            StaffDailyShift.hospital_id == self.hospital_id,
            StaffDailyShift.roster_date == roster_date,
        ).delete(synchronize_session=False)

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="clear",
            details={
                "roster_date": roster_date.isoformat(),
            },
        )
        self.db.commit()

    def get_staff_work_pattern(self, user_id: UUID) -> StaffWorkPatternResponse:
        user = (
            self.db.query(HospitalUser)
            .filter(HospitalUser.id == user_id, HospitalUser.hospital_id == self.hospital_id)
            .first()
        )
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff user not found")

        wp = (
            self.db.query(StaffWorkPattern)
            .options(
                selectinload(StaffWorkPattern.segments).joinedload(StaffWorkPatternSegment.shift_type),
                selectinload(StaffWorkPattern.segments).joinedload(StaffWorkPatternSegment.department),
            )
            .filter(
                StaffWorkPattern.hospital_id == self.hospital_id,
                StaffWorkPattern.user_id == user_id,
            )
            .first()
        )
        if not wp:
            wp = StaffWorkPattern(
                hospital_id=self.hospital_id,
                user_id=user_id,
                name=f"{user.name}'s Recurring Pattern",
                is_active=True,
            )
            self.db.add(wp)
            self.db.commit()
            self.db.refresh(wp)

        segments = [
            WorkPatternSegmentItem(
                id=s.id,
                day_of_week=s.day_of_week,
                shift_type_id=s.shift_type_id,
                shift_name=s.shift_type.name if s.shift_type else None,
                department_id=s.department_id,
                department_name=s.department.name if s.department else None,
                start_time=s.start_time.strftime("%H:%M") if hasattr(s.start_time, "strftime") else str(s.start_time)[:5],
                end_time=s.end_time.strftime("%H:%M") if hasattr(s.end_time, "strftime") else str(s.end_time)[:5],
                duty_kind=s.duty_kind or "OTHER",
                presence_type=s.presence_type or "ONSITE",
                is_bookable=s.is_bookable,
                notes=s.notes,
            )
            for s in sorted(wp.segments or [], key=lambda x: (x.day_of_week, x.start_time))
        ]

        return StaffWorkPatternResponse(
            id=wp.id,
            hospital_id=wp.hospital_id,
            user_id=wp.user_id,
            name=wp.name,
            is_active=wp.is_active,
            segments=segments,
        )

    def update_staff_work_pattern(
        self, user_id: UUID, payload: StaffWorkPatternUpdate
    ) -> StaffWorkPatternResponse:
        user = (
            self.db.query(HospitalUser)
            .filter(HospitalUser.id == user_id, HospitalUser.hospital_id == self.hospital_id)
            .first()
        )
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff user not found")

        wp = (
            self.db.query(StaffWorkPattern)
            .filter(
                StaffWorkPattern.hospital_id == self.hospital_id,
                StaffWorkPattern.user_id == user_id,
            )
            .first()
        )
        if not wp:
            wp = StaffWorkPattern(
                hospital_id=self.hospital_id,
                user_id=user_id,
                name=payload.name or f"{user.name}'s Recurring Pattern",
                is_active=payload.is_active,
            )
            self.db.add(wp)
            self.db.flush()
        else:
            if payload.name:
                wp.name = payload.name
            wp.is_active = payload.is_active

        # Replace all segments
        self.db.query(StaffWorkPatternSegment).filter(
            StaffWorkPatternSegment.pattern_id == wp.id
        ).delete(synchronize_session=False)

        # Validate overlaps within each day_of_week
        by_day: dict[int, list[tuple[time, time]]] = defaultdict(list)
        for item in payload.segments:
            st_parts = [int(p) for p in item.start_time.split(":")[:2]]
            et_parts = [int(p) for p in item.end_time.split(":")[:2]]
            st_val = time(st_parts[0], st_parts[1])
            et_val = time(et_parts[0], et_parts[1])

            if item.presence_type == "ONSITE":
                for prev_st, prev_et in by_day[item.day_of_week]:
                    if intervals_overlap(st_val, et_val, prev_st, prev_et):
                        raise HTTPException(
                            status_code=400,
                            detail=f"Recurring duty segment {item.start_time}-{item.end_time} overlaps another segment on day {item.day_of_week}",
                        )
                by_day[item.day_of_week].append((st_val, et_val))

            seg = StaffWorkPatternSegment(
                hospital_id=self.hospital_id,
                pattern_id=wp.id,
                day_of_week=item.day_of_week,
                shift_type_id=item.shift_type_id,
                department_id=item.department_id or user.department_id,
                start_time=st_val,
                end_time=et_val,
                duty_kind=item.duty_kind or "OTHER",
                presence_type=item.presence_type or "ONSITE",
                is_bookable=item.is_bookable,
                notes=item.notes,
            )
            self.db.add(seg)

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="update_pattern",
            entity_id=user_id,
            details={"pattern_id": str(wp.id)},
        )
        self.db.commit()
        return self.get_staff_work_pattern(user_id)

    def add_work_pattern_segment(
        self, user_id: UUID, payload: WorkPatternSegmentCreate
    ) -> WorkPatternSegmentItem:
        user = (
            self.db.query(HospitalUser)
            .filter(HospitalUser.id == user_id, HospitalUser.hospital_id == self.hospital_id)
            .first()
        )
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff user not found")

        wp = (
            self.db.query(StaffWorkPattern)
            .filter(
                StaffWorkPattern.hospital_id == self.hospital_id,
                StaffWorkPattern.user_id == user_id,
            )
            .first()
        )
        if not wp:
            wp = StaffWorkPattern(
                hospital_id=self.hospital_id,
                user_id=user_id,
                name=f"{user.name}'s Recurring Pattern",
                is_active=True,
            )
            self.db.add(wp)
            self.db.flush()

        st_parts = [int(p) for p in payload.start_time.split(":")[:2]]
        et_parts = [int(p) for p in payload.end_time.split(":")[:2]]
        st_val = time(st_parts[0], st_parts[1])
        et_val = time(et_parts[0], et_parts[1])

        # Validate overlap with existing segments on the same day_of_week
        if payload.presence_type == "ONSITE":
            existing = (
                self.db.query(StaffWorkPatternSegment)
                .filter(
                    StaffWorkPatternSegment.pattern_id == wp.id,
                    StaffWorkPatternSegment.day_of_week == payload.day_of_week,
                    StaffWorkPatternSegment.presence_type == "ONSITE",
                )
                .all()
            )
            for ex in existing:
                if intervals_overlap(st_val, et_val, ex.start_time, ex.end_time):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Recurring duty segment {payload.start_time}-{payload.end_time} overlaps another segment on day {payload.day_of_week}",
                    )

        seg = StaffWorkPatternSegment(
            hospital_id=self.hospital_id,
            pattern_id=wp.id,
            day_of_week=payload.day_of_week,
            shift_type_id=payload.shift_type_id,
            department_id=payload.department_id or user.department_id,
            start_time=st_val,
            end_time=et_val,
            duty_kind=payload.duty_kind or "OTHER",
            presence_type=payload.presence_type or "ONSITE",
            is_bookable=payload.is_bookable,
            notes=payload.notes,
        )
        self.db.add(seg)
        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="update_pattern",
            entity_id=user_id,
            details={"pattern_id": str(wp.id)},
        )
        self.db.commit()
        self.db.refresh(seg)

        return WorkPatternSegmentItem(
            id=seg.id,
            day_of_week=seg.day_of_week,
            shift_type_id=seg.shift_type_id,
            shift_name=seg.shift_type.name if seg.shift_type else None,
            department_id=seg.department_id,
            department_name=seg.department.name if seg.department else None,
            start_time=seg.start_time.strftime("%H:%M"),
            end_time=seg.end_time.strftime("%H:%M"),
            duty_kind=seg.duty_kind,
            presence_type=seg.presence_type,
            is_bookable=seg.is_bookable,
            notes=seg.notes,
        )

    def delete_work_pattern_segment(self, user_id: UUID, segment_id: UUID) -> dict[str, Any]:
        wp = (
            self.db.query(StaffWorkPattern)
            .filter(
                StaffWorkPattern.hospital_id == self.hospital_id,
                StaffWorkPattern.user_id == user_id,
            )
            .first()
        )
        if not wp:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work pattern not found")

        deleted = (
            self.db.query(StaffWorkPatternSegment)
            .filter(
                StaffWorkPatternSegment.id == segment_id,
                StaffWorkPatternSegment.pattern_id == wp.id,
            )
            .delete(synchronize_session=False)
        )
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work pattern segment not found")

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="update_pattern",
            entity_id=user_id,
            details={"pattern_id": str(wp.id), "deleted_segment_id": str(segment_id)},
        )
        self.db.commit()
        return {"success": True, "message": "Work pattern segment deleted"}

    def clear_work_pattern_day(self, user_id: UUID, day_of_week: int) -> dict[str, Any]:
        wp = (
            self.db.query(StaffWorkPattern)
            .filter(
                StaffWorkPattern.hospital_id == self.hospital_id,
                StaffWorkPattern.user_id == user_id,
            )
            .first()
        )
        if not wp:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work pattern not found")

        self.db.query(StaffWorkPatternSegment).filter(
            StaffWorkPatternSegment.pattern_id == wp.id,
            StaffWorkPatternSegment.day_of_week == day_of_week,
        ).delete(synchronize_session=False)

        stage_sync_event(
            self.db,
            hospital_id=self.hospital_id,
            entity_type="staff_roster",
            action="update_pattern",
            entity_id=user_id,
            details={"pattern_id": str(wp.id), "cleared_day": day_of_week},
        )
        self.db.commit()
        return {"success": True, "message": f"Day {day_of_week} cleared"}

    def get_shift_roster_snapshot(self, roster_date: date) -> ShiftRosterSnapshot:
        hospital = self.db.query(Hospital).filter(Hospital.id == self.hospital_id).first()
        hospital_name = hospital.name if hospital else "Hospital"
        holiday_name = self._holiday_name_for(roster_date)
        entries = self._build_roster_entries(roster_date)

        date_label = roster_date.strftime("%d %b %Y")
        lines = [
            f"*{hospital_name}*",
            f"📅 Daily Shift Roster — {date_label}",
        ]
        if holiday_name:
            lines.append(f"🏖 Holiday: {holiday_name}")
        lines.append("")

        on_duty = [e for e in entries if e.status in ("on_duty", "working") and (e.segments or e.shift_id)]
        off_leave = [e for e in entries if e.status in ("off", "leave") or (not e.segments and not e.shift_id)]

        by_dept: dict[str, list[ShiftRosterEntry]] = defaultdict(list)
        for e in on_duty:
            by_dept[e.department_name or "Unassigned"].append(e)

        total_assignments = 0
        for dept in sorted(by_dept.keys()):
            lines.append(f"*{dept}*")
            for e in sorted(by_dept[dept], key=lambda x: x.name.lower()):
                role = f" ({e.role_name})" if e.role_name else ""
                if e.segments:
                    total_assignments += len(e.segments)
                    if len(e.segments) == 1:
                        seg = e.segments[0]
                        duty_info = f" · {seg.duty_kind}" if seg.duty_kind != "OPD" else ""
                        lines.append(f"  - {e.name}{role}: {seg.start_time}–{seg.end_time}{duty_info}")
                    else:
                        lines.append(f"  - {e.name}{role}:")
                        for seg in e.segments:
                            duty_info = f" · {seg.duty_kind}" if seg.duty_kind else ""
                            cross_info = " (+1d)" if seg.is_cross_midnight else ""
                            lines.append(f"    • {seg.start_time}–{seg.end_time}{cross_info}{duty_info}")
                elif e.shift_name:
                    total_assignments += 1
                    time_bit = f" ({e.start_time}–{e.end_time})" if e.start_time and e.end_time else ""
                    lines.append(f"  - {e.name}{role}: {e.shift_name}{time_bit}")
            lines.append("")

        if off_leave:
            lines.append("*Off / Leave / Unassigned*")
            for e in sorted(off_leave, key=lambda x: x.name.lower()):
                status_label = {"leave": "Leave", "off": "Off"}.get(e.status, "Unassigned")
                role = f" ({e.role_name})" if e.role_name else ""
                note = f" — {e.notes}" if e.notes else ""
                lines.append(f"- {e.name}{role} — {status_label}{note}")
            lines.append("")

        unique_working = len(on_duty)
        lines.append(f"_Staff working: {unique_working} | Total duty assignments: {total_assignments} | Total staff: {len(entries)}_")
        lines.append("_Shared from Ultrion HMS_")
        text = "\n".join(lines).strip() + "\n"

        return ShiftRosterSnapshot(
            roster_date=roster_date,
            hospital_name=hospital_name,
            holiday_name=holiday_name,
            text=text,
            entries_count=len(entries),
        )


    # ── Hospital Facility Settings (SCR-022) ──────────────────────────────────
    def get_facility_settings(self) -> HospitalFacilitySettings:
        hospital = self.db.query(Hospital).filter(Hospital.id == self.hospital_id).first()
        if not hospital:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")

        stored = hospital.facility_settings or {}
        return HospitalFacilitySettings(
            hospital_id=hospital.hospital_id,
            legal_name=stored.get("legal_name", hospital.name),
            display_name=stored.get("display_name", hospital.name.split(" & ")[0] if " & " in hospital.name else hospital.name),
            institutional_code=stored.get("institutional_code", hospital.hospital_id),
            address=stored.get("address", hospital.address),
            phone=stored.get("phone", hospital.phone),
            email=stored.get("email", hospital.email),
            cea_registration=stored.get("cea_registration", "CEA/DL/2024/0981"),
            nabh_accreditation_ref=stored.get("nabh_accreditation_ref", "NABH-2024-0418"),
            nabh_status=stored.get("nabh_status", "Full Accreditation Valid"),
            tax_gstin=stored.get("tax_gstin", "07AAACH1234F1Z5"),
            rohini_id=stored.get("rohini_id", "890000124578"),
            aerb_license_no=stored.get("aerb_license_no", "AERB/MED/DL/0412"),
            pharmacy_license_no=stored.get("pharmacy_license_no", "DL-20B/21B-45892"),
            prescription_header_text=stored.get("prescription_header_text", f"{hospital.name.upper()} • CLINICAL SERVICES"),
            prescription_footer_text=stored.get("prescription_footer_text", f"Emergency Hotline: {hospital.phone} • Contact: {hospital.email}"),
            abdm_facility_id=stored.get("abdm_facility_id", "IN0710000412"),
            abdm_m1_active=stored.get("abdm_m1_active", True),
            abdm_m2_active=stored.get("abdm_m2_active", True),
            abdm_m3_active=stored.get("abdm_m3_active", True),
            sms_gateway_active=stored.get("sms_gateway_active", True),
            whatsapp_gateway_active=stored.get("whatsapp_gateway_active", True),
            dlt_sender_id=stored.get("dlt_sender_id", "CGHHSP"),
        )

    def update_facility_settings(self, payload: HospitalFacilitySettingsUpdate) -> HospitalFacilitySettings:
        hospital = self.db.query(Hospital).filter(Hospital.id == self.hospital_id).first()
        if not hospital:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")

        updates = payload.model_dump(exclude_unset=True)
        if "legal_name" in updates and updates["legal_name"]:
            hospital.name = updates["legal_name"]
        if "address" in updates and updates["address"]:
            hospital.address = updates["address"]
        if "phone" in updates and updates["phone"]:
            hospital.phone = updates["phone"]
        if "email" in updates and updates["email"]:
            hospital.email = updates["email"]

        stored = dict(hospital.facility_settings or {})
        stored.update(updates)
        hospital.facility_settings = stored
        self.db.flush()

        stage_sync_event(self.db, self.hospital_id, "facility_settings", "update", self.hospital_id)
        self._audit("update", "facility_settings", self.hospital_id, f"Updated facility settings for {hospital.name}")
        self.db.commit()
        return self.get_facility_settings()


