from datetime import date, datetime, time, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from pydantic import BaseModel

from app.database import get_db
from app.models import (
    Admission,
    AdmissionStatus,
    AuditLog,
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
    DischargeQueueItem,
    DischargeRequest,
    DischargeRequestCreate,
    OccupancyReport,
    RoomOption,
    TransferRequest,
    WardRoomOption,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user
from app.utils.billing import ensure_bed_charge_for_admission, patient_ledger_totals

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
    ward = a.ward
    return AdmissionDetail(
        id=a.id,
        patient_id=a.patient_id,
        patient_name=a.patient.name if a.patient else None,
        patient_uhid=getattr(a.patient, "uhid", None) if a.patient else None,
        patient_mobile=a.patient.mobile if a.patient else None,
        ward_id=a.ward_id,
        room_id=a.room_id,
        bed_id=a.bed_id,
        ward_name=ward.name if ward else None,
        room_code=a.room.room_code if a.room else None,
        bed_code=a.bed.bed_code if a.bed else None,
        doctor_id=a.doctor_id,
        doctor_name=a.doctor.name if a.doctor else None,
        status=a.status,
        notes=a.notes,
        discharge_notes=getattr(a, "discharge_notes", None),
        admitted_at=a.admitted_at,
        discharged_at=a.discharged_at,
        admission_fee=float(getattr(ward, "admission_fee", 0) or 0) if ward else 0.0,
        bed_charge_per_day=float(getattr(ward, "bed_charge_per_day", 0) or 0) if ward else 0.0,
        ip_id=getattr(a, "ip_id", None),
        source_appointment_id=getattr(a, "source_appointment_id", None),
    )


def _load_admission(
    db: Session,
    hospital_id: UUID,
    admission_id: UUID | None,
    patient_id: UUID | None,
    *,
    statuses: tuple[AdmissionStatus, ...] = (AdmissionStatus.admitted,),
) -> Admission:
    q = (
        db.query(Admission)
        .options(
            joinedload(Admission.patient),
            joinedload(Admission.ward),
            joinedload(Admission.room),
            joinedload(Admission.bed),
            joinedload(Admission.doctor),
        )
        .filter(Admission.hospital_id == hospital_id, Admission.status.in_(statuses))
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


def _apply_bed_charge(db: Session, admission: Admission, *, until: datetime, actor_name: str) -> None:
    ward = admission.ward
    ensure_bed_charge_for_admission(
        db,
        hospital_id=admission.hospital_id,
        patient_id=admission.patient_id,
        admission_id=admission.id,
        admitted_at=admission.admitted_at,
        discharged_at=until,
        ward_name=ward.name if ward else None,
        room_code=admission.room.room_code if admission.room else None,
        bed_code=admission.bed.bed_code if admission.bed else None,
        bed_charge_per_day=float(getattr(ward, "bed_charge_per_day", 0) or 0) if ward else 0.0,
        created_by_name=actor_name,
    )


def _discharge_queue_item(db: Session, hospital_id: UUID, a: Admission) -> DischargeQueueItem:
    fin = patient_ledger_totals(db, hospital_id, a.patient_id)
    outstanding = float(fin.get("outstanding") or 0)
    base = _admission_detail(a)
    return DischargeQueueItem(
        **base.model_dump(),
        total_charges=float(fin.get("total_charges") or 0),
        total_paid=float(fin.get("total_paid") or 0),
        outstanding=outstanding,
        can_discharge=outstanding <= 0.009,
    )


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
        .filter(
            Admission.hospital_id == hospital_id,
            Admission.status.in_([AdmissionStatus.admitted, AdmissionStatus.discharge_requested]),
        )
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
    if wards:
        ward_ids = [w.id for w in wards]
        total_rows = (
            db.query(Bed.ward_id, func.count(Bed.id))
            .filter(Bed.hospital_id == hospital_id, Bed.ward_id.in_(ward_ids), Bed.is_active.is_(True))
            .group_by(Bed.ward_id)
            .all()
        )
        occ_rows = (
            db.query(Bed.ward_id, func.count(Bed.id))
            .filter(
                Bed.hospital_id == hospital_id,
                Bed.ward_id.in_(ward_ids),
                Bed.is_active.is_(True),
                Bed.is_occupied.is_(True),
            )
            .group_by(Bed.ward_id)
            .all()
        )
        totals_map = {wid: int(cnt) for wid, cnt in total_rows}
        occ_map = {wid: int(cnt) for wid, cnt in occ_rows}
        for w in wards:
            wt = totals_map.get(w.id, 0)
            wo = occ_map.get(w.id, 0)
            by_ward.append(
                {
                    "ward_id": str(w.id),
                    "ward_name": w.name,
                    "total": wt,
                    "occupied": wo,
                    "available": wt - wo,
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
    return [
        WardRoomOption(
            id=w.id,
            name=w.name,
            ward_type=w.ward_type.value if w.ward_type else None,
            admission_fee=float(getattr(w, "admission_fee", 0) or 0),
            bed_charge_per_day=float(getattr(w, "bed_charge_per_day", 0) or 0),
        )
        for w in wards
    ]


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
        .filter(
            Admission.hospital_id == hospital_id,
            Admission.status.in_([AdmissionStatus.admitted, AdmissionStatus.discharge_requested]),
        )
    )
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            Admission.patient_id.in_(
                db.query(Patient.id).filter(
                    Patient.hospital_id == hospital_id,
                    or_(
                        Patient.name.ilike(term),
                        Patient.uhid.ilike(term),
                        Patient.mobile.ilike(term),
                    ),
                )
            )
        )
    rows = q.order_by(Admission.admitted_at.desc()).all()
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
        for d in users
        if _is_doctor(d)
    ]


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

    from app.utils.admissions import create_admission

    admitted_at = datetime.now(timezone.utc)
    if payload.admission_date:
        admitted_at = datetime.combine(payload.admission_date, time(9, 0), tzinfo=timezone.utc)

    admission = create_admission(
        db,
        hospital_id=hospital_id,
        patient_id=payload.patient_id,
        ward_id=payload.ward_id,
        room_id=payload.room_id,
        bed_id=payload.bed_id,
        doctor_id=payload.doctor_id,
        notes=payload.notes,
        created_by_name=str(user.get("name") or "System"),
        admitted_at=admitted_at,
    )

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="admission",
        entity_id=admission.id,
        summary=f"Admitted {patient.uhid} {patient.name} ({admission.ip_id})",
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


@router.post("/discharge-request", response_model=AdmissionDetail)
def request_discharge(
    payload: DischargeRequestCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Doctor requests discharge — nurse completes it after dues are cleared."""
    admission = _load_admission(db, hospital_id, payload.admission_id, payload.patient_id)
    now = datetime.now(timezone.utc)
    admission.status = AdmissionStatus.discharge_requested
    if payload.discharge_notes and payload.discharge_notes.strip():
        admission.discharge_notes = payload.discharge_notes.strip()
    _apply_bed_charge(db, admission, until=now, actor_name=user.get("name") or "System")

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="admission",
        entity_id=admission.id,
        summary=f"Discharge requested for {admission.patient.name if admission.patient else 'patient'}",
    )
    db.commit()
    return _admission_detail(_load_admission(
        db,
        hospital_id,
        admission.id,
        None,
        statuses=(AdmissionStatus.discharge_requested,),
    ))


@router.get("/discharge-requests", response_model=list[DischargeQueueItem])
def list_discharge_requests(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Nurse queue: discharge requests with billing totals."""
    rows = (
        db.query(Admission)
        .options(
            joinedload(Admission.patient),
            joinedload(Admission.ward),
            joinedload(Admission.room),
            joinedload(Admission.bed),
            joinedload(Admission.doctor),
        )
        .filter(
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.discharge_requested,
        )
        .order_by(Admission.admitted_at.asc())
        .all()
    )
    now = datetime.now(timezone.utc)
    items: list[DischargeQueueItem] = []
    for a in rows:
        _apply_bed_charge(db, a, until=now, actor_name="System")
        items.append(_discharge_queue_item(db, hospital_id, a))
    if rows:
        db.commit()
    return items


@router.post("/discharge", response_model=AdmissionDetail)
def discharge_patient(
    payload: DischargeRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Nurse completes discharge only after doctor requested it and dues are ₹0."""
    admission = _load_admission(
        db,
        hospital_id,
        payload.admission_id,
        payload.patient_id,
        statuses=(AdmissionStatus.discharge_requested,),
    )

    d_date = payload.discharge_date or date.today()
    d_time = payload.discharge_time or datetime.now(timezone.utc).time().replace(microsecond=0)
    discharged_at = datetime.combine(d_date, d_time, tzinfo=timezone.utc)

    _apply_bed_charge(
        db,
        admission,
        until=discharged_at,
        actor_name=user.get("name") or "System",
    )
    fin = patient_ledger_totals(db, hospital_id, admission.patient_id)
    outstanding = float(fin.get("outstanding") or 0)
    if outstanding > 0.009:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot discharge — outstanding balance ₹{outstanding:,.2f}. "
                "Clear all dues before discharging."
            ),
        )

    admission.status = AdmissionStatus.discharged
    admission.discharged_at = discharged_at
    if payload.discharge_notes and payload.discharge_notes.strip():
        admission.discharge_notes = payload.discharge_notes.strip()

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
    if a is None:
        raise HTTPException(status_code=404, detail="Admission not found after discharge")
    return _admission_detail(a)


# ── Admission audit-trail (transfer/discharge history panel) ────────────────────
class AdmissionHistoryEntry(BaseModel):
    id: UUID
    action: str
    actor_name: str
    actor_role_label: str | None
    summary: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/admissions/{admission_id}/history", response_model=list[AdmissionHistoryEntry])
def get_admission_history(
    admission_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Real audit-trail feed (allocate/transfer/discharge events) for one admission,
    for the Bed Transfer/Discharge sidebar. Open to hospital_admin and hospital_staff
    (reception/nursing use this screen), unlike the admin-only /api/admin/audit-logs
    endpoint — scoped to the same audit_logs rows written by write_audit() in this file."""
    admission = (
        db.query(Admission)
        .filter(Admission.id == admission_id, Admission.hospital_id == hospital_id)
        .first()
    )
    if not admission:
        raise HTTPException(status_code=404, detail="Admission not found")

    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.hospital_id == hospital_id,
            AuditLog.entity_type == "admission",
            AuditLog.entity_id == str(admission_id),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(200)
        .all()
    )
    return rows
