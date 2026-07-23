from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Admission,
    AdmissionStatus,
    Appointment,
    AppointmentStatus,
    Bed,
    HospitalUser,
    MedicalRecord,
    Patient,
    PatientStatus,
    Prescription,
    Room,
    Ward,
)
from app.schemas_registration import (
    AdmitPatientRequest,
    AdmissionSummary,
    BedOption,
    DischargeResponse,
    PatientDirectoryItem,
    PatientProfile,
    PatientRegister,
    PatientRegisterUpdate,
    PrescriptionSummary,
    ReportSummary,
    VisitSummary,
    validate_emergency_contact_bundle,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user
from app.utils.billing import build_ledger_entries, patient_ledger_totals

router = APIRouter(prefix="/registration", tags=["registration"])


def _display_name(first: str, last: str) -> str:
    return f"{first.strip()} {last.strip()}".strip()


def _next_uhid(db: Session, hospital_id: UUID) -> str:
    count = db.query(func.count(Patient.id)).filter(Patient.hospital_id == hospital_id).scalar() or 0
    # Keep generating until unique (handles gaps / concurrent inserts lightly)
    for i in range(1, 100_000):
        uhid = f"P{count + i:04d}"
        exists = db.query(Patient.id).filter(Patient.hospital_id == hospital_id, Patient.uhid == uhid).first()
        if not exists:
            return uhid
    raise HTTPException(status_code=500, detail="Unable to generate UHID")


def _last_visit(db: Session, patient_id: UUID) -> date | None:
    row = (
        db.query(Appointment.appointment_date)
        .filter(
            Appointment.patient_id == patient_id,
            Appointment.status != AppointmentStatus.cancelled,
        )
        .order_by(Appointment.appointment_date.desc())
        .first()
    )
    return row[0] if row else None


def _age_from_dob(dob: date | None) -> int | None:
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _to_directory(db: Session, p: Patient) -> PatientDirectoryItem:
    return PatientDirectoryItem(
        id=p.id,
        uhid=p.uhid,
        name=p.name,
        first_name=p.first_name or "",
        last_name=p.last_name or "",
        mobile=p.mobile,
        email=p.email,
        gender=p.gender,
        age=p.age,
        date_of_birth=p.date_of_birth,
        blood_group=p.blood_group,
        status=p.status,
        last_visit=_last_visit(db, p.id),
        created_at=p.created_at,
    )


def _ensure_beds_for_room(db: Session, hospital_id: UUID, room: Room) -> None:
    """Auto-create bed rows from room.bed_count if missing."""
    existing = db.query(func.count(Bed.id)).filter(Bed.room_id == room.id, Bed.hospital_id == hospital_id).scalar() or 0
    if existing >= room.bed_count:
        return
    for i in range(existing + 1, room.bed_count + 1):
        db.add(
            Bed(
                hospital_id=hospital_id,
                ward_id=room.ward_id,
                room_id=room.id,
                bed_code=f"B{i}",
                is_occupied=False,
                is_active=True,
            )
        )
    db.flush()


@router.post("/patients", response_model=PatientDirectoryItem, status_code=status.HTTP_201_CREATED)
def register_patient(
    payload: PatientRegister,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    mobile = payload.mobile.strip()
    if db.query(Patient.id).filter(Patient.hospital_id == hospital_id, Patient.mobile == mobile).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Patient with this mobile already exists")

    first = payload.first_name.strip()
    last = payload.last_name.strip()
    age = payload.age if payload.age is not None else _age_from_dob(payload.date_of_birth)
    uhid = _next_uhid(db, hospital_id)

    patient = Patient(
        hospital_id=hospital_id,
        uhid=uhid,
        first_name=first,
        last_name=last,
        name=_display_name(first, last),
        mobile=mobile,
        email=str(payload.email).lower() if payload.email else None,
        age=age,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender.strip(),
        address=payload.address.strip() if payload.address else None,
        emergency_contact=payload.emergency_contact.strip() if payload.emergency_contact else None,
        emergency_contact_name=payload.emergency_contact_name,
        emergency_contact_relation=payload.emergency_contact_relation,
        blood_group=payload.blood_group,
        has_insurance=bool(payload.has_insurance),
        insurance_provider=payload.insurance_provider if payload.has_insurance else None,
        insurance_details=None,
        status=PatientStatus.active,
    )
    db.add(patient)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="patient",
        entity_id=patient.id,
        summary=f"Registered patient {patient.uhid} {patient.name}",
    )
    db.commit()
    db.refresh(patient)
    return _to_directory(db, patient)


@router.get("/patients", response_model=list[PatientDirectoryItem])
def list_patients(
    search: str | None = Query(default=None),
    status_filter: PatientStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(Patient).filter(Patient.hospital_id == hospital_id)
    if status_filter:
        q = q.filter(Patient.status == status_filter)
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(
            or_(
                Patient.name.ilike(term),
                Patient.uhid.ilike(term),
                Patient.mobile.ilike(term),
                Patient.first_name.ilike(term),
                Patient.last_name.ilike(term),
            )
        )
    rows = q.order_by(Patient.created_at.desc()).all()
    return [_to_directory(db, p) for p in rows]


@router.get("/patients/{patient_id}", response_model=PatientProfile)
def get_patient_profile(
    patient_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    visits = (
        db.query(Appointment)
        .options(joinedload(Appointment.doctor))
        .filter(Appointment.patient_id == patient_id, Appointment.hospital_id == hospital_id)
        .order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
        .all()
    )
    prescriptions = (
        db.query(Prescription)
        .options(joinedload(Prescription.doctor))
        .filter(Prescription.patient_id == patient_id, Prescription.hospital_id == hospital_id)
        .order_by(Prescription.created_at.desc())
        .all()
    )
    reports = (
        db.query(MedicalRecord)
        .options(joinedload(MedicalRecord.doctor))
        .filter(MedicalRecord.patient_id == patient_id, MedicalRecord.hospital_id == hospital_id)
        .order_by(MedicalRecord.created_at.desc())
        .all()
    )
    admissions = (
        db.query(Admission)
        .options(
            joinedload(Admission.ward),
            joinedload(Admission.room),
            joinedload(Admission.bed),
            joinedload(Admission.doctor),
        )
        .filter(Admission.patient_id == patient_id, Admission.hospital_id == hospital_id)
        .order_by(Admission.admitted_at.desc())
        .all()
    )

    return PatientProfile(
        id=patient.id,
        uhid=patient.uhid,
        first_name=patient.first_name or "",
        last_name=patient.last_name or "",
        name=patient.name,
        mobile=patient.mobile,
        email=patient.email,
        gender=patient.gender,
        age=patient.age,
        date_of_birth=patient.date_of_birth,
        address=patient.address,
        emergency_contact=patient.emergency_contact,
        emergency_contact_name=getattr(patient, "emergency_contact_name", None),
        emergency_contact_relation=getattr(patient, "emergency_contact_relation", None),
        blood_group=patient.blood_group,
        has_insurance=bool(getattr(patient, "has_insurance", False)),
        insurance_provider=getattr(patient, "insurance_provider", None),
        insurance_details=getattr(patient, "insurance_details", None),
        status=patient.status,
        created_at=patient.created_at,
        visits=[
            VisitSummary(
                id=v.id,
                appointment_date=v.appointment_date,
                appointment_time=v.appointment_time.strftime("%H:%M"),
                doctor_name=v.doctor.name if v.doctor else None,
                purpose=v.purpose,
                visit_type=getattr(v, "visit_type", None) or "OPD",
                status=v.status.value if hasattr(v.status, "value") else str(v.status),
            )
            for v in visits
        ],
        prescriptions=[
            PrescriptionSummary(
                id=rx.id,
                diagnosis=rx.diagnosis,
                medicines=rx.medicines,
                doctor_name=rx.doctor.name if rx.doctor else None,
                created_at=rx.created_at,
            )
            for rx in prescriptions
        ],
        medical_reports=[
            ReportSummary(
                id=r.id,
                report_type=r.report_type,
                title=r.title,
                notes=r.notes,
                created_at=r.created_at,
                doctor_name=r.doctor.name if r.doctor else None,
            )
            for r in reports
        ],
        admissions=[
            AdmissionSummary(
                id=a.id,
                ward_id=a.ward_id,
                room_id=a.room_id,
                bed_id=a.bed_id,
                ward_name=a.ward.name if a.ward else None,
                room_code=a.room.room_code if a.room else None,
                bed_code=a.bed.bed_code if a.bed else None,
                doctor_name=a.doctor.name if a.doctor else None,
                status=a.status,
                admitted_at=a.admitted_at,
                discharged_at=a.discharged_at,
                notes=a.notes,
            )
            for a in admissions
        ],
        bills=[
            {
                "id": str(e["ref_id"]),
                "type": e["entry_type"],
                "description": e["description"],
                "debit": e["debit"],
                "credit": e["credit"],
                "status": e["status"],
                "occurred_at": e["occurred_at"].isoformat() if e.get("occurred_at") else None,
            }
            for e in build_ledger_entries(db, hospital_id, patient_id)[:50]
        ],
        financial_summary=patient_ledger_totals(db, hospital_id, patient_id),
    )


@router.put("/patients/{patient_id}", response_model=PatientDirectoryItem)
def update_patient(
    patient_id: UUID,
    payload: PatientRegisterUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    data = payload.model_dump(exclude_unset=True)
    if "mobile" in data and data["mobile"]:
        mobile = data["mobile"].strip()
        clash = (
            db.query(Patient.id)
            .filter(Patient.hospital_id == hospital_id, Patient.mobile == mobile, Patient.id != patient_id)
            .first()
        )
        if clash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Mobile already used by another patient")
        patient.mobile = mobile
    if "first_name" in data and data["first_name"]:
        patient.first_name = data["first_name"].strip()
    if "last_name" in data and data["last_name"]:
        patient.last_name = data["last_name"].strip()
    if "first_name" in data or "last_name" in data:
        patient.name = _display_name(patient.first_name or "", patient.last_name or "")
    if "gender" in data and data["gender"]:
        patient.gender = data["gender"].strip()
    if "date_of_birth" in data:
        patient.date_of_birth = data["date_of_birth"]
        if data.get("age") is None and data["date_of_birth"]:
            patient.age = _age_from_dob(data["date_of_birth"])
    if "age" in data and data["age"] is not None:
        patient.age = data["age"]
    if "email" in data:
        patient.email = str(data["email"]).lower() if data["email"] else None
    if "address" in data:
        patient.address = data["address"].strip() if data["address"] else None
    if "emergency_contact" in data:
        patient.emergency_contact = data["emergency_contact"].strip() if data["emergency_contact"] else None
    if "emergency_contact_name" in data:
        patient.emergency_contact_name = data["emergency_contact_name"]
    if "emergency_contact_relation" in data:
        patient.emergency_contact_relation = data["emergency_contact_relation"]
    if "blood_group" in data:
        patient.blood_group = data["blood_group"]
    if "has_insurance" in data and data["has_insurance"] is not None:
        patient.has_insurance = bool(data["has_insurance"])
        if not patient.has_insurance:
            patient.insurance_provider = None
    if "insurance_provider" in data:
        if getattr(patient, "has_insurance", False):
            patient.insurance_provider = data["insurance_provider"]
        else:
            patient.insurance_provider = None
    if "status" in data and data["status"] is not None:
        patient.status = data["status"]

    try:
        validate_emergency_contact_bundle(
            name=patient.emergency_contact_name,
            relation=patient.emergency_contact_relation,
            phone=patient.emergency_contact,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="patient",
        entity_id=patient.id,
        summary=f"Updated patient {patient.uhid} {patient.name}",
    )
    db.commit()
    db.refresh(patient)
    return _to_directory(db, patient)


@router.get("/beds", response_model=list[BedOption])
def list_available_beds(
    ward_id: UUID | None = Query(default=None),
    room_id: UUID | None = Query(default=None),
    available_only: bool = Query(default=True),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    # Sync beds from room bed_count
    rooms_q = db.query(Room).filter(Room.hospital_id == hospital_id, Room.is_active.is_(True))
    if ward_id:
        rooms_q = rooms_q.filter(Room.ward_id == ward_id)
    if room_id:
        rooms_q = rooms_q.filter(Room.id == room_id)
    for room in rooms_q.all():
        _ensure_beds_for_room(db, hospital_id, room)
    db.commit()

    q = (
        db.query(Bed)
        .options(joinedload(Bed.ward), joinedload(Bed.room))
        .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
    )
    if ward_id:
        q = q.filter(Bed.ward_id == ward_id)
    if room_id:
        q = q.filter(Bed.room_id == room_id)
    if available_only:
        q = q.filter(Bed.is_occupied.is_(False))
    beds = q.order_by(Bed.bed_code.asc()).all()
    return [
        BedOption(
            id=b.id,
            bed_code=b.bed_code,
            room_id=b.room_id,
            room_code=b.room.room_code if b.room else None,
            ward_id=b.ward_id,
            ward_name=b.ward.name if b.ward else None,
            is_occupied=b.is_occupied,
        )
        for b in beds
    ]


@router.get("/wards-rooms")
def list_wards_rooms(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    wards = (
        db.query(Ward)
        .filter(Ward.hospital_id == hospital_id, Ward.is_active.is_(True))
        .order_by(Ward.name.asc())
        .all()
    )
    rooms = (
        db.query(Room)
        .filter(Room.hospital_id == hospital_id, Room.is_active.is_(True))
        .order_by(Room.room_code.asc())
        .all()
    )
    return {
        "wards": [{"id": str(w.id), "name": w.name, "ward_type": w.ward_type.value} for w in wards],
        "rooms": [
            {"id": str(r.id), "ward_id": str(r.ward_id), "room_code": r.room_code, "name": r.name, "bed_count": r.bed_count}
            for r in rooms
        ],
    }


@router.post("/patients/{patient_id}/admit", response_model=AdmissionSummary, status_code=status.HTTP_201_CREATED)
def admit_patient(
    patient_id: UUID,
    payload: AdmitPatientRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    active = (
        db.query(Admission)
        .filter(
            Admission.patient_id == patient_id,
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.admitted,
        )
        .first()
    )
    if active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Patient is already admitted")

    bed = (
        db.query(Bed)
        .options(joinedload(Bed.ward), joinedload(Bed.room))
        .filter(Bed.id == payload.bed_id, Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
        .first()
    )
    if not bed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bed not found")
    if bed.is_occupied:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bed is already occupied")
    if bed.ward_id != payload.ward_id or bed.room_id != payload.room_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ward/Room does not match selected bed")

    doctor = None
    if payload.doctor_id:
        doctor = (
            db.query(HospitalUser)
            .filter(HospitalUser.id == payload.doctor_id, HospitalUser.hospital_id == hospital_id)
            .first()
        )
        if not doctor:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

    admission = Admission(
        hospital_id=hospital_id,
        patient_id=patient_id,
        ward_id=payload.ward_id,
        room_id=payload.room_id,
        bed_id=payload.bed_id,
        doctor_id=payload.doctor_id,
        status=AdmissionStatus.admitted,
        notes=payload.notes.strip() if payload.notes else None,
    )
    bed.is_occupied = True
    patient.status = PatientStatus.admitted
    db.add(admission)
    db.flush()

    from app.utils.billing import ensure_admission_charge

    ensure_admission_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient_id,
        admission_id=admission.id,
        ward_name=bed.ward.name if bed.ward else None,
        admission_fee=float(getattr(bed.ward, "admission_fee", 0) or 0) if bed.ward else 0.0,
        created_by_name=user.get("name") or "System",
    )

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="admission",
        entity_id=admission.id,
        summary=f"Admitted {patient.uhid} {patient.name} to {bed.ward.name if bed.ward else ''} / {bed.room.room_code if bed.room else ''} / {bed.bed_code}",
    )
    db.commit()
    admission = (
        db.query(Admission)
        .options(
            joinedload(Admission.ward),
            joinedload(Admission.room),
            joinedload(Admission.bed),
            joinedload(Admission.doctor),
        )
        .filter(Admission.id == admission.id)
        .first()
    )
    return AdmissionSummary(
        id=admission.id,
        ward_id=admission.ward_id,
        room_id=admission.room_id,
        bed_id=admission.bed_id,
        ward_name=admission.ward.name if admission.ward else None,
        room_code=admission.room.room_code if admission.room else None,
        bed_code=admission.bed.bed_code if admission.bed else None,
        doctor_name=admission.doctor.name if admission.doctor else None,
        status=admission.status,
        admitted_at=admission.admitted_at,
        discharged_at=admission.discharged_at,
        notes=admission.notes,
    )


@router.post("/admissions/{admission_id}/discharge", response_model=DischargeResponse)
def discharge_patient(
    admission_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    admission = (
        db.query(Admission)
        .options(
            joinedload(Admission.bed),
            joinedload(Admission.patient),
            joinedload(Admission.ward),
            joinedload(Admission.room),
        )
        .filter(Admission.id == admission_id, Admission.hospital_id == hospital_id)
        .first()
    )
    if not admission:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admission not found")
    if admission.status != AdmissionStatus.admitted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Admission already discharged")

    admission.status = AdmissionStatus.discharged
    admission.discharged_at = datetime.now(timezone.utc)
    if admission.bed:
        admission.bed.is_occupied = False
    if admission.patient:
        admission.patient.status = PatientStatus.active

    from app.utils.billing import ensure_bed_charge_for_admission

    ward = admission.ward
    ensure_bed_charge_for_admission(
        db,
        hospital_id=hospital_id,
        patient_id=admission.patient_id,
        admission_id=admission.id,
        admitted_at=admission.admitted_at,
        discharged_at=admission.discharged_at,
        ward_name=ward.name if ward else None,
        room_code=admission.room.room_code if admission.room else None,
        bed_code=admission.bed.bed_code if admission.bed else None,
        bed_charge_per_day=float(getattr(ward, "bed_charge_per_day", 0) or 0) if ward else 0.0,
        created_by_name=user.get("name") or "System",
    )

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="admission",
        entity_id=admission.id,
        summary=f"Discharged patient admission {admission_id}",
    )
    db.commit()
    return DischargeResponse(id=admission.id, status=admission.status, discharged_at=admission.discharged_at)


@router.get("/doctors")
def list_doctors_for_registration(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    users = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .all()
    )
    doctors = [u for u in users if u.role and "doctor" in (u.role.name or "").lower()]
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "phone": d.phone,
            "email": d.email,
            "specialization": d.specialization,
            "qualification": d.qualification,
            "medical_registration_number": d.medical_registration_number,
            "years_of_experience": d.years_of_experience,
            "consultation_room": d.consultation_room,
        }
        for d in doctors
    ]
