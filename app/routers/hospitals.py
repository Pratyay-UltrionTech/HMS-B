from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Admission,
    AdmissionStatus,
    Appointment,
    Bed,
    Hospital,
    HospitalUser,
    Patient,
)
from app.schemas import HospitalCreate, HospitalCreateResponse, HospitalDashboardResponse, HospitalResponse
from app.schemas_admin import BASIC_MODULE_KEYS
from app.utils.auth import get_hospital_context, require_hospital_user, require_super_admin
from app.utils.hospital_id import generate_hospital_id
from app.utils.password import generate_temp_password, hash_password

router = APIRouter(prefix="/hospitals", tags=["hospitals"])


def _is_doctor_role(name: str | None) -> bool:
    return bool(name and "doctor" in name.lower())


@router.post("", response_model=HospitalCreateResponse, status_code=status.HTTP_201_CREATED)
def create_hospital(
    payload: HospitalCreate,
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin),
):
    email = payload.email.strip().lower()
    existing = db.query(Hospital).filter(Hospital.email == email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A hospital with this email already exists.",
        )

    generated_password = generate_temp_password(5)
    hospital = Hospital(
        hospital_id=generate_hospital_id(db),
        name=payload.name.strip(),
        address=payload.address.strip(),
        phone=payload.phone.strip(),
        email=email,
        password_hash=hash_password(generated_password),
        plan=payload.plan,
        icon_url=payload.icon_url,
    )
    db.add(hospital)
    db.commit()
    db.refresh(hospital)

    return HospitalCreateResponse(
        **HospitalResponse.model_validate(hospital).model_dump(),
        generated_password=generated_password,
    )


@router.get("", response_model=list[HospitalResponse])
def list_hospitals(
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin),
):
    query = db.query(Hospital).order_by(Hospital.created_at.desc())
    if search:
        term = f"%{search.strip().lower()}%"
        query = query.filter(
            (Hospital.name.ilike(term)) | (Hospital.email.ilike(term)) | (Hospital.hospital_id.ilike(term))
        )
    return query.all()


@router.get("/me/dashboard", response_model=HospitalDashboardResponse)
def hospital_dashboard(
    db: Session = Depends(get_db),
    hospital_id=Depends(get_hospital_context),
    _: dict = Depends(require_hospital_user),
):
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    if not hospital:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")

    staff_users = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .all()
    )
    staff_count = len(staff_users)
    doctor_count = sum(1 for u in staff_users if _is_doctor_role(u.role.name if u.role else None))

    patient_count = int(
        db.query(func.count(Patient.id)).filter(Patient.hospital_id == hospital_id).scalar() or 0
    )
    today = date.today()
    appointments_today = int(
        db.query(func.count(Appointment.id))
        .filter(Appointment.hospital_id == hospital_id, Appointment.appointment_date == today)
        .scalar()
        or 0
    )
    active_admissions = int(
        db.query(func.count(Admission.id))
        .filter(
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.admitted,
        )
        .scalar()
        or 0
    )
    beds_total = int(
        db.query(func.count(Bed.id))
        .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
        .scalar()
        or 0
    )
    beds_occupied = int(
        db.query(func.count(Bed.id))
        .filter(
            Bed.hospital_id == hospital_id,
            Bed.is_active.is_(True),
            Bed.is_occupied.is_(True),
        )
        .scalar()
        or 0
    )

    return HospitalDashboardResponse(
        id=hospital.id,
        hospital_id=hospital.hospital_id,
        name=hospital.name,
        address=hospital.address,
        phone=hospital.phone,
        email=hospital.email,
        plan=hospital.plan,
        icon_url=hospital.icon_url,
        is_active=hospital.is_active,
        created_at=hospital.created_at,
        staff_count=staff_count,
        doctor_count=doctor_count,
        patient_count=patient_count,
        appointments_today=appointments_today,
        active_admissions=active_admissions,
        beds_total=beds_total,
        beds_occupied=beds_occupied,
        modules_available=len(BASIC_MODULE_KEYS),
    )


@router.get("/{hospital_uuid}", response_model=HospitalResponse)
def get_hospital(
    hospital_uuid: str,
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin),
):
    hospital = db.query(Hospital).filter(Hospital.id == hospital_uuid).first()
    if not hospital:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")
    return hospital


@router.delete("/{hospital_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_hospital(
    hospital_uuid: str,
    db: Session = Depends(get_db),
    _: dict = Depends(require_super_admin),
):
    hospital = db.query(Hospital).filter(Hospital.id == hospital_uuid).first()
    if not hospital:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")
    db.delete(hospital)
    db.commit()
    return None
