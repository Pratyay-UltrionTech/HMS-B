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
from datetime import date, datetime
import re
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.admin.contracts.admin_contracts import (
    BASIC_MODULE_KEYS,
    BASIC_MODULE_LABELS,
    AuditLogResponse,
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
    HospitalFacilitySettings,
    HospitalFacilitySettingsUpdate,
)
from hms_migration.modules.doctors.entities.doctor import (
    Holiday,
    HospitalUser,
    RoleCustomField,
    RolePermission,
    ShiftType,
    StaffDailyShift,
    StaffRole,
)
from hms_migration.modules.masters.entities.organization_entities import Department
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.audit import write_audit_log
from hms_migration.shared.audit.entities.audit_log import AuditLog
from hms_migration.shared.auth.security import hash_password


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
            .options(joinedload(StaffRole.custom_fields), joinedload(StaffRole.permissions))
            .filter(StaffRole.id == role_id, StaffRole.hospital_id == self.hospital_id)
            .first()
        )
        if not role:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
        return role

    def list_roles(self) -> list[RoleResponse]:
        roles = (
            self.db.query(StaffRole)
            .options(joinedload(StaffRole.custom_fields), joinedload(StaffRole.permissions))
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
                )
            )

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
                    )
                )

        self._audit("update", "role", role.id, f"Updated role '{role.name}'")
        self.db.commit()
        return self._role_to_response(self._get_role(role.id))

    def delete_role(self, role_id: UUID) -> None:
        role = self._get_role(role_id)
        assigned = (
            self.db.query(HospitalUser)
            .filter(HospitalUser.hospital_id == self.hospital_id, HospitalUser.role_id == role_id)
            .count()
        )
        if assigned > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete role: {assigned} active staff user(s) are assigned to it.",
            )
        name = role.name
        self.db.delete(role)
        self._audit("delete", "role", role_id, f"Deleted role '{name}'")
        self.db.commit()

    # ── Users ─────────────────────────────────────────────────────────────────
    def _validate_department_shift(
        self,
        department_id: UUID | None,
        shift_id: UUID | None,
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
        if department_id is not None and shift.department_id != department_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selected shift does not belong to the selected department",
            )
        return shift

    def _user_to_response(self, user: HospitalUser) -> HospitalUserResponse:
        shift = user.shift
        dept = self.db.query(Department).filter(Department.id == shift.department_id).first() if shift and shift.department_id else None
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
            shift_department_name=dept.name if dept else None,
            shift_start_time=fmt_shift_time(shift.start_time) if shift else None,
            shift_end_time=fmt_shift_time(shift.end_time) if shift else None,
        )

    def list_users(self) -> list[HospitalUserResponse]:
        users = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role), joinedload(HospitalUser.shift))
            .filter(HospitalUser.hospital_id == self.hospital_id)
            .order_by(HospitalUser.name)
            .all()
        )
        return [self._user_to_response(u) for u in users]

    def create_user(self, payload: HospitalUserCreate) -> HospitalUserResponse:
        role = self._get_role(payload.role_id)
        email = payload.email.strip().lower()
        exists = (
            self.db.query(HospitalUser)
            .filter(HospitalUser.hospital_id == self.hospital_id, HospitalUser.email == email)
            .first()
        )
        if exists:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists.")

        shift_id = None
        shift_label = None
        if payload.shift_id or payload.department_id:
            shift = self._validate_department_shift(
                department_id=payload.department_id,
                shift_id=payload.shift_id,
            )
            if shift:
                shift_id = shift.id
                shift_label = shift.name

        user = HospitalUser(
            hospital_id=self.hospital_id,
            role_id=payload.role_id,
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
        self._audit(
            "create",
            "user",
            user.id,
            f"Created user '{user.name}' with role '{role.name}'",
            details={"email": user.email, "role": role.name, "shift": shift_label},
        )
        self.db.commit()
        user = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role), joinedload(HospitalUser.shift))
            .filter(HospitalUser.id == user.id)
            .first()
        )
        return self._user_to_response(user)

    def update_user(self, user_id: UUID, payload: HospitalUserUpdate) -> HospitalUserResponse:
        user = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role), joinedload(HospitalUser.shift))
            .filter(HospitalUser.id == user_id, HospitalUser.hospital_id == self.hospital_id)
            .first()
        )
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        data = payload.model_dump(exclude_unset=True, exclude={"password", "department_id"})
        raw = payload.model_dump(exclude_unset=True)
        if "role_id" in data and data["role_id"]:
            self._get_role(data["role_id"])

        if "shift_id" in data or "department_id" in raw:
            effective_shift_id = data["shift_id"] if "shift_id" in data else user.shift_id
            if "shift_id" in data and not data["shift_id"]:
                effective_shift_id = None
                data["shift_id"] = None
            same_shift = effective_shift_id is not None and user.shift_id == effective_shift_id
            dept_for_check = raw.get("department_id") if "department_id" in raw else None
            if effective_shift_id is not None or "department_id" in raw:
                self._validate_department_shift(
                    department_id=dept_for_check,
                    shift_id=effective_shift_id,
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

        for k, v in data.items():
            setattr(user, k, v)
        if payload.password:
            user.password_hash = hash_password(payload.password)

        self._audit(
            "update",
            "user",
            user.id,
            f"Updated user '{user.name}'",
            details={"email": user.email, "password_changed": bool(payload.password)},
        )
        self.db.commit()
        user = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role), joinedload(HospitalUser.shift))
            .filter(HospitalUser.id == user_id)
            .first()
        )
        return self._user_to_response(user)

    def delete_user(self, user_id: UUID) -> None:
        user = self.db.query(HospitalUser).filter(HospitalUser.id == user_id, HospitalUser.hospital_id == self.hospital_id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        name = user.name
        email = user.email
        self.db.delete(user)
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
        row = (
            self.db.query(Holiday)
            .filter(Holiday.hospital_id == self.hospital_id, Holiday.holiday_date == roster_date)
            .order_by(Holiday.name)
            .first()
        )
        return row.name if row else None

    def _build_roster_entries(self, roster_date: date) -> list[ShiftRosterEntry]:
        users = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role), joinedload(HospitalUser.shift))
            .filter(HospitalUser.hospital_id == self.hospital_id, HospitalUser.is_active.is_(True))
            .order_by(HospitalUser.name)
            .all()
        )
        overrides = {
            row.user_id: row
            for row in self.db.query(StaffDailyShift)
            .options(joinedload(StaffDailyShift.shift))
            .filter(StaffDailyShift.hospital_id == self.hospital_id, StaffDailyShift.roster_date == roster_date)
            .all()
        }
        shift_ids = {o.shift_id for o in overrides.values() if o.shift_id}
        shift_ids |= {u.shift_id for u in users if u.shift_id}
        shifts_by_id: dict[UUID, ShiftType] = {}
        if shift_ids:
            for s in self.db.query(ShiftType).filter(ShiftType.id.in_(shift_ids)).all():
                shifts_by_id[s.id] = s

        dept_ids = {s.department_id for s in shifts_by_id.values() if s.department_id}
        depts_by_id: dict[UUID, Department] = {}
        if dept_ids:
            for d in self.db.query(Department).filter(Department.id.in_(dept_ids)).all():
                depts_by_id[d.id] = d

        entries: list[ShiftRosterEntry] = []
        for user in users:
            override = overrides.get(user.id)
            if override:
                status_val = override.status or "on_duty"
                shift = shifts_by_id.get(override.shift_id) if override.shift_id else None
                if status_val in ("off", "leave"):
                    shift = None
                dept = depts_by_id.get(shift.department_id) if shift and shift.department_id else None
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
                        department_name=dept.name if dept else None,
                        start_time=fmt_time(shift.start_time) if shift else None,
                        end_time=fmt_time(shift.end_time) if shift else None,
                        status=status_val,
                        is_override=True,
                        notes=override.notes,
                        default_shift_id=user.shift_id,
                    )
                )
            else:
                shift = shifts_by_id.get(user.shift_id) if user.shift_id else None
                dept = depts_by_id.get(shift.department_id) if shift and shift.department_id else None
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
                        department_name=dept.name if dept else None,
                        start_time=fmt_time(shift.start_time) if shift else None,
                        end_time=fmt_time(shift.end_time) if shift else None,
                        status="on_duty" if shift else "off",
                        is_override=False,
                        notes=None,
                        default_shift_id=user.shift_id,
                    )
                )
        return entries

    def get_shift_roster(self, roster_date: date) -> ShiftRosterResponse:
        hospital = self.db.query(Hospital).filter(Hospital.id == self.hospital_id).first()
        entries = self._build_roster_entries(roster_date)
        return ShiftRosterResponse(
            roster_date=roster_date,
            hospital_name=hospital.name if hospital else "Hospital",
            holiday_name=self._holiday_name_for(roster_date),
            entries=entries,
        )

    def assign_shift_roster(self, payload: ShiftRosterAssign) -> ShiftRosterEntry:
        user = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role))
            .filter(HospitalUser.id == payload.user_id, HospitalUser.hospital_id == self.hospital_id)
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
                self.db.query(ShiftType)
                .filter(ShiftType.id == shift_id, ShiftType.hospital_id == self.hospital_id)
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
        self.db.commit()

        entries = self._build_roster_entries(payload.roster_date)
        for e in entries:
            if e.user_id == payload.user_id:
                return e
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to load assignment")

    def seed_shift_roster(self, payload: ShiftRosterSeed) -> ShiftRosterResponse:
        users = (
            self.db.query(HospitalUser)
            .filter(HospitalUser.hospital_id == self.hospital_id, HospitalUser.is_active.is_(True))
            .all()
        )
        existing = {
            row.user_id: row
            for row in self.db.query(StaffDailyShift)
            .filter(StaffDailyShift.hospital_id == self.hospital_id, StaffDailyShift.roster_date == payload.roster_date)
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
                self.db.add(
                    StaffDailyShift(
                        hospital_id=self.hospital_id,
                        user_id=user.id,
                        roster_date=payload.roster_date,
                        shift_id=user.shift_id,
                        status=status_val,
                        notes=None,
                    )
                )
        self.db.commit()
        hospital = self.db.query(Hospital).filter(Hospital.id == self.hospital_id).first()
        entries = self._build_roster_entries(payload.roster_date)
        return ShiftRosterResponse(
            roster_date=payload.roster_date,
            hospital_name=hospital.name if hospital else "Hospital",
            holiday_name=self._holiday_name_for(payload.roster_date),
            entries=entries,
        )

    def clear_shift_roster_overrides(self, roster_date: date) -> None:
        self.db.query(StaffDailyShift).filter(
            StaffDailyShift.hospital_id == self.hospital_id,
            StaffDailyShift.roster_date == roster_date,
        ).delete(synchronize_session=False)
        self.db.commit()

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

        stored = _FACILITY_SETTINGS_STORE.get(self.hospital_id, {})
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

        self.db.flush()

        stored = _FACILITY_SETTINGS_STORE.setdefault(self.hospital_id, {})
        stored.update(updates)

        self._audit("update", "facility_settings", self.hospital_id, f"Updated facility settings for {hospital.name}")
        self.db.commit()
        return self.get_facility_settings()


_FACILITY_SETTINGS_STORE: dict[UUID, dict[str, Any]] = {}

