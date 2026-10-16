"""Canonical tenant-scoped eligibility for active primary or secondary doctors."""
from datetime import datetime, timezone
from sqlalchemy import and_, or_
from modules.doctors.entities.doctor import HospitalUser, HospitalUserRole, StaffRole


def doctor_eligibility_clause(hospital_id):
    role = and_(StaffRole.hospital_id == hospital_id,
                StaffRole.is_active.is_(True), StaffRole.name.ilike("%doctor%"))
    return and_(
        HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True),
        or_(HospitalUser.role.has(role), HospitalUser.assigned_roles.any(and_(
            HospitalUserRole.hospital_id == hospital_id,
            or_(HospitalUserRole.expires_at.is_(None),
                HospitalUserRole.expires_at > datetime.now(timezone.utc)),
            HospitalUserRole.role.has(role),
        ))),
    )


def is_eligible_doctor(db, hospital_id, user_id):
    return db.query(HospitalUser.id).filter(
        HospitalUser.id == user_id, doctor_eligibility_clause(hospital_id)
    ).first() is not None
