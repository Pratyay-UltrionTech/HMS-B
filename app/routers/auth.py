import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from fastapi import Depends

from app.config import get_settings
from app.database import get_db
from app.models import Hospital, HospitalUser, StaffRole
from app.schemas import LoginRequest, LoginResponse
from app.utils.auth import create_access_token
from app.utils.password import verify_password
from app.utils.audit import write_audit

logger = logging.getLogger("hms.api")

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    logger.info("Login attempt for %s", email)

    if email == settings.super_admin_email.lower() and payload.password == settings.super_admin_password:
        logger.info("Super admin login success for %s", email)
        token = create_access_token({"sub": email, "role": "super_admin"})
        return LoginResponse(access_token=token, role="super_admin", name="Super Admin")

    hospital = db.query(Hospital).filter(Hospital.email == email, Hospital.is_active.is_(True)).first()
    if hospital and verify_password(payload.password, hospital.password_hash):
        logger.info("Hospital admin login success for %s (%s)", email, hospital.hospital_id)
        token = create_access_token(
            {
                "sub": email,
                "role": "hospital_admin",
                "name": hospital.name,
                "hospital_uuid": str(hospital.id),
                "hospital_id": hospital.hospital_id,
            }
        )
        write_audit(
            db,
            hospital_id=hospital.id,
            actor={"sub": email, "name": hospital.name, "role": "hospital_admin", "staff_role_name": "Hospital Admin"},
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

    staff = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role).joinedload(StaffRole.permissions))
        .filter(HospitalUser.email == email, HospitalUser.is_active.is_(True))
        .first()
    )
    if staff and verify_password(payload.password, staff.password_hash):
        hospital = db.query(Hospital).filter(Hospital.id == staff.hospital_id, Hospital.is_active.is_(True)).first()
        if not hospital:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Hospital account is inactive.")
        if not staff.role or not staff.role.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Your role is inactive. Contact admin.")

        permissions = [
            {"module_key": p.module_key, "can_view": p.can_view, "can_edit": p.can_edit}
            for p in staff.role.permissions
            if p.can_view or p.can_edit
        ]
        logger.info("Hospital staff login success for %s (%s / %s)", email, hospital.hospital_id, staff.role.name)
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
        write_audit(
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
