"""Authentication API router conforming to UltrionTech-Backend-Template modules/auth/api/ specification."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from hms_migration.config.settings import Settings, get_settings
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.auth.contracts.auth_contracts import LoginRequest, LoginResponse
from hms_migration.modules.doctors.entities.doctor import HospitalUser, StaffRole
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.audit.service import write_audit_log
from hms_migration.shared.auth.jwt import create_access_token
from hms_migration.shared.auth.security import verify_password

logger = logging.getLogger("hms.api")

router = APIRouter(prefix="/auth", tags=["auth"])


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
        )

    logger.warning("Login failed for %s", email)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials. Please check your email and password.",
    )
