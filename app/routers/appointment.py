from datetime import date, datetime, time, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    Holiday,
    HospitalUser,
    Patient,
)
from app.schemas_appointment import (
    AppointmentListItem,
    BookAppointmentRequest,
    DoctorAvailability,
    QueueGroup,
    RescheduleRequest,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _is_doctor(user: HospitalUser) -> bool:
    return bool(user.role and "doctor" in (user.role.name or "").lower())


def _to_item(a: Appointment) -> AppointmentListItem:
    return AppointmentListItem(
        id=a.id,
        hospital_id=a.hospital_id,
        doctor_id=a.doctor_id,
        patient_id=a.patient_id,
        appointment_date=a.appointment_date,
        appointment_time=a.appointment_time,
        purpose=a.purpose,
        visit_type=getattr(a, "visit_type", None) or "OPD",
        status=a.status,
        notes=a.notes,
        queue_token=getattr(a, "queue_token", None),
        checked_in_at=getattr(a, "checked_in_at", None),
        created_at=a.created_at,
        patient_name=a.patient.name if a.patient else None,
        patient_uhid=getattr(a.patient, "uhid", None) if a.patient else None,
        patient_mobile=a.patient.mobile if a.patient else None,
        doctor_name=a.doctor.name if a.doctor else None,
    )


def _load_appt(db: Session, appt_id: UUID, hospital_id: UUID) -> Appointment:
    appt = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(Appointment.id == appt_id, Appointment.hospital_id == hospital_id)
        .first()
    )
    if not appt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    return appt


def _check_holiday(db: Session, hospital_id: UUID, on_date: date) -> Holiday | None:
    return (
        db.query(Holiday)
        .filter(Holiday.hospital_id == hospital_id, Holiday.holiday_date == on_date)
        .first()
    )


def _slot_conflict(
    db: Session,
    hospital_id: UUID,
    doctor_id: UUID,
    on_date: date,
    on_time: time,
    exclude_id: UUID | None = None,
) -> bool:
    q = db.query(Appointment.id).filter(
        Appointment.hospital_id == hospital_id,
        Appointment.doctor_id == doctor_id,
        Appointment.appointment_date == on_date,
        Appointment.appointment_time == on_time,
        Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
    )
    if exclude_id:
        q = q.filter(Appointment.id != exclude_id)
    return q.first() is not None


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
        {"id": str(d.id), "name": d.name, "phone": d.phone, "email": d.email}
        for d in users
        if _is_doctor(d)
    ]


@router.get("/visit-types")
def list_visit_types(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    types = (
        db.query(AppointmentType)
        .filter(AppointmentType.hospital_id == hospital_id, AppointmentType.is_active.is_(True))
        .order_by(AppointmentType.name.asc())
        .all()
    )
    if not types:
        return [{"name": "OPD", "slot_duration_minutes": 15}, {"name": "Follow-up", "slot_duration_minutes": 10}, {"name": "Emergency", "slot_duration_minutes": 20}]
    return [{"name": t.name, "slot_duration_minutes": t.slot_duration_minutes, "id": str(t.id)} for t in types]


@router.get("/availability", response_model=DoctorAvailability)
def check_doctor_availability(
    doctor_id: UUID = Query(...),
    on_date: date = Query(..., alias="date"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    doctor = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.id == doctor_id, HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .first()
    )
    if not doctor or not _is_doctor(doctor):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

    holiday = _check_holiday(db, hospital_id, on_date)
    if holiday:
        return DoctorAvailability(
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            date=on_date,
            available=False,
            reason=f"Hospital holiday: {holiday.name}",
            booked_slots=[],
        )

    booked = (
        db.query(Appointment)
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.doctor_id == doctor_id,
            Appointment.appointment_date == on_date,
            Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
        )
        .order_by(Appointment.appointment_time.asc())
        .all()
    )
    slots = [a.appointment_time.strftime("%H:%M") for a in booked]
    return DoctorAvailability(
        doctor_id=doctor.id,
        doctor_name=doctor.name,
        date=on_date,
        available=True,
        reason=None,
        booked_slots=slots,
    )


@router.post("", response_model=AppointmentListItem, status_code=status.HTTP_201_CREATED)
def book_appointment(
    payload: BookAppointmentRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = db.query(Patient).filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    doctor = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.id == payload.doctor_id, HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .first()
    )
    if not doctor or not _is_doctor(doctor):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

    holiday = _check_holiday(db, hospital_id, payload.appointment_date)
    if holiday:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot book on holiday: {holiday.name}",
        )

    if _slot_conflict(db, hospital_id, payload.doctor_id, payload.appointment_date, payload.appointment_time):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Doctor already has an appointment at this time slot",
        )

    purpose = (payload.purpose or payload.visit_type or "Consultation").strip()
    appt = Appointment(
        hospital_id=hospital_id,
        doctor_id=payload.doctor_id,
        patient_id=payload.patient_id,
        appointment_date=payload.appointment_date,
        appointment_time=payload.appointment_time,
        purpose=purpose,
        visit_type=payload.visit_type.strip(),
        status=AppointmentStatus.scheduled,
        notes=payload.notes.strip() if payload.notes else None,
    )
    db.add(appt)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="appointment",
        entity_id=appt.id,
        summary=f"Booked {patient.name} with {doctor.name} on {payload.appointment_date} {payload.appointment_time}",
    )
    db.commit()
    return _to_item(_load_appt(db, appt.id, hospital_id))


@router.get("/today", response_model=list[AppointmentListItem])
def todays_appointments(
    doctor_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = date.today()
    q = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(Appointment.hospital_id == hospital_id, Appointment.appointment_date == today)
    )
    if doctor_id:
        q = q.filter(Appointment.doctor_id == doctor_id)
    rows = q.order_by(Appointment.appointment_time.asc()).all()
    return [_to_item(a) for a in rows]


@router.get("/calendar", response_model=list[AppointmentListItem])
def calendar_view(
    date_from: date = Query(...),
    date_to: date = Query(...),
    doctor_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="date_to must be on or after date_from")
    if (date_to - date_from).days > 62:
        raise HTTPException(status_code=400, detail="Range cannot exceed 62 days")

    q = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date >= date_from,
            Appointment.appointment_date <= date_to,
            Appointment.status != AppointmentStatus.cancelled,
        )
    )
    if doctor_id:
        q = q.filter(Appointment.doctor_id == doctor_id)
    rows = q.order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc()).all()
    return [_to_item(a) for a in rows]


@router.get("/queue", response_model=list[QueueGroup])
def doctor_queue(
    on_date: date | None = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    day = on_date or date.today()
    rows = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date == day,
            Appointment.status.in_([AppointmentStatus.scheduled, AppointmentStatus.waiting]),
        )
        .order_by(Appointment.doctor_id.asc(), Appointment.queue_token.asc().nulls_last(), Appointment.appointment_time.asc())
        .all()
    )
    groups: dict[UUID, QueueGroup] = {}
    for a in rows:
        if a.doctor_id not in groups:
            groups[a.doctor_id] = QueueGroup(
                doctor_id=a.doctor_id,
                doctor_name=a.doctor.name if a.doctor else "Doctor",
                patients=[],
            )
        groups[a.doctor_id].patients.append(_to_item(a))
    return list(groups.values())


@router.get("/history", response_model=list[AppointmentListItem])
def appointment_history(
    patient: str | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    on_date: date | None = Query(default=None, alias="date"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(Appointment.hospital_id == hospital_id)
    )
    if doctor_id:
        q = q.filter(Appointment.doctor_id == doctor_id)
    if on_date:
        q = q.filter(Appointment.appointment_date == on_date)
    if date_from:
        q = q.filter(Appointment.appointment_date >= date_from)
    if date_to:
        q = q.filter(Appointment.appointment_date <= date_to)
    if patient:
        term = f"%{patient.strip()}%"
        q = q.join(Patient).filter(
            or_(Patient.name.ilike(term), Patient.uhid.ilike(term), Patient.mobile.ilike(term))
        )
    rows = q.order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc()).limit(200).all()
    return [_to_item(a) for a in rows]


@router.post("/{appointment_id}/check-in", response_model=AppointmentListItem)
def check_in(
    appointment_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    appt = _load_appt(db, appointment_id, hospital_id)
    if appt.status in {AppointmentStatus.cancelled, AppointmentStatus.completed, AppointmentStatus.no_show}:
        raise HTTPException(status_code=400, detail=f"Cannot check in appointment with status {appt.status.value}")
    if appt.status == AppointmentStatus.waiting:
        return _to_item(appt)

    appt.status = AppointmentStatus.waiting
    appt.checked_in_at = datetime.now(timezone.utc)
    if not appt.queue_token:
        appt.queue_token = _next_queue_token(db, hospital_id, appt.doctor_id, appt.appointment_date)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="appointment",
        entity_id=appt.id,
        summary=f"Checked in {appt.patient.name if appt.patient else 'patient'} (token #{appt.queue_token})",
    )
    db.commit()
    return _to_item(_load_appt(db, appointment_id, hospital_id))


@router.post("/{appointment_id}/complete", response_model=AppointmentListItem)
def complete_appointment(
    appointment_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    appt = _load_appt(db, appointment_id, hospital_id)
    if appt.status == AppointmentStatus.cancelled:
        raise HTTPException(status_code=400, detail="Cancelled appointment cannot be completed")
    appt.status = AppointmentStatus.completed
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="appointment",
        entity_id=appt.id,
        summary=f"Completed appointment for {appt.patient.name if appt.patient else 'patient'}",
    )
    db.commit()
    return _to_item(_load_appt(db, appointment_id, hospital_id))


@router.post("/{appointment_id}/cancel", response_model=AppointmentListItem)
def cancel_appointment(
    appointment_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    appt = _load_appt(db, appointment_id, hospital_id)
    if appt.status == AppointmentStatus.completed:
        raise HTTPException(status_code=400, detail="Completed appointment cannot be cancelled")
    appt.status = AppointmentStatus.cancelled
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="appointment",
        entity_id=appt.id,
        summary=f"Cancelled appointment for {appt.patient.name if appt.patient else 'patient'}",
    )
    db.commit()
    return _to_item(_load_appt(db, appointment_id, hospital_id))


@router.put("/{appointment_id}/reschedule", response_model=AppointmentListItem)
def reschedule_appointment(
    appointment_id: UUID,
    payload: RescheduleRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    appt = _load_appt(db, appointment_id, hospital_id)
    if appt.status in {AppointmentStatus.cancelled, AppointmentStatus.completed}:
        raise HTTPException(status_code=400, detail="Cannot reschedule this appointment")

    holiday = _check_holiday(db, hospital_id, payload.appointment_date)
    if holiday:
        raise HTTPException(status_code=400, detail=f"Cannot reschedule to holiday: {holiday.name}")

    if _slot_conflict(db, hospital_id, appt.doctor_id, payload.appointment_date, payload.appointment_time, appt.id):
        raise HTTPException(status_code=409, detail="Doctor already has an appointment at this time slot")

    appt.appointment_date = payload.appointment_date
    appt.appointment_time = payload.appointment_time
    appt.status = AppointmentStatus.scheduled
    appt.queue_token = None
    appt.checked_in_at = None
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="appointment",
        entity_id=appt.id,
        summary=f"Rescheduled to {payload.appointment_date} {payload.appointment_time}",
    )
    db.commit()
    return _to_item(_load_appt(db, appointment_id, hospital_id))
