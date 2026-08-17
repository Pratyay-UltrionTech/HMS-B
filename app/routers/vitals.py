from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Appointment, AppointmentStatus, Patient, VitalReading
from app.schemas_vitals import (
    VitalBatchCreate,
    VitalItemUpdate,
    VitalReadingResponse,
    VitalsTodayItem,
)
from app.utils.appointment_lifecycle import mark_in_progress
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/vitals", tags=["vitals"])


def _next_queue_token(db: Session, hospital_id: UUID, doctor_id: UUID, on_date: date) -> int:
    current = (
        db.query(func.max(Appointment.queue_token))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.doctor_id == doctor_id,
            Appointment.appointment_date == on_date,
        )
        .scalar()
    )
    return int(current or 0) + 1


def _revert_checked_in_without_vitals(
    db: Session,
    hospital_id: UUID,
    appointments: list[Appointment],
) -> None:
    """Keep status Scheduled until vitals exist (fixes walk-in auto check-in)."""
    if not appointments:
        return
    appt_ids = [a.id for a in appointments if a.status == AppointmentStatus.waiting]
    if not appt_ids:
        return
    with_vitals = {
        row[0]
        for row in db.query(VitalReading.appointment_id)
        .filter(
            VitalReading.hospital_id == hospital_id,
            VitalReading.appointment_id.in_(appt_ids),
        )
        .distinct()
        .all()
    }
    dirty = False
    for a in appointments:
        if a.status == AppointmentStatus.waiting and a.id not in with_vitals:
            a.status = AppointmentStatus.scheduled
            a.checked_in_at = None
            dirty = True
    if dirty:
        db.commit()
        for a in appointments:
            db.refresh(a)


def _reading_response(row: VitalReading) -> VitalReadingResponse:
    patient = row.patient
    appt = row.appointment
    doctor_name = None
    if appt is not None and getattr(appt, "doctor", None) is not None:
        doctor_name = appt.doctor.name
    return VitalReadingResponse(
        id=row.id,
        hospital_id=row.hospital_id,
        appointment_id=row.appointment_id,
        patient_id=row.patient_id,
        name=row.name,
        suitable_range=row.suitable_range,
        result=row.result,
        recorded_by_name=row.recorded_by_name,
        recorded_at=row.recorded_at,
        created_at=row.created_at,
        patient_name=patient.name if patient else None,
        doctor_name=doctor_name,
    )


@router.get("/today", response_model=list[VitalsTodayItem])
def list_today_bookings(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Today's appointments sorted by time, with any recorded vitals."""
    today = date.today()
    rows = (
        db.query(Appointment)
        .options(
            joinedload(Appointment.patient),
            joinedload(Appointment.doctor),
        )
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date == today,
            Appointment.status != AppointmentStatus.cancelled,
        )
        .order_by(Appointment.appointment_time.asc())
        .all()
    )
    if not rows:
        return []

    _revert_checked_in_without_vitals(db, hospital_id, rows)

    appt_ids = [a.id for a in rows]
    vitals = (
        db.query(VitalReading)
        .options(joinedload(VitalReading.patient), joinedload(VitalReading.appointment).joinedload(Appointment.doctor))
        .filter(
            VitalReading.hospital_id == hospital_id,
            VitalReading.appointment_id.in_(appt_ids),
        )
        .order_by(VitalReading.created_at.asc())
        .all()
    )
    by_appt: dict[UUID, list[VitalReading]] = {}
    for v in vitals:
        by_appt.setdefault(v.appointment_id, []).append(v)

    out: list[VitalsTodayItem] = []
    for a in rows:
        patient: Patient | None = a.patient
        items = by_appt.get(a.id, [])
        out.append(
            VitalsTodayItem(
                appointment_id=a.id,
                patient_id=a.patient_id,
                patient_name=patient.name if patient else "—",
                patient_uhid=getattr(patient, "uhid", None) if patient else None,
                patient_mobile=patient.mobile if patient else None,
                doctor_id=a.doctor_id,
                doctor_name=a.doctor.name if a.doctor else None,
                appointment_date=a.appointment_date,
                appointment_time=a.appointment_time,
                purpose=a.purpose,
                status=a.status.value if hasattr(a.status, "value") else str(a.status),
                vitals_count=len(items),
                vitals=[_reading_response(v) for v in items],
            )
        )
    return out


@router.get("", response_model=list[VitalReadingResponse])
def list_vitals(
    appointment_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(VitalReading)
        .options(
            joinedload(VitalReading.patient),
            joinedload(VitalReading.appointment).joinedload(Appointment.doctor),
        )
        .filter(VitalReading.hospital_id == hospital_id)
    )
    if appointment_id:
        q = q.filter(VitalReading.appointment_id == appointment_id)
    if patient_id:
        q = q.filter(VitalReading.patient_id == patient_id)
    if not appointment_id and not patient_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide appointment_id or patient_id",
        )
    rows = q.order_by(VitalReading.created_at.asc()).all()
    return [_reading_response(r) for r in rows]


def _assert_can_mutate_vitals(appt: Appointment) -> None:
    if appt.status in {AppointmentStatus.cancelled, AppointmentStatus.no_show}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot change vitals on this visit")
    if appt.status == AppointmentStatus.completed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Visit is already completed")
    if appt.status == AppointmentStatus.transferred_to_inpatient:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Visit was transferred to inpatient",
        )


@router.post("", response_model=list[VitalReadingResponse], status_code=status.HTTP_201_CREATED)
def create_vitals(
    payload: VitalBatchCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    appt = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(Appointment.id == payload.appointment_id, Appointment.hospital_id == hospital_id)
        .first()
    )
    if not appt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    _assert_can_mutate_vitals(appt)

    actor = str(user.get("name") or "Staff")
    created: list[VitalReading] = []
    for item in payload.items:
        name = item.name.strip()
        result = item.result.strip()
        suitable = (item.suitable_range or "").strip()
        if not name or not result:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Each vital needs name and result",
            )
        row = VitalReading(
            hospital_id=hospital_id,
            appointment_id=appt.id,
            patient_id=appt.patient_id,
            name=name,
            suitable_range=suitable,
            result=result,
            recorded_by_name=actor,
        )
        db.add(row)
        created.append(row)

    db.flush()

    # Vitals recorded → Checked in (waiting)
    if appt.status == AppointmentStatus.scheduled:
        mark_in_progress(appt, assign_checked_in=True)
        if not appt.queue_token:
            appt.queue_token = _next_queue_token(db, hospital_id, appt.doctor_id, appt.appointment_date)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="vital_reading",
        entity_id=created[0].id,
        summary=f"Vitals recorded for {appt.patient.name if appt.patient else appt.patient_id}: "
        f"{', '.join(r.name for r in created)} → Checked in",
    )
    db.commit()

    rows = (
        db.query(VitalReading)
        .options(
            joinedload(VitalReading.patient),
            joinedload(VitalReading.appointment).joinedload(Appointment.doctor),
        )
        .filter(VitalReading.id.in_([r.id for r in created]))
        .all()
    )
    return [_reading_response(r) for r in rows]


def _load_vital(db: Session, hospital_id: UUID, vital_id: UUID) -> VitalReading:
    row = (
        db.query(VitalReading)
        .options(
            joinedload(VitalReading.patient),
            joinedload(VitalReading.appointment).joinedload(Appointment.doctor),
        )
        .filter(VitalReading.id == vital_id, VitalReading.hospital_id == hospital_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vital reading not found")
    return row


@router.put("/{vital_id}", response_model=VitalReadingResponse)
def update_vital(
    vital_id: UUID,
    payload: VitalItemUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = _load_vital(db, hospital_id, vital_id)
    appt = row.appointment
    if not appt or appt.hospital_id != hospital_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    _assert_can_mutate_vitals(appt)

    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.result is not None:
        row.result = payload.result.strip()
    if payload.suitable_range is not None:
        row.suitable_range = payload.suitable_range.strip()
    if not row.name or not row.result:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Each vital needs name and result")
    row.recorded_by_name = str(user.get("name") or row.recorded_by_name or "Staff")
    row.recorded_at = datetime.now(timezone.utc)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="vital_reading",
        entity_id=row.id,
        summary=f"Updated vital {row.name} for {row.patient.name if row.patient else row.patient_id}",
    )
    db.commit()
    db.refresh(row)
    return _reading_response(_load_vital(db, hospital_id, row.id))


@router.delete("/{vital_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vital(
    vital_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    row = _load_vital(db, hospital_id, vital_id)
    appt = row.appointment
    if not appt or appt.hospital_id != hospital_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    _assert_can_mutate_vitals(appt)

    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="delete",
        entity_type="vital_reading",
        entity_id=row.id,
        summary=f"Deleted vital {row.name} for {row.patient.name if row.patient else row.patient_id}",
    )
    db.delete(row)
    db.commit()
    return None
