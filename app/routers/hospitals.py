from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Hospital
from app.schemas import HospitalCreate, HospitalCreateResponse, HospitalResponse
from app.utils.auth import require_super_admin
from app.utils.hospital_id import generate_hospital_id
from app.utils.password import generate_temp_password, hash_password

router = APIRouter(prefix="/hospitals", tags=["hospitals"])


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
