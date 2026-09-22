"""Authentication API router conforming to UltrionTech-Backend-Template modules/auth/api/ specification."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from config.settings import Settings, get_settings
from infrastructure.postgres.session import get_transitional_sync_session
from modules.auth.contracts.auth_contracts import LoginRequest, LoginResponse, MeResponse
from modules.doctors.entities.doctor import HospitalUser, StaffRole, HospitalUserRole, HospitalUserDepartment, HospitalUserLocation
from modules.masters.entities.organization_entities import Department
from modules.tenancy.entities.hospital import Hospital
from shared.audit.service import write_audit_log
from shared.auth.dependencies import get_current_user
from shared.auth.jwt import create_access_token
from shared.auth.security import verify_password

logger = logging.getLogger("hms.api")

router = APIRouter(prefix="/auth", tags=["auth"])


def _extract_user_session_info(db: Session, staff: HospitalUser):
    """Aggregate assigned roles, departments, locations, and union permissions."""
    assigned_roles = (
        db.query(HospitalUserRole)
        .options(joinedload(HospitalUserRole.role).joinedload(StaffRole.permissions))
        .filter(HospitalUserRole.user_id == staff.id)
        .all()
    )

    roles_list: list[str] = []
    role_ids: list[str] = []
    primary_role_name = None

    for ur in assigned_roles:
        if ur.role and ur.role.is_active:
            roles_list.append(ur.role.name)
            role_ids.append(str(ur.role_id))
            if ur.is_primary:
                primary_role_name = ur.role.name

    # Fallback to direct role_id if no active assigned_roles
    if not roles_list and staff.role and staff.role.is_active:
        roles_list.append(staff.role.name)
        role_ids.append(str(staff.role_id))
        primary_role_name = staff.role.name

    if not primary_role_name and roles_list:
        primary_role_name = roles_list[0]

    # Compute union of permissions
    merged_perms: dict[str, dict] = {}
    sources = []
    if assigned_roles:
        sources.extend([ur.role for ur in assigned_roles if ur.role and ur.role.is_active])
    elif staff.role and staff.role.is_active:
        sources.append(staff.role)

    for role_obj in sources:
        for p in role_obj.permissions:
            if not (p.can_view or p.can_edit or getattr(p, "can_create", False) or getattr(p, "can_delete", False)):
                continue
            if p.module_key not in merged_perms:
                merged_perms[p.module_key] = {
                    "module_key": p.module_key,
                    "can_view": bool(p.can_view),
                    "can_edit": bool(p.can_edit),
                    "can_create": bool(getattr(p, "can_create", False)),
                    "can_delete": bool(getattr(p, "can_delete", False)),
                    "can_approve": bool(getattr(p, "can_approve", False)),
                    "can_validate": bool(getattr(p, "can_validate", False)),
                    "can_release": bool(getattr(p, "can_release", False)),
                    "can_dispense": bool(getattr(p, "can_dispense", False)),
                    "can_refund": bool(getattr(p, "can_refund", False)),
                    "can_cancel": bool(getattr(p, "can_cancel", False)),
                    "can_administer": bool(getattr(p, "can_administer", False)),
                }
            else:
                curr = merged_perms[p.module_key]
                curr["can_view"] = curr["can_view"] or bool(p.can_view)
                curr["can_edit"] = curr["can_edit"] or bool(p.can_edit)
                curr["can_create"] = curr["can_create"] or bool(getattr(p, "can_create", False))
                curr["can_delete"] = curr["can_delete"] or bool(getattr(p, "can_delete", False))
                curr["can_approve"] = curr["can_approve"] or bool(getattr(p, "can_approve", False))
                curr["can_validate"] = curr["can_validate"] or bool(getattr(p, "can_validate", False))
                curr["can_release"] = curr["can_release"] or bool(getattr(p, "can_release", False))
                curr["can_dispense"] = curr["can_dispense"] or bool(getattr(p, "can_dispense", False))
                curr["can_refund"] = curr["can_refund"] or bool(getattr(p, "can_refund", False))
                curr["can_cancel"] = curr["can_cancel"] or bool(getattr(p, "can_cancel", False))
                curr["can_administer"] = curr["can_administer"] or bool(getattr(p, "can_administer", False))

    # Departments
    assigned_depts = db.query(HospitalUserDepartment).filter(HospitalUserDepartment.user_id == staff.id).all()
    dept_ids = [str(d.department_id) for d in assigned_depts]
    if not dept_ids and staff.department_id:
        dept_ids.append(str(staff.department_id))

    primary_dept_id = staff.department_id
    if not primary_dept_id and assigned_depts:
        for d in assigned_depts:
            if d.is_primary:
                primary_dept_id = d.department_id
                break
        if not primary_dept_id:
            primary_dept_id = assigned_depts[0].department_id

    dept_name = None
    if primary_dept_id:
        dept_record = db.query(Department).filter(Department.id == primary_dept_id).first()
        if dept_record:
            dept_name = dept_record.name

    # Wards
    assigned_locs = db.query(HospitalUserLocation).filter(HospitalUserLocation.user_id == staff.id).all()
    ward_ids = [str(l.ward_id) for l in assigned_locs if l.ward_id]

    return {
        "roles": roles_list,
        "role_ids": role_ids,
        "primary_role": primary_role_name or (staff.role.name if staff.role else "hospital_staff"),
        "permissions": list(merged_perms.values()),
        "department_id": str(primary_dept_id) if primary_dept_id else None,
        "department_name": dept_name,
        "department_ids": dept_ids,
        "ward_ids": ward_ids,
    }


@router.get("/me", response_model=MeResponse)
def me(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_transitional_sync_session),
) -> MeResponse:
    """Return server-authoritative session state for the current token holder."""
    role = user.get("role")

    # Super admin: no HospitalUser row; treated as always active.
    if role == "super_admin":
        return MeResponse(
            sub=str(user.get("sub") or ""),
            role="super_admin",
            name=user.get("name"),
            is_active=True,
            token_version=1,
        )

    # Hospital admin: account lives on the Hospital row.
    if role == "hospital_admin":
        hospital = None
        if user.get("hospital_uuid"):
            hospital = (
                db.query(Hospital)
                .filter(Hospital.id == UUID(str(user["hospital_uuid"])))
                .first()
            )
        active = bool(hospital and hospital.is_active)
        if not active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Hospital account is inactive.",
            )
        return MeResponse(
            sub=str(user.get("sub") or ""),
            role="hospital_admin",
            name=user.get("name") or hospital.name,
            hospital_id=hospital.hospital_id,
            hospital_uuid=str(hospital.id),
            is_active=True,
            token_version=1,
        )

    # Hospital staff: authoritative state from the HospitalUser row.
    user_id = user.get("user_id")
    hospital_uuid = user.get("hospital_uuid")
    staff = None
    if user_id and hospital_uuid:
        staff = (
            db.query(HospitalUser)
            .options(joinedload(HospitalUser.role).joinedload(StaffRole.permissions))
            .filter(HospitalUser.id == UUID(str(user_id)), HospitalUser.hospital_id == UUID(str(hospital_uuid)))
            .first()
        )
    if not staff:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account no longer exists.",
        )
    if not staff.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive.",
        )

    # FLAW-024: Validate token_version for immediate session invalidation
    token_v = user.get("token_version")
    if token_v is not None and staff.token_version > token_v:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or revoked. Please log in again.",
        )

    session_info = _extract_user_session_info(db, staff)
    if not session_info["roles"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your role is inactive or no active role assigned. Contact admin.",
        )

    hospital = db.query(Hospital).filter(Hospital.id == staff.hospital_id).first()
    return MeResponse(
        sub=str(user.get("sub") or ""),
        role="hospital_staff",
        name=staff.name,
        hospital_id=hospital.hospital_id if hospital else None,
        hospital_uuid=str(staff.hospital_id),
        staff_role_name=session_info["primary_role"],
        roles=session_info["roles"],
        primary_role=session_info["primary_role"],
        department_id=session_info["department_id"],
        department_name=session_info["department_name"],
        department_ids=session_info["department_ids"],
        ward_ids=session_info["ward_ids"],
        user_id=str(staff.id),
        is_active=True,
        token_version=int(getattr(staff, "token_version", 1) or 1),
        permissions=session_info["permissions"],
    )


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    db: Session = Depends(get_transitional_sync_session),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    """Authenticate super admin, hospital admin, or hospital staff member."""
    email = payload.email.strip().lower()
    logger.info("Login attempt for %s", email)

    # 1. Super admin authentication (UNTOUCHED & FROZEN)
    if (
        email == settings.super_admin_email.lower()
        and payload.password == settings.super_admin_password
    ):
        logger.info("Super admin login success for %s", email)
        token = create_access_token({"sub": email, "role": "super_admin"})
        return LoginResponse(access_token=token, role="super_admin", name="Super Admin")

    # 2. Hospital admin authentication (UNTOUCHED & FROZEN)
    hospital = (
        db.query(Hospital)
        .filter(Hospital.email.ilike(email), Hospital.is_active.is_(True))
        .first()
    )
    if hospital and verify_password(payload.password, hospital.password_hash):
        logger.info(
            "Hospital admin login success for %s (%s)", email, hospital.hospital_id
        )
        token = create_access_token(
            {
                "sub": email,
                "role": "hospital_admin",
                "name": hospital.name,
                "hospital_uuid": str(hospital.id),
                "hospital_id": hospital.hospital_id,
            }
        )
        write_audit_log(
            db,
            hospital_id=hospital.id,
            actor={
                "sub": email,
                "name": hospital.name,
                "role": "hospital_admin",
                "staff_role_name": "Hospital Admin",
            },
            action="login",
            entity_type="session",
            summary=f"Hospital admin signed in ({hospital.name})",
        )
        db.commit()
        return LoginResponse(
            access_token=token,
            role="hospital_admin",
            hospital_id=hospital.hospital_id,
            name=hospital.name,
            plan=hospital.plan,
        )

    # 3. Hospital staff authentication (WITH MULTIPLE ROLES, DEPARTMENT, WARDS)
    staff = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role).joinedload(StaffRole.permissions))
        .filter(HospitalUser.email.ilike(email), HospitalUser.is_active.is_(True))
        .first()
    )
    if staff and verify_password(payload.password, staff.password_hash):
        hospital = (
            db.query(Hospital)
            .filter(Hospital.id == staff.hospital_id, Hospital.is_active.is_(True))
            .first()
        )
        if not hospital:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Hospital account is inactive.",
            )

        session_info = _extract_user_session_info(db, staff)
        if not session_info["roles"]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Your role is inactive or no active role assigned. Contact admin.",
            )

        logger.info(
            "Hospital staff login success for %s (%s / %s)",
            email,
            hospital.hospital_id,
            session_info["primary_role"],
        )
        token = create_access_token(
            {
                "sub": email,
                "role": "hospital_staff",
                "name": staff.name,
                "staff_role_name": session_info["primary_role"],
                "roles": session_info["roles"],
                "primary_role": session_info["primary_role"],
                "hospital_uuid": str(hospital.id),
                "hospital_id": hospital.hospital_id,
                "user_id": str(staff.id),
                "staff_role_id": session_info["role_ids"][0] if session_info["role_ids"] else str(staff.role_id),
                "staff_role_ids": session_info["role_ids"],
                "department_id": session_info["department_id"],
                "department_ids": session_info["department_ids"],
                "ward_ids": session_info["ward_ids"],
                "token_version": int(getattr(staff, "token_version", 1) or 1),
            }
        )
        write_audit_log(
            db,
            hospital_id=hospital.id,
            actor={
                "sub": email,
                "name": staff.name,
                "role": "hospital_staff",
                "staff_role_name": session_info["primary_role"],
            },
            action="login",
            entity_type="session",
            summary=f"{session_info['primary_role']} {staff.name} signed in",
        )
        db.commit()
        return LoginResponse(
            access_token=token,
            role="hospital_staff",
            hospital_id=hospital.hospital_id,
            name=staff.name,
            plan=hospital.plan,
            staff_role_name=session_info["primary_role"],
            roles=session_info["roles"],
            primary_role=session_info["primary_role"],
            department_id=session_info["department_id"],
            department_name=session_info["department_name"],
            department_ids=session_info["department_ids"],
            ward_ids=session_info["ward_ids"],
            permissions=session_info["permissions"],
            user_id=str(staff.id),
            token_version=int(getattr(staff, "token_version", 1) or 1),
        )

    logger.warning("Login failed for %s", email)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials. Please check your email and password.",
    )
