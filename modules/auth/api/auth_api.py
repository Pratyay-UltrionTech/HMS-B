"""Authentication API router conforming to UltrionTech-Backend-Template modules/auth/api/ specification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import secrets
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from config.settings import Settings, get_settings
from infrastructure.postgres.session import get_transitional_sync_session
from modules.auth.contracts.auth_contracts import (
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    ResetPasswordRequest,
    SetupPasswordRequest,
)
from modules.auth.entities.login_attempt import LoginAttempt
from modules.auth.entities.setup_token import AccountSetupToken
from modules.doctors.entities.doctor import (
    HospitalUser,
    HospitalUserDepartment,
    HospitalUserLocation,
    HospitalUserRole,
    StaffRole,
)
from modules.masters.entities.organization_entities import Department
from modules.tenancy.entities.hospital import Hospital
from shared.audit.service import write_audit_log
from shared.auth.dependencies import check_hospital_tenant_active, get_current_user
from shared.auth.jwt import create_access_token
from shared.auth.security import hash_password, verify_password

logger = logging.getLogger("hms.api")

router = APIRouter(prefix="/auth", tags=["auth"])



def _extract_user_session_info(db: Session, staff: HospitalUser):
    """Aggregate assigned roles, departments, locations, and union permissions."""
    assigned_roles = (
        db.query(HospitalUserRole)
        .options(joinedload(HospitalUserRole.role).selectinload(StaffRole.permissions))
        .filter(HospitalUserRole.user_id == staff.id, HospitalUserRole.hospital_id == staff.hospital_id,
                or_(HospitalUserRole.expires_at.is_(None), HospitalUserRole.expires_at > datetime.now(timezone.utc)))
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

    # All hospital users must have an authoritative active tenant in database
    if role in {"hospital_admin", "hospital_staff"}:
        check_hospital_tenant_active(user.get("hospital_uuid"), db)

    user_id = user.get("user_id")
    hospital_uuid = user.get("hospital_uuid")

    # When user_id is present, principal is a human HospitalUser
    if user_id and hospital_uuid:
        staff = (
            db.query(HospitalUser)
            .options(joinedload(HospitalUser.role).selectinload(StaffRole.permissions))
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

        token_v = user.get("token_version")
        token_v_int = int(token_v) if token_v is not None else 1
        if staff.token_version and staff.token_version > token_v_int:
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

        # Canonical admin role detection
        is_admin = False
        if staff.role and (
            getattr(staff.role, "is_admin_role", False)
            or getattr(staff.role, "code", None) == "HOSPITAL_ADMIN"
            or (staff.role.name and staff.role.name.lower() in ("hospital admin", "hospital_admin"))
        ):
            is_admin = True
        elif staff.assigned_roles:
            for ur in staff.assigned_roles:
                if (
                    ur.role
                    and (
                        getattr(ur.role, "is_admin_role", False)
                        or getattr(ur.role, "code", None) == "HOSPITAL_ADMIN"
                        or (ur.role.name and ur.role.name.lower() in ("hospital admin", "hospital_admin"))
                    )
                    and ur.role.is_active
                ):
                    is_admin = True
                    break

        hospital = db.query(Hospital).filter(Hospital.id == staff.hospital_id).first()
        effective_role = "hospital_admin" if is_admin else "hospital_staff"
        return MeResponse(
            sub=str(user.get("sub") or ""),
            role=effective_role,
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

    # Legacy hospital organization principal (no user_id in token)
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
        if getattr(hospital, "auth_model", "user_accounts") == "user_accounts":
            user_count = db.query(HospitalUser.id).filter(HospitalUser.hospital_id == hospital.id).count()
            if user_count > 0:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Organization credential login is disabled. Please authenticate using your personal administrator account.",
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

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized context.",
    )


def _check_login_rate_limit(db: Session, key: str, max_attempts: int = 10, window_seconds: int = 60) -> None:
    key_hash = hashlib.sha256(key.strip().lower().encode("utf-8")).hexdigest()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    count = (
        db.query(LoginAttempt)
        .filter(
            LoginAttempt.key_hash == key_hash,
            LoginAttempt.success.is_(False),
            LoginAttempt.attempted_at >= cutoff,
        )
        .count()
    )
    if count >= max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please wait before retrying.",
        )


def _record_login_failure(db: Session, key: str) -> None:
    key_hash = hashlib.sha256(key.strip().lower().encode("utf-8")).hexdigest()
    attempt = LoginAttempt(
        key_hash=key_hash,
        success=False,
    )
    db.add(attempt)
    db.commit()


def _clear_login_failures(db: Session, key: str) -> None:
    key_hash = hashlib.sha256(key.strip().lower().encode("utf-8")).hexdigest()
    db.query(LoginAttempt).filter(LoginAttempt.key_hash == key_hash).delete(synchronize_session=False)
    db.commit()


def _build_user_login_response(
    db: Session, staff: HospitalUser, hospital: Hospital, email: str
) -> LoginResponse:
    session_info = _extract_user_session_info(db, staff)
    if not session_info["roles"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your role is inactive or no active role assigned. Contact admin.",
        )

    # Determine canonical admin authority
    is_admin = False
    if staff.role and (
        getattr(staff.role, "is_admin_role", False)
        or getattr(staff.role, "code", None) == "HOSPITAL_ADMIN"
        or (staff.role.name and staff.role.name.lower() in ("hospital admin", "hospital_admin"))
    ):
        is_admin = True
    elif staff.assigned_roles:
        for ur in staff.assigned_roles:
            if (
                ur.role
                and (
                    getattr(ur.role, "is_admin_role", False)
                    or getattr(ur.role, "code", None) == "HOSPITAL_ADMIN"
                    or (ur.role.name and ur.role.name.lower() in ("hospital admin", "hospital_admin"))
                )
                and ur.role.is_active
            ):
                is_admin = True
                break

    effective_role = "hospital_admin" if is_admin else "hospital_staff"

    logger.info(
        "Hospital user login success for %s (%s / %s as %s)",
        email,
        hospital.hospital_id,
        session_info["primary_role"],
        effective_role,
    )
    token = create_access_token(
        {
            "sub": email,
            "role": effective_role,
            "name": staff.name,
            "staff_role_name": session_info["primary_role"],
            "roles": session_info["roles"],
            "primary_role": session_info["primary_role"],
            "hospital_uuid": str(hospital.id),
            "hospital_id": hospital.hospital_id,
            "user_id": str(staff.id),
            "staff_role_id": session_info["role_ids"][0] if session_info["role_ids"] else (str(staff.role_id) if staff.role_id else None),
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
            "role": effective_role,
            "staff_role_name": session_info["primary_role"],
        },
        action="login",
        entity_type="session",
        summary=f"{session_info['primary_role']} {staff.name} signed in",
    )
    db.commit()
    return LoginResponse(
        access_token=token,
        role=effective_role,
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


_build_staff_login_response = _build_user_login_response


def _build_admin_login_response(
    db: Session, hospital: Hospital, email: str
) -> LoginResponse:
    logger.info(
        "Hospital legacy admin login success for %s (%s)", email, hospital.hospital_id
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


@router.post("/login")
def login(
    payload: LoginRequest,
    db: Session = Depends(get_transitional_sync_session),
    settings: Settings = Depends(get_settings),
):
    """Authenticate super admin, hospital admin, or hospital staff member."""
    email = payload.email.strip().lower()
    _check_login_rate_limit(db, email)
    logger.info("Login attempt for %s", email)

    # 1. Super admin authentication
    if (
        email == settings.super_admin_email.lower()
        and payload.password == settings.super_admin_password
    ):
        logger.info("Super admin login success for %s", email)
        _clear_login_failures(db, email)
        token = create_access_token({"sub": email, "role": "super_admin"})
        return LoginResponse(access_token=token, role="super_admin", name="Super Admin")

    # 2. If hospital_id is explicitly supplied, qualify directly
    if payload.hospital_id:
        target_hosp = None
        try:
            target_uuid = UUID(payload.hospital_id)
            target_hosp = (
                db.query(Hospital)
                .filter(or_(Hospital.hospital_id == payload.hospital_id, Hospital.id == target_uuid))
                .first()
            )
        except (ValueError, AttributeError):
            target_hosp = (
                db.query(Hospital)
                .filter(Hospital.hospital_id == payload.hospital_id)
                .first()
            )

        if not target_hosp:
            _record_login_failure(db, email)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials. Hospital not found.",
            )

        if getattr(target_hosp, "status", None) and target_hosp.status != "active":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Hospital account is suspended or inactive.",
            )
        if not target_hosp.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Hospital account is inactive.",
            )

        # Check hospital user in target hospital (both active and inactive)
        staff = (
            db.query(HospitalUser)
            .options(joinedload(HospitalUser.role).selectinload(StaffRole.permissions))
            .filter(
                HospitalUser.hospital_id == target_hosp.id,
                HospitalUser.email.ilike(email),
            )
            .first()
        )
        if staff:
            if not staff.is_active:
                _record_login_failure(db, email)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User account is inactive.",
                )
            if verify_password(payload.password, staff.password_hash):
                _clear_login_failures(db, email)
                return _build_user_login_response(db, staff, target_hosp, email)

            _record_login_failure(db, email)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials. Please check your email and password.",
            )

        # Only allow legacy organization login if auth_model is legacy_organization AND password_hash exists
        auth_model = getattr(target_hosp, "auth_model", "user_accounts")
        if (
            auth_model == "legacy_organization"
            and target_hosp.password_hash
            and target_hosp.email
            and target_hosp.email.lower() == email
            and verify_password(payload.password, target_hosp.password_hash)
        ):
            _clear_login_failures(db, email)
            return _build_admin_login_response(db, target_hosp, email)

        _record_login_failure(db, email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials. Please check your email and password.",
        )

    # 3. Model B Candidate Verification: query accounts across all active hospitals
    all_staff_accounts = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role).selectinload(StaffRole.permissions))
        .filter(HospitalUser.email.ilike(email))
        .all()
    )
    # Track which hospitals have user records with this email
    hospitals_with_user_emails = {s.hospital_id for s in all_staff_accounts}

    # CRITICAL SECURITY RULE: Verify credentials BEFORE revealing any hospital membership.
    # An attacker providing a wrong password must receive generic 401 Unauthorized without
    # learning whether the email belongs to one or multiple hospitals.
    valid_candidates: list[tuple[Hospital, HospitalUser | None]] = []
    seen_hospital_ids: set[UUID] = set()

    for s in all_staff_accounts:
        if not s.is_active:
            # Deactivated user account MUST NOT authenticate and MUST NOT fall through to Hospital row
            continue
        h = db.query(Hospital).filter(Hospital.id == s.hospital_id).first()
        if h and h.is_active and getattr(h, "status", "active") == "active":
            if verify_password(payload.password, s.password_hash):
                valid_candidates.append((h, s))
                seen_hospital_ids.add(h.id)

    # Legacy hospital organization accounts:
    # A hospital row can ONLY authenticate if it has legacy_organization auth_model,
    # does NOT have a HospitalUser with this email, and has a valid password_hash
    admin_accounts = (
        db.query(Hospital)
        .filter(Hospital.email.ilike(email), Hospital.is_active.is_(True))
        .all()
    )
    for h in admin_accounts:
        if (
            h.id not in seen_hospital_ids
            and h.id not in hospitals_with_user_emails
            and getattr(h, "status", "active") == "active"
            and getattr(h, "auth_model", "user_accounts") == "legacy_organization"
            and h.password_hash
            and verify_password(payload.password, h.password_hash)
        ):
            valid_candidates.append((h, None))
            seen_hospital_ids.add(h.id)

    # If NO candidates matched password, reject immediately with generic failure
    if not valid_candidates:
        _record_login_failure(db, email)
        logger.warning("Login failed for %s (no valid matching credentials)", email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials. Please check your email and password.",
        )

    # If MULTIPLE valid memberships matched this password, return disambiguation
    if len(valid_candidates) > 1:
        _clear_login_failures(db, email)
        matching_list = [
            {
                "hospital_id": h.hospital_id,
                "hospital_uuid": str(h.id),
                "name": h.name,
                "hospital_name": h.name,
            }
            for h, _ in sorted(valid_candidates, key=lambda x: x[0].name)
        ]
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "Multiple hospital accounts found for this email. Please select your hospital.",
                "matching_hospitals": matching_list,
            },
        )

    # EXACTLY 1 valid membership matched: log in directly
    target_hosp, staff = valid_candidates[0]
    _clear_login_failures(db, email)
    if staff:
        return _build_user_login_response(db, staff, target_hosp, email)
    return _build_admin_login_response(db, target_hosp, email)


@router.post("/setup-password")
def setup_password(
    payload: SetupPasswordRequest,
    db: Session = Depends(get_transitional_sync_session),
):
    """Set password using single-use setup token."""
    raw_token = payload.token.strip()
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is required.",
        )
    if len(payload.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long.",
        )

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    record = (
        db.query(AccountSetupToken)
        .filter(AccountSetupToken.token_hash == token_hash)
        .first()
    )
    if not record or record.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is invalid or already used.",
        )

    # Token purpose separation: setup token cannot be used for reset, reset cannot be used for setup
    if getattr(record, "token_type", "setup") != "setup":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token type. This token cannot be used for account setup.",
        )

    now = datetime.now(timezone.utc)
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token has expired. Please request a new setup link.",
        )

    new_hash = hash_password(payload.password)
    if record.user_id:
        user = db.query(HospitalUser).filter(HospitalUser.id == record.user_id).first()
        if user:
            user.password_hash = new_hash
            user.token_version = (user.token_version or 1) + 1
            user.is_active = True
    elif record.hospital_id:
        # Legacy hospital row setup ONLY if user_id is None
        hosp = db.query(Hospital).filter(Hospital.id == record.hospital_id).first()
        if hosp and getattr(hosp, "auth_model", "user_accounts") == "legacy_organization":
            hosp.password_hash = new_hash

    record.used_at = now
    db.commit()

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": "Password setup completed successfully. You may now log in."},
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.post("/forgot-password")
def forgot_password(
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_transitional_sync_session),
    settings: Settings = Depends(get_settings),
):
    """Generate a password reset token for a registered account."""
    email = payload.email.strip().lower()
    target_hospital = None
    if payload.hospital_id:
        try:
            target_uuid = UUID(payload.hospital_id)
            target_hospital = (
                db.query(Hospital)
                .filter(or_(Hospital.hospital_id == payload.hospital_id, Hospital.id == target_uuid))
                .first()
            )
        except (ValueError, AttributeError):
            target_hospital = (
                db.query(Hospital)
                .filter(Hospital.hospital_id == payload.hospital_id)
                .first()
            )

    query = db.query(HospitalUser).filter(HospitalUser.email.ilike(email), HospitalUser.is_active.is_(True))
    if target_hospital:
        query = query.filter(HospitalUser.hospital_id == target_hospital.id)
    users = query.all()

    raw_token = None
    if len(users) == 1:
        user = users[0]
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires = datetime.now(timezone.utc) + timedelta(hours=2)
        token_obj = AccountSetupToken(
            token_hash=token_hash,
            user_id=user.id,
            hospital_id=user.hospital_id,
            email=email,
            token_type="reset",
            expires_at=expires,
        )
        db.add(token_obj)
        db.commit()
        logger.info("Password reset token generated for user %s in hospital %s", email, user.hospital_id)
    elif len(users) == 0 and target_hospital and getattr(target_hospital, "auth_model", "user_accounts") == "legacy_organization":
        if target_hospital.email and target_hospital.email.lower() == email and target_hospital.is_active:
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            expires = datetime.now(timezone.utc) + timedelta(hours=2)
            token_obj = AccountSetupToken(
                token_hash=token_hash,
                user_id=None,
                hospital_id=target_hospital.id,
                email=email,
                token_type="reset",
                expires_at=expires,
            )
            db.add(token_obj)
            db.commit()
            logger.info("Password reset token generated for legacy hospital %s", target_hospital.id)

    content: dict[str, Any] = {
        "message": "If an eligible account exists, password reset instructions will be sent when account recovery delivery is available."
    }
    if raw_token and settings.environment in ("development", "test", "local"):
        content["reset_token"] = raw_token
        content["reset_url"] = f"/reset-password#{raw_token}"

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=content,
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.post("/reset-password")
def reset_password(
    payload: ResetPasswordRequest,
    db: Session = Depends(get_transitional_sync_session),
):
    """Reset password using a single-use reset token."""
    raw_token = payload.token.strip()
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is required.",
        )
    if len(payload.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long.",
        )

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    record = (
        db.query(AccountSetupToken)
        .filter(AccountSetupToken.token_hash == token_hash)
        .first()
    )
    if not record or record.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is invalid or already used.",
        )

    # Token purpose separation: reset token cannot be used for setup, setup cannot be used for reset
    if getattr(record, "token_type", "reset") != "reset":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token type. This token cannot be used for password reset.",
        )

    now = datetime.now(timezone.utc)
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token has expired. Please request a new password reset link.",
        )

    new_hash = hash_password(payload.password)
    if record.user_id:
        user = db.query(HospitalUser).filter(HospitalUser.id == record.user_id).first()
        if user:
            user.password_hash = new_hash
            user.token_version = (user.token_version or 1) + 1
            user.is_active = True
    elif record.hospital_id:
        # Legacy hospital row reset ONLY if user_id is None
        hosp = db.query(Hospital).filter(Hospital.id == record.hospital_id).first()
        if hosp and getattr(hosp, "auth_model", "user_accounts") == "legacy_organization":
            hosp.password_hash = new_hash

    record.used_at = now
    db.commit()
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": "Password reset successfully. You may now log in with your new password."},
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store, no-cache, must-revalidate"},
    )


