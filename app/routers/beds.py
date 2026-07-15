from datetime import date, datetime, time, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Admission,
    AdmissionStatus,
    Bed,
    HospitalUser,
    Patient,
    PatientStatus,
    Room,
    Ward,
)
from app.schemas_beds import (
    AdmitRequest,
    AdmissionDetail,
    AllocateRequest,
    BedDashboardRow,
    BedOption,
    DischargeRequest,
    OccupancyReport,
    RoomOption,
    TransferRequest,
    WardRoomOption,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/beds", tags=["beds"])


def _ensure_beds_for_room(db: Session, hospital_id: UUID, room: Room) -> None:
    existing = db.query(func.count(Bed.id)).filter(Bed.room_id == room.id, Bed.hospital_id == hospital_id).scalar() or 0
    if existing >= room.bed_count:
        return
    for i in range(existing + 1, room.bed_count + 1):
        db.add(
            Bed(
                hospital_id=hospital_id,
                ward_id=room.ward_id,
                room_id=room.id,
                bed_code=f"Bed-{i}",
                is_occupied=False,
                is_active=True,
            )
        )
    db.flush()


def _sync_all_beds(db: Session, hospital_id: UUID) -> None:
    rooms = db.query(Room).filter(Room.hospital_id == hospital_id, Room.is_active.is_(True)).all()
    for room in rooms:
        _ensure_beds_for_room(db, hospital_id, room)
    db.flush()


def _admission_detail(a: Admission) -> AdmissionDetail:
    return AdmissionDetail(
        id=a.id,
        patient_id=a.patient_id,
        patient_name=a.patient.name if a.patient else None,
        patient_uhid=getattr(a.patient, "uhid", None) if a.patient else None,
        patient_mobile=a.patient.mobile if a.patient else None,
        ward_id=a.ward_id,
        room_id=a.room_id,
        bed_id=a.bed_id,
        ward_name=a.ward.name if a.ward else None,
        room_code=a.room.room_code if a.room else None,
        bed_code=a.bed.bed_code if a.bed else None,
        doctor_id=a.doctor_id,
        doctor_name=a.doctor.name if a.doctor else None,
        status=a.status,
        notes=a.notes,
        discharge_notes=getattr(a, "discharge_notes", None),
        admitted_at=a.admitted_at,
        discharged_at=a.discharged_at,
    )


def _load_admission(db: Session, hospital_id: UUID, admission_id: UUID | None, patient_id: UUID | None) -> Admission:
    q = (
        db.query(Admission)
        .options(
            joinedload(Admission.patient),
            joinedload(Admission.ward),
            joinedload(Admission.room),
            joinedload(Admission.bed),
            joinedload(Admission.doctor),
        )
        .filter(Admission.hospital_id == hospital_id, Admission.status == AdmissionStatus.admitted)
    )
    if admission_id:
        a = q.filter(Admission.id == admission_id).first()
    elif patient_id:
        a = q.filter(Admission.patient_id == patient_id).first()
    else:
        raise HTTPException(status_code=400, detail="admission_id or patient_id is required")
    if not a:
        raise HTTPException(status_code=404, detail="Active admission not found")
    return a


def _get_free_bed(db: Session, hospital_id: UUID, ward_id: UUID, room_id: UUID, bed_id: UUID) -> Bed:
    bed = (
        db.query(Bed)
        .options(joinedload(Bed.ward), joinedload(Bed.room))
        .filter(Bed.id == bed_id, Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
        .first()
    )
    if not bed:
        raise HTTPException(status_code=404, detail="Bed not found")
    if bed.ward_id != ward_id or bed.room_id != room_id:
        raise HTTPException(status_code=400, detail="Ward/Room does not match selected bed")
    if bed.is_occupied:
        raise HTTPException(status_code=409, detail="Bed is already occupied")
    return bed


def _is_doctor(user: HospitalUser) -> bool:
    return bool(user.role and "doctor" in (user.role.name or "").lower())


@router.get("/dashboard", response_model=list[BedDashboardRow])
def bed_dashboard(
    ward_id: UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),  # available | occupied
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    _sync_all_beds(db, hospital_id)
    db.commit()

    beds = (
        db.query(Bed)
        .options(joinedload(Bed.ward), joinedload(Bed.room))
        .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
    )
    if ward_id:
        beds = beds.filter(Bed.ward_id == ward_id)
    if status_filter == "available":
        beds = beds.filter(Bed.is_occupied.is_(False))
    elif status_filter == "occupied":
        beds = beds.filter(Bed.is_occupied.is_(True))
    beds = beds.order_by(Bed.ward_id.asc(), Bed.room_id.asc(), Bed.bed_code.asc()).all()

    active = (
        db.query(Admission)
        .options(joinedload(Admission.patient), joinedload(Admission.doctor))
        .filter(Admission.hospital_id == hospital_id, Admission.status == AdmissionStatus.admitted)
        .all()
    )
    by_bed = {a.bed_id: a for a in active}

    rows: list[BedDashboardRow] = []
    for b in beds:
        a = by_bed.get(b.id)
        occupied = bool(b.is_occupied or a)
        rows.append(
            BedDashboardRow(
                bed_id=b.id,
                ward_id=b.ward_id,
                room_id=b.room_id,
                ward_name=b.ward.name if b.ward else None,
                room_code=b.room.room_code if b.room else None,
                bed_code=b.bed_code,
                status="Occupied" if occupied else "Available",
                is_occupied=occupied,
                patient_id=a.patient_id if a else None,
                patient_name=a.patient.name if a and a.patient else None,
                patient_uhid=a.patient.uhid if a and a.patient else None,
                admission_id=a.id if a else None,
                doctor_name=a.doctor.name if a and a.doctor else None,
            )
        )
    return rows


@router.get("/occupancy", response_model=OccupancyReport)
def occupancy_report(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    _sync_all_beds(db, hospital_id)
    db.commit()

    total = db.query(func.count(Bed.id)).filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True)).scalar() or 0
    occupied = (
        db.query(func.count(Bed.id))
        .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True), Bed.is_occupied.is_(True))
        .scalar()
        or 0
    )
    available = int(total) - int(occupied)
    pct = round((occupied / total) * 100, 1) if total else 0.0

    wards = db.query(Ward).filter(Ward.hospital_id == hospital_id, Ward.is_active.is_(True)).all()
    by_ward = []
    for w in wards:
        wt = db.query(func.count(Bed.id)).filter(Bed.hospital_id == hospital_id, Bed.ward_id == w.id, Bed.is_active.is_(True)).scalar() or 0
        wo = (
            db.query(func.count(Bed.id))
            .filter(Bed.hospital_id == hospital_id, Bed.ward_id == w.id, Bed.is_active.is_(True), Bed.is_occupied.is_(True))
            .scalar()
            or 0
        )
        by_ward.append(
            {
                "ward_id": str(w.id),
                "ward_name": w.name,
                "total": int(wt),
                "occupied": int(wo),
                "available": int(wt) - int(wo),
                "occupancy_percent": round((wo / wt) * 100, 1) if wt else 0.0,
            }
        )

    return OccupancyReport(
        total_beds=int(total),
        occupied_beds=int(occupied),
        available_beds=available,
        occupancy_percent=pct,
        by_ward=by_ward,
    )


@router.get("/wards", response_model=list[WardRoomOption])
def list_wards(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    wards = db.query(Ward).filter(Ward.hospital_id == hospital_id, Ward.is_active.is_(True)).order_by(Ward.name.asc()).all()
    return [WardRoomOption(id=w.id, name=w.name, ward_type=w.ward_type.value if w.ward_type else None) for w in wards]


@router.get("/rooms", response_model=list[RoomOption])
def list_rooms(
    ward_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(Room).filter(Room.hospital_id == hospital_id, Room.is_active.is_(True))
    if ward_id:
        q = q.filter(Room.ward_id == ward_id)
    rooms = q.order_by(Room.room_code.asc()).all()
    return [
        RoomOption(id=r.id, ward_id=r.ward_id, room_code=r.room_code, name=r.name, bed_count=r.bed_count)
        for r in rooms
    ]


@router.get("/options", response_model=list[BedOption])
def list_bed_options(
    ward_id: UUID | None = Query(default=None),
    room_id: UUID | None = Query(default=None),
    available_only: bool = Query(default=True),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
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


@router.get("/admissions/active", response_model=list[AdmissionDetail])
def list_active_admissions(
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(Admission)
        .options(
            joinedload(Admission.patient),
            joinedload(Admission.ward),
            joinedload(Admission.room),
            joinedload(Admission.bed),
            joinedload(Admission.doctor),
        )
        .filter(Admission.hospital_id == hospital_id, Admission.status == AdmissionStatus.admitted)
    )
    rows = q.order_by(Admission.admitted_at.desc()).all()
    if search:
        term = search.strip().lower()
        rows = [
            a
            for a in rows
            if a.patient
            and (
                term in (a.patient.name or "").lower()
                or term in (a.patient.uhid or "").lower()
                or term in (a.patient.mobile or "")
            )
        ]
    return [_admission_detail(a) for a in rows]


@router.get("/doctors")
def list_doctors(
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
    return [{"id": str(d.id), "name": d.name, "phone": d.phone, "email": d.email} for d in users if _is_doctor(d)]


@router.post("/admit", response_model=AdmissionDetail, status_code=status.HTTP_201_CREATED)
def admit_patient(
    payload: AdmitRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = db.query(Patient).filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    active = (
        db.query(Admission)
        .filter(
            Admission.patient_id == payload.patient_id,
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.admitted,
        )
        .first()
    )
    if active:
        raise HTTPException(status_code=409, detail="Patient is already admitted")

    bed = _get_free_bed(db, hospital_id, payload.ward_id, payload.room_id, payload.bed_id)

    if payload.doctor_id:
        doctor = (
            db.query(HospitalUser)
            .filter(HospitalUser.id == payload.doctor_id, HospitalUser.hospital_id == hospital_id)
            .first()
        )
        if not doctor:
            raise HTTPException(status_code=404, detail="Doctor not found")

    admitted_at = datetime.now(timezone.utc)
    if payload.admission_date:
        admitted_at = datetime.combine(payload.admission_date, time(9, 0), tzinfo=timezone.utc)

    admission = Admission(
        hospital_id=hospital_id,
        patient_id=payload.patient_id,
        ward_id=payload.ward_id,
        room_id=payload.room_id,
        bed_id=payload.bed_id,
        doctor_id=payload.doctor_id,
        status=AdmissionStatus.admitted,
        notes=payload.notes.strip() if payload.notes else None,
        admitted_at=admitted_at,
    )
    bed.is_occupied = True
    patient.status = PatientStatus.admitted
    db.add(admission)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="admission",
        entity_id=admission.id,
        summary=f"Admitted {patient.uhid} {patient.name} → {bed.ward.name if bed.ward else ''}/{bed.room.room_code if bed.room else ''}/{bed.bed_code}",
    )
    db.commit()
    return _admission_detail(_load_admission(db, hospital_id, admission.id, None))


@router.put("/allocate", response_model=AdmissionDetail)
def allocate_bed(
    payload: AllocateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    admission = _load_admission(db, hospital_id, payload.admission_id, payload.patient_id)
    if admission.bed_id == payload.bed_id:
        return _admission_detail(admission)

    new_bed = _get_free_bed(db, hospital_id, payload.ward_id, payload.room_id, payload.bed_id)
    old_bed = db.query(Bed).filter(Bed.id == admission.bed_id).first()
    if old_bed:
        old_bed.is_occupied = False

    admission.ward_id = payload.ward_id
    admission.room_id = payload.room_id
    admission.bed_id = payload.bed_id
    new_bed.is_occupied = True

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="admission",
        entity_id=admission.id,
        summary=f"Allocated bed {new_bed.bed_code} to {admission.patient.name if admission.patient else 'patient'}",
    )
    db.commit()
    return _admission_detail(_load_admission(db, hospital_id, admission.id, None))


@router.put("/transfer", response_model=AdmissionDetail)
def transfer_bed(
    payload: TransferRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    admission = _load_admission(db, hospital_id, payload.admission_id, payload.patient_id)
    if admission.bed_id == payload.to_bed_id:
        raise HTTPException(status_code=400, detail="Patient is already on this bed")

    new_bed = _get_free_bed(db, hospital_id, payload.to_ward_id, payload.to_room_id, payload.to_bed_id)
    old_bed = (
        db.query(Bed)
        .options(joinedload(Bed.ward), joinedload(Bed.room))
        .filter(Bed.id == admission.bed_id)
        .first()
    )
    from_label = (
        f"{old_bed.ward.name if old_bed and old_bed.ward else '?'} → {old_bed.room.room_code if old_bed and old_bed.room else '?'} → {old_bed.bed_code if old_bed else '?'}"
    )
    to_label = (
        f"{new_bed.ward.name if new_bed.ward else '?'} → {new_bed.room.room_code if new_bed.room else '?'} → {new_bed.bed_code}"
    )

    if old_bed:
        old_bed.is_occupied = False
    admission.ward_id = payload.to_ward_id
    admission.room_id = payload.to_room_id
    admission.bed_id = payload.to_bed_id
    new_bed.is_occupied = True

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="admission",
        entity_id=admission.id,
        summary=f"Transferred {admission.patient.name if admission.patient else 'patient'}: {from_label} → {to_label}",
    )
    db.commit()
    return _admission_detail(_load_admission(db, hospital_id, admission.id, None))


@router.post("/discharge", response_model=AdmissionDetail)
def discharge_patient(
    payload: DischargeRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    admission = _load_admission(db, hospital_id, payload.admission_id, payload.patient_id)

    d_date = payload.discharge_date or date.today()
    d_time = payload.discharge_time or datetime.now(timezone.utc).time().replace(microsecond=0)
    discharged_at = datetime.combine(d_date, d_time, tzinfo=timezone.utc)

    admission.status = AdmissionStatus.discharged
    admission.discharged_at = discharged_at
    admission.discharge_notes = payload.discharge_notes.strip() if payload.discharge_notes else None

    if admission.bed:
        admission.bed.is_occupied = False
    if admission.patient:
        admission.patient.status = PatientStatus.active

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="admission",
        entity_id=admission.id,
        summary=f"Discharged {admission.patient.name if admission.patient else 'patient'} — bed freed",
    )
    db.commit()

    a = (
        db.query(Admission)
        .options(
            joinedload(Admission.patient),
            joinedload(Admission.ward),
            joinedload(Admission.room),
            joinedload(Admission.bed),
            joinedload(Admission.doctor),
        )
        .filter(Admission.id == admission.id)
        .first()
    )
    return _admission_detail(a)
