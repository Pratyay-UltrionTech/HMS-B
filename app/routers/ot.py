from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Department, Hospital, HospitalUser, OtRoom, OtSurgery, OtSurgeryStatus, Patient
from app.schemas_ot import (
    OtCalendarEntry,
    OtCompleteRequest,
    OtDashboardResponse,
    OtNotesRequest,
    OtRescheduleRequest,
    OtSurgeryCreate,
    OtSurgeryResponse,
    OtSurgeryUpdate,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user
from app.utils.medical_record_sync import sync_ot_surgery_medical_record

router = APIRouter(prefix="/ot", tags=["ot"])


def _actor_name(user: dict) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _actor_role(user: dict) -> str:
    return str(user.get("staff_role_name") or user.get("role") or "")


def _next_surgery_no(db: Session, hospital_id: UUID) -> str:
    count = db.query(func.count(OtSurgery.id)).filter(OtSurgery.hospital_id == hospital_id).scalar() or 0
    return f"OT{int(count) + 1:04d}"


def _resolve_ot_room(db: Session, hospital_id: UUID, ot_room_id: UUID, department_id: UUID | None = None) -> OtRoom:
    room = (
        db.query(OtRoom)
        .filter(OtRoom.id == ot_room_id, OtRoom.hospital_id == hospital_id, OtRoom.is_active.is_(True))
        .first()
    )
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OT room not found")
    if department_id and room.department_id != department_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected OT room does not belong to the selected department",
        )
    return room


def _room_label(room: OtRoom) -> str:
    return f"{room.code} — {room.name}"


def _surgery_to_response(item: OtSurgery) -> OtSurgeryResponse:
    dept = item.department
    room = item.ot_room_ref
    return OtSurgeryResponse(
        id=item.id,
        hospital_id=item.hospital_id,
        surgery_no=item.surgery_no,
        patient_id=item.patient_id,
        surgeon_id=item.surgeon_id,
        assistant_surgeon=item.assistant_surgeon,
        surgery_type=item.surgery_type,
        surgery_category=item.surgery_category,
        priority=item.priority,
        department_id=item.department_id,
        ot_room_id=item.ot_room_id,
        ot_room=item.ot_room,
        ot_charge_amount=float(getattr(item, "ot_charge_amount", 0) or 0),
        ot_room_rate=float(getattr(room, "base_ot_charge", 0) or 0) if room else None,
        scheduled_at=item.scheduled_at,
        duration_minutes=item.duration_minutes,
        anaesthetist=item.anaesthetist,
        remarks=item.remarks,
        booked_by_name=item.booked_by_name,
        booked_by_role=item.booked_by_role,
        status=item.status,
        started_at=item.started_at,
        completed_at=item.completed_at,
        actual_duration_minutes=item.actual_duration_minutes,
        shifted_to=item.shifted_to,
        pre_op_diagnosis=item.pre_op_diagnosis,
        procedure_performed=item.procedure_performed,
        findings=item.findings,
        implants_used=item.implants_used,
        complications=item.complications,
        post_op_instructions=item.post_op_instructions,
        follow_up_notes=item.follow_up_notes,
        notes_recorded_by=item.notes_recorded_by,
        notes_recorded_at=item.notes_recorded_at,
        ot_report_file_name=item.ot_report_file_name,
        has_ot_report=bool(item.ot_report_file_data),
        consent_file_name=item.consent_file_name,
        has_consent=bool(item.consent_file_data),
        image_file_name=item.image_file_name,
        has_image=bool(item.image_file_data),
        has_notes=bool(item.pre_op_diagnosis and item.procedure_performed),
        created_at=item.created_at,
        patient_name=item.patient.name if item.patient else None,
        patient_uhid=item.patient.uhid if item.patient else None,
        patient_mobile=item.patient.mobile if item.patient else None,
        surgeon_name=item.surgeon.name if item.surgeon else None,
        department_name=dept.name if dept else None,
        ot_room_name=room.name if room else None,
    )


def _get_surgery(db: Session, surgery_id: UUID, hospital_id: UUID) -> OtSurgery:
    item = (
        db.query(OtSurgery)
        .options(
            joinedload(OtSurgery.patient),
            joinedload(OtSurgery.surgeon),
            joinedload(OtSurgery.department),
            joinedload(OtSurgery.ot_room_ref),
        )
        .filter(OtSurgery.id == surgery_id, OtSurgery.hospital_id == hospital_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
    return item


def _surgery_end(item: OtSurgery) -> datetime:
    start = item.scheduled_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start + timedelta(minutes=int(item.duration_minutes or 60))


def _sync_time_based_status(item: OtSurgery, now: datetime | None = None) -> None:
    """Move scheduled surgeries into in_progress while inside the booked window."""
    if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
        return
    now = now or datetime.now(timezone.utc)
    start = item.scheduled_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = _surgery_end(item)
    if item.status in (OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed) and start <= now < end:
        item.status = OtSurgeryStatus.in_progress
        if not item.started_at:
            item.started_at = start


def _ot_room_conflict(
    db: Session,
    hospital_id: UUID,
    ot_room_id: UUID,
    start: datetime,
    end: datetime,
    exclude_id: UUID | None = None,
) -> bool:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    rows = (
        db.query(OtSurgery)
        .filter(
            OtSurgery.hospital_id == hospital_id,
            OtSurgery.ot_room_id == ot_room_id,
            OtSurgery.status.notin_([OtSurgeryStatus.cancelled]),
        )
        .all()
    )
    for item in rows:
        if exclude_id and item.id == exclude_id:
            continue
        s = item.scheduled_at
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc)
        e = s + timedelta(minutes=int(item.duration_minutes or 60))
        if s < end and start < e:
            return True
    return False


@router.get("/calendar", response_model=list[OtCalendarEntry])
def ot_calendar(
    date_from: date = Query(...),
    date_to: date = Query(...),
    ot_room_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="date_to must be on or after date_from")
    start = datetime.combine(date_from, time.min).replace(tzinfo=timezone.utc)
    end = datetime.combine(date_to, time.max).replace(tzinfo=timezone.utc)
    q = (
        db.query(OtSurgery)
        .options(joinedload(OtSurgery.patient), joinedload(OtSurgery.surgeon), joinedload(OtSurgery.ot_room_ref))
        .filter(
            OtSurgery.hospital_id == hospital_id,
            OtSurgery.scheduled_at <= end,
            OtSurgery.status != OtSurgeryStatus.cancelled,
        )
    )
    if ot_room_id:
        q = q.filter(OtSurgery.ot_room_id == ot_room_id)
    rows = q.order_by(OtSurgery.scheduled_at.asc()).all()
    out: list[OtCalendarEntry] = []
    for item in rows:
        ends = _surgery_end(item)
        if ends < start:
            continue
        room = item.ot_room_ref
        if not item.ot_room_id and not room:
            continue
        label = _room_label(room) if room else (item.ot_room or "OT")
        out.append(
            OtCalendarEntry(
                id=item.id,
                ot_room_id=item.ot_room_id or room.id,
                ot_room_label=label,
                scheduled_at=item.scheduled_at,
                ends_at=ends,
                surgery_type=item.surgery_type,
                status=item.status,
                patient_name=item.patient.name if item.patient else None,
                surgeon_name=item.surgeon.name if item.surgeon else None,
            )
        )
    return out


@router.get("/dashboard", response_model=OtDashboardResponse)
def dashboard(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = date.today()
    start = datetime.combine(today, time.min).replace(tzinfo=timezone.utc)
    end = datetime.combine(today, time.max).replace(tzinfo=timezone.utc)
    q = db.query(OtSurgery).filter(OtSurgery.hospital_id == hospital_id)
    todays = q.filter(OtSurgery.scheduled_at >= start, OtSurgery.scheduled_at <= end).count()
    completed = q.filter(OtSurgery.status == OtSurgeryStatus.completed).count()
    ongoing = q.filter(OtSurgery.status == OtSurgeryStatus.in_progress).count()
    scheduled = q.filter(
        OtSurgery.status.in_([OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed])
    ).count()
    cancelled = q.filter(OtSurgery.status == OtSurgeryStatus.cancelled).count()
    return OtDashboardResponse(
        todays_surgeries=todays,
        completed=completed,
        ongoing=ongoing,
        scheduled=scheduled,
        cancelled=cancelled,
    )


@router.get("/surgeries", response_model=list[OtSurgeryResponse])
def list_surgeries(
    status_filter: str | None = Query(None, alias="status"),
    patient_id: UUID | None = None,
    search: str | None = None,
    schedule_only: bool | None = Query(None),
    ongoing_only: bool | None = Query(None),
    history_only: bool | None = Query(None),
    notes_pending: bool | None = Query(None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(OtSurgery)
        .options(joinedload(OtSurgery.patient), joinedload(OtSurgery.surgeon), joinedload(OtSurgery.department), joinedload(OtSurgery.ot_room_ref))
        .filter(OtSurgery.hospital_id == hospital_id)
    )
    if patient_id:
        q = q.filter(OtSurgery.patient_id == patient_id)
    if status_filter:
        try:
            q = q.filter(OtSurgery.status == OtSurgeryStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")
    if schedule_only:
        q = q.filter(OtSurgery.status.notin_([OtSurgeryStatus.completed, OtSurgeryStatus.cancelled]))
    if ongoing_only:
        q = q.filter(OtSurgery.status == OtSurgeryStatus.in_progress)
    if history_only:
        q = q.filter(OtSurgery.status == OtSurgeryStatus.completed)
    if notes_pending:
        q = q.filter(
            OtSurgery.status.in_([OtSurgeryStatus.completed, OtSurgeryStatus.in_progress]),
            or_(OtSurgery.pre_op_diagnosis.is_(None), OtSurgery.procedure_performed.is_(None)),
        )
    if search:
        like = f"%{search.strip()}%"
        q = q.outerjoin(Patient, OtSurgery.patient_id == Patient.id).filter(
            or_(
                OtSurgery.surgery_no.ilike(like),
                OtSurgery.surgery_type.ilike(like),
                OtSurgery.ot_room.ilike(like),
                Patient.name.ilike(like),
                Patient.uhid.ilike(like),
            )
        )
    rows = q.order_by(OtSurgery.scheduled_at.desc()).limit(300).all()
    now = datetime.now(timezone.utc)
    for r in rows:
        _sync_time_based_status(r, now)
    db.commit()
    return [_surgery_to_response(r) for r in rows]


@router.get("/surgeries/{surgery_id}", response_model=OtSurgeryResponse)
def get_surgery(
    surgery_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    return _surgery_to_response(_get_surgery(db, surgery_id, hospital_id))


@router.post("/surgeries", response_model=OtSurgeryResponse, status_code=status.HTTP_201_CREATED)
def create_surgery(
    payload: OtSurgeryCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = (
        db.query(Patient)
        .filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id)
        .first()
    )
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    surgeon = None
    if payload.surgeon_id:
        surgeon = (
            db.query(HospitalUser)
            .filter(HospitalUser.id == payload.surgeon_id, HospitalUser.hospital_id == hospital_id)
            .first()
        )
        if not surgeon:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgeon not found")

    dept = (
        db.query(Department)
        .filter(Department.id == payload.department_id, Department.hospital_id == hospital_id)
        .first()
    )
    if not dept:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    ot_room = _resolve_ot_room(db, hospital_id, payload.ot_room_id, payload.department_id)

    start = payload.scheduled_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = start + timedelta(minutes=int(payload.duration_minutes or 60))
    if _ot_room_conflict(db, hospital_id, ot_room.id, start, end):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="OT room is already booked for this time range",
        )

    item = OtSurgery(
        hospital_id=hospital_id,
        surgery_no=_next_surgery_no(db, hospital_id),
        patient_id=payload.patient_id,
        surgeon_id=payload.surgeon_id,
        assistant_surgeon=payload.assistant_surgeon.strip() if payload.assistant_surgeon else None,
        surgery_type=payload.surgery_type.strip(),
        surgery_category=payload.surgery_category.strip() or "General",
        priority=payload.priority,
        department_id=payload.department_id,
        ot_room_id=ot_room.id,
        ot_room=_room_label(ot_room),
        ot_charge_amount=float(getattr(ot_room, "base_ot_charge", 0) or 0),
        scheduled_at=payload.scheduled_at,
        duration_minutes=payload.duration_minutes,
        anaesthetist=payload.anaesthetist.strip() if payload.anaesthetist else None,
        remarks=payload.remarks.strip() if payload.remarks else None,
        booked_by_name=_actor_name(user),
        booked_by_role=_actor_role(user),
        status=OtSurgeryStatus.scheduled,
    )
    db.add(item)
    db.flush()

    from app.models import BillingSourceType
    from app.utils.billing import ensure_charge

    ensure_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient.id,
        source_type=BillingSourceType.ot,
        source_id=item.id,
        description=f"OT Charge — {item.surgery_no} · {_room_label(ot_room)}"[:512],
        charge_amount=float(item.ot_charge_amount or 0),
        created_by_name=_actor_name(user),
    )

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="ot_surgery",
        entity_id=item.id,
        summary=f"Booked surgery {item.surgery_no} — {item.surgery_type} for {patient.name}",
    )
    db.commit()
    return _surgery_to_response(_get_surgery(db, item.id, hospital_id))


@router.put("/surgeries/{surgery_id}", response_model=OtSurgeryResponse)
def update_surgery(
    surgery_id: UUID,
    payload: OtSurgeryUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_surgery(db, surgery_id, hospital_id)
    if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot edit completed/cancelled surgery")

    data = payload.model_dump(exclude_unset=True)
    if "surgeon_id" in data and data["surgeon_id"]:
        surgeon = (
            db.query(HospitalUser)
            .filter(HospitalUser.id == data["surgeon_id"], HospitalUser.hospital_id == hospital_id)
            .first()
        )
        if not surgeon:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgeon not found")
    for key, value in data.items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(item, key, value)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="ot_surgery",
        entity_id=item.id,
        summary=f"Updated surgery {item.surgery_no}",
    )
    db.commit()
    return _surgery_to_response(_get_surgery(db, surgery_id, hospital_id))


@router.post("/surgeries/{surgery_id}/confirm", response_model=OtSurgeryResponse)
def confirm_surgery(
    surgery_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_surgery(db, surgery_id, hospital_id)
    if item.status != OtSurgeryStatus.scheduled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only scheduled surgeries can be confirmed")
    item.status = OtSurgeryStatus.confirmed
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="ot_surgery",
        entity_id=item.id,
        summary=f"Confirmed surgery {item.surgery_no}",
    )
    db.commit()
    return _surgery_to_response(_get_surgery(db, surgery_id, hospital_id))


@router.post("/surgeries/{surgery_id}/reschedule", response_model=OtSurgeryResponse)
def reschedule_surgery(
    surgery_id: UUID,
    payload: OtRescheduleRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_surgery(db, surgery_id, hospital_id)
    if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot reschedule this surgery")
    item.scheduled_at = payload.scheduled_at
    if payload.ot_room_id:
        dept_id = payload.department_id or item.department_id
        if payload.department_id:
            dept = (
                db.query(Department)
                .filter(Department.id == payload.department_id, Department.hospital_id == hospital_id)
                .first()
            )
            if not dept:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
            item.department_id = payload.department_id
        ot_room = _resolve_ot_room(db, hospital_id, payload.ot_room_id, dept_id)
        item.ot_room_id = ot_room.id
        item.ot_room = _room_label(ot_room)
        if not item.department_id:
            item.department_id = ot_room.department_id
    elif payload.ot_room:
        item.ot_room = payload.ot_room.strip()
    if payload.duration_minutes:
        item.duration_minutes = payload.duration_minutes
    if payload.remarks is not None:
        item.remarks = payload.remarks.strip() or None
    if item.status == OtSurgeryStatus.confirmed:
        item.status = OtSurgeryStatus.scheduled
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="ot_surgery",
        entity_id=item.id,
        summary=f"Rescheduled surgery {item.surgery_no}",
    )
    db.commit()
    return _surgery_to_response(_get_surgery(db, surgery_id, hospital_id))


@router.post("/surgeries/{surgery_id}/start", response_model=OtSurgeryResponse)
def start_surgery(
    surgery_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_surgery(db, surgery_id, hospital_id)
    if item.status not in (OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Surgery cannot be started from current status")
    item.status = OtSurgeryStatus.in_progress
    item.started_at = datetime.now(timezone.utc)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="ot_surgery",
        entity_id=item.id,
        summary=f"Started surgery {item.surgery_no}",
    )
    db.commit()
    return _surgery_to_response(_get_surgery(db, surgery_id, hospital_id))


@router.post("/surgeries/{surgery_id}/complete", response_model=OtSurgeryResponse)
def complete_surgery(
    surgery_id: UUID,
    payload: OtCompleteRequest | None = None,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_surgery(db, surgery_id, hospital_id)
    now = datetime.now(timezone.utc)
    _sync_time_based_status(item, now)
    if item.status in (OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed):
        start = item.scheduled_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if now >= start:
            item.status = OtSurgeryStatus.in_progress
            item.started_at = item.started_at or start
    if item.status != OtSurgeryStatus.in_progress:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Surgery is not ongoing yet")
    item.status = OtSurgeryStatus.completed
    item.completed_at = now
    if payload:
        if payload.shifted_to:
            item.shifted_to = payload.shifted_to.strip()
        if payload.actual_duration_minutes:
            item.actual_duration_minutes = payload.actual_duration_minutes
        elif item.started_at:
            item.actual_duration_minutes = max(1, int((now - item.started_at).total_seconds() // 60))
    elif item.started_at:
        item.actual_duration_minutes = max(1, int((now - item.started_at).total_seconds() // 60))
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="ot_surgery",
        entity_id=item.id,
        summary=f"Completed surgery {item.surgery_no}",
    )
    db.commit()
    return _surgery_to_response(_get_surgery(db, surgery_id, hospital_id))


@router.post("/surgeries/{surgery_id}/cancel", response_model=OtSurgeryResponse)
def cancel_surgery(
    surgery_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_surgery(db, surgery_id, hospital_id)
    if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Surgery cannot be cancelled")
    item.status = OtSurgeryStatus.cancelled

    from app.models import BillingSourceType
    from app.utils.billing import cancel_charge_for_source

    cancel_charge_for_source(db, hospital_id, BillingSourceType.ot, item.id)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="ot_surgery",
        entity_id=item.id,
        summary=f"Cancelled surgery {item.surgery_no}",
    )
    db.commit()
    return _surgery_to_response(_get_surgery(db, surgery_id, hospital_id))


@router.post("/surgeries/{surgery_id}/notes", response_model=OtSurgeryResponse)
def save_notes(
    surgery_id: UUID,
    payload: OtNotesRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_surgery(db, surgery_id, hospital_id)
    if item.status == OtSurgeryStatus.cancelled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot add notes to a cancelled surgery")
    _sync_time_based_status(item)
    item.pre_op_diagnosis = payload.pre_op_diagnosis.strip()
    item.procedure_performed = payload.procedure_performed.strip()
    item.findings = payload.findings.strip() if payload.findings else None
    item.implants_used = payload.implants_used.strip() if payload.implants_used else None
    item.complications = payload.complications.strip() if payload.complications else None
    item.post_op_instructions = payload.post_op_instructions.strip() if payload.post_op_instructions else None
    item.follow_up_notes = payload.follow_up_notes.strip() if payload.follow_up_notes else None
    if payload.shifted_to:
        item.shifted_to = payload.shifted_to.strip()
    item.notes_recorded_by = _actor_name(user)
    item.notes_recorded_at = datetime.now(timezone.utc)
    if payload.ot_report_file_data:
        item.ot_report_file_name = payload.ot_report_file_name
        item.ot_report_file_data = payload.ot_report_file_data
    if payload.consent_file_data:
        item.consent_file_name = payload.consent_file_name
        item.consent_file_data = payload.consent_file_data
    if payload.image_file_data:
        item.image_file_name = payload.image_file_name
        item.image_file_data = payload.image_file_data
    if item.status == OtSurgeryStatus.in_progress:
        item.status = OtSurgeryStatus.completed
        item.completed_at = item.completed_at or datetime.now(timezone.utc)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="ot_notes",
        entity_id=item.id,
        summary=f"Saved operation notes for {item.surgery_no}",
    )
    sync_ot_surgery_medical_record(db, item)
    db.commit()
    return _surgery_to_response(_get_surgery(db, surgery_id, hospital_id))


@router.get("/surgeries/{surgery_id}/summary-view")
def summary_html(
    surgery_id: UUID,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_surgery(db, surgery_id, hospital_id)
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    html = _summary_html(item, hospital)
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{item.surgery_no}-ot-summary.html"'},
    )


@router.get("/surgeries/{surgery_id}/file/{kind}")
def download_file(
    surgery_id: UUID,
    kind: str,
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    item = _get_surgery(db, surgery_id, hospital_id)
    if kind == "report":
        data, name = item.ot_report_file_data, item.ot_report_file_name or "ot-report.pdf"
    elif kind == "consent":
        data, name = item.consent_file_data, item.consent_file_name or "consent.pdf"
    elif kind == "image":
        data, name = item.image_file_data, item.image_file_name or "image.jpg"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="kind must be report, consent, or image")
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if data.startswith("data:"):
        header, b64 = data.split(",", 1)
        mime = header.split(";")[0].replace("data:", "") or "application/octet-stream"
        import base64

        raw = base64.b64decode(b64)
        return StreamingResponse(
            BytesIO(raw),
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file data")


def _summary_html(item: OtSurgery, hospital: Hospital | None) -> str:
    hosp = hospital.name if hospital else "Hospital"
    scheduled = item.scheduled_at.strftime("%d %b %Y %H:%M") if item.scheduled_at else "—"
    started = item.started_at.strftime("%d %b %Y %H:%M") if item.started_at else "—"
    completed = item.completed_at.strftime("%d %b %Y %H:%M") if item.completed_at else "—"
    duration = item.actual_duration_minutes or item.duration_minutes
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{item.surgery_no} OT Summary</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 840px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #c2410c; margin-bottom: 4px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .box {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; margin: 12px 0; background: #fff7ed; }}
  .label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #9a3412; }}
  .value {{ margin-top: 6px; white-space: pre-wrap; font-size: 14px; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <h1>{hosp}</h1>
  <p class="meta">Operation Theatre Summary · {item.surgery_no} · {item.surgery_type}</p>
  <div class="grid">
    <p><strong>Patient:</strong> {item.patient.name if item.patient else '—'} ({item.patient.uhid if item.patient else ''})</p>
    <p><strong>Surgeon:</strong> {item.surgeon.name if item.surgeon else '—'}</p>
    <p><strong>OT Room:</strong> {item.ot_room}</p>
    <p><strong>Priority:</strong> {item.priority.value if item.priority else '—'}</p>
    <p><strong>Scheduled:</strong> {scheduled}</p>
    <p><strong>Duration:</strong> {duration} min</p>
    <p><strong>Started:</strong> {started}</p>
    <p><strong>Completed:</strong> {completed}</p>
    <p><strong>Anaesthetist:</strong> {item.anaesthetist or '—'}</p>
    <p><strong>Shifted to:</strong> {item.shifted_to or '—'}</p>
  </div>
  <div class="box"><div class="label">Pre-operative Diagnosis</div><div class="value">{item.pre_op_diagnosis or '—'}</div></div>
  <div class="box"><div class="label">Procedure Performed</div><div class="value">{item.procedure_performed or '—'}</div></div>
  <div class="box"><div class="label">Findings</div><div class="value">{item.findings or '—'}</div></div>
  <div class="box"><div class="label">Implants Used</div><div class="value">{item.implants_used or '—'}</div></div>
  <div class="box"><div class="label">Complications</div><div class="value">{item.complications or '—'}</div></div>
  <div class="box"><div class="label">Post-operative Instructions</div><div class="value">{item.post_op_instructions or '—'}</div></div>
  <div class="box"><div class="label">Follow-up Notes</div><div class="value">{item.follow_up_notes or '—'}</div></div>
</body></html>"""
