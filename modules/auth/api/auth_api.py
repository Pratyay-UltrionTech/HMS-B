"""Authentication API router conforming to UltrionTech-Backend-Template modules/auth/api/ specification."""

from __future__ import annotations

import logging

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from config.settings import Settings, get_settings
from infrastructure.postgres.session import get_transitional_sync_session
from modules.auth.contracts.auth_contracts import LoginRequest, LoginResponse, MeResponse
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.tenancy.entities.hospital import Hospital
from shared.audit.service import write_audit_log
from shared.auth.dependencies import get_current_user
from shared.auth.jwt import create_access_token
from shared.auth.security import verify_password

logger = logging.getLogger("hms.api")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=MeResponse)
def me(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_transitional_sync_session),
) -> MeResponse:
    """Return server-authoritative session state for the current token holder.

    FLAW-024: This endpoint is called on frontend boot so that a deactivated or
    role-revoked user's stale localStorage session is rejected immediately rather
    than lingering until the JWT expires. It returns the authoritative
    `is_active` and `token_version` from the database.
    """
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
    if not staff.role or not staff.role.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your role is inactive. Contact admin.",
        )

    permissions = [
        {"module_key": p.module_key, "can_view": p.can_view, "can_edit": p.can_edit}
        for p in staff.role.permissions
        if p.can_view or p.can_edit
    ]
    hospital = (
        db.query(Hospital).filter(Hospital.id == staff.hospital_id).first()
    )
    return MeResponse(
        sub=str(user.get("sub") or ""),
        role="hospital_staff",
        name=staff.name,
        hospital_id=hospital.hospital_id if hospital else None,
        hospital_uuid=str(staff.hospital_id),
        staff_role_name=staff.role.name,
        user_id=str(staff.id),
        is_active=True,
        token_version=int(getattr(staff, "token_version", 1) or 1),
        permissions=permissions,
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

    # 1. Super admin authentication
    if (
        email == settings.super_admin_email.lower()
        and payload.password == settings.super_admin_password
    ):
        logger.info("Super admin login success for %s", email)
        token = create_access_token({"sub": email, "role": "super_admin"})
        return LoginResponse(access_token=token, role="super_admin", name="Super Admin")

    # 2. Hospital admin authentication
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

    # 3. Hospital staff authentication
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
        if not staff.role or not staff.role.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Your role is inactive. Contact admin.",
            )

        permissions = [
            {"module_key": p.module_key, "can_view": p.can_view, "can_edit": p.can_edit}
            for p in staff.role.permissions
            if p.can_view or p.can_edit
        ]
        logger.info(
            "Hospital staff login success for %s (%s / %s)",
            email,
            hospital.hospital_id,
            staff.role.name,
        )
        token = create_access_token(
            {
                "sub": email,
                "role": "hospital_staff",
                "name": staff.name,
                "staff_role_name": staff.role.name,
                "hospital_uuid": str(hospital.id),
                "hospital_id": hospital.hospital_id,
                "user_id": str(staff.id),
                "staff_role_id": str(staff.role_id),
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
                "staff_role_name": staff.role.name,
            },
            action="login",
            entity_type="session",
            summary=f"{staff.role.name} {staff.name} signed in",
        )
        db.commit()
        return LoginResponse(
            access_token=token,
            role="hospital_staff",
            hospital_id=hospital.hospital_id,
            name=staff.name,
            plan=hospital.plan,
            staff_role_name=staff.role.name,
            permissions=permissions,
            user_id=str(staff.id),
            token_version=int(getattr(staff, "token_version", 1) or 1),
        )

    logger.warning("Login failed for %s", email)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials. Please check your email and password.",
    )
