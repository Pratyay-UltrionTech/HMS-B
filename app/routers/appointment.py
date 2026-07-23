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
    ConsultationPricing,
    Department,
    Holiday,
    HospitalUser,
    Patient,
    PatientStatus,
    Wing,
)
from app.routers.registration import _age_from_dob, _display_name, _next_uhid
from app.schemas_appointment import (
    AppointmentListItem,
    BookAppointmentRequest,
    DoctorAvailability,
    FeePreviewResponse,
    LeaveBlock,
    QueueGroup,
    RescheduleRequest,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user
from app.utils.doctor_leave import (
    fmt_time_hhmm,
    get_shift_bounds,
    leave_for_slot,
    list_leaves_for_date,
    slot_duration_minutes,
)

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _is_doctor(user: HospitalUser) -> bool:
    return bool(user.role and "doctor" in (user.role.name or "").lower())


def _doctor_fallback_fee(doctor: HospitalUser) -> float:
    cv = doctor.custom_values or {}
    for key in ("consultation_fee", "consultationFee", "fee", "consult_fee", "Consultation Fee"):
        val = cv.get(key)
        if val is None or val == "":
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return 0.0


def _is_follow_up_type(appt_type: AppointmentType | None) -> bool:
    if not appt_type:
        return False
    if getattr(appt_type, "is_follow_up", False):
        return True
    normalized = (appt_type.name or "").lower().replace(" ", "").replace("-", "").replace("_", "")
    return "followup" in normalized or normalized in ("fu", "follow")


def _find_pricing(
    db: Session,
    hospital_id: UUID,
    wing_id: UUID,
    department_id: UUID,
    doctor_id: UUID,
    appointment_type_id: UUID,
) -> ConsultationPricing | None:
    return (
        db.query(ConsultationPricing)
        .filter(
            ConsultationPricing.hospital_id == hospital_id,
            ConsultationPricing.wing_id == wing_id,
            ConsultationPricing.department_id == department_id,
            ConsultationPricing.doctor_id == doctor_id,
            ConsultationPricing.appointment_type_id == appointment_type_id,
            ConsultationPricing.is_active.is_(True),
        )
        .first()
    )


def _last_completed_visit(
    db: Session,
    hospital_id: UUID,
    patient_id: UUID,
    doctor_id: UUID,
    before_date: date,
) -> Appointment | None:
    return (
        db.query(Appointment)
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.patient_id == patient_id,
            Appointment.doctor_id == doctor_id,
            Appointment.status == AppointmentStatus.completed,
            Appointment.appointment_date <= before_date,
        )
        .order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
        .first()
    )


def resolve_consultation_fee(
    db: Session,
    hospital_id: UUID,
    *,
    doctor: HospitalUser,
    appt_type: AppointmentType | None,
    wing_id: UUID | None,
    department_id: UUID | None,
    patient_id: UUID | None,
    appointment_date: date,
) -> dict:
    """Resolve fee + follow-up eligibility. Returns snapshot-ready values."""
    pricing = None
    if wing_id and department_id and appt_type:
        pricing = _find_pricing(db, hospital_id, wing_id, department_id, doctor.id, appt_type.id)

    base_fee = float(pricing.consultation_fee) if pricing else _doctor_fallback_fee(doctor)
    followup_free_days = pricing.followup_free_days if pricing else None
    is_follow_up = _is_follow_up_type(appt_type)

    eligibility: str | None = None
    message: str | None = None
    last_visit_date: date | None = None
    final_fee = base_fee

    if is_follow_up:
        if not patient_id:
            eligibility = "no_prior_visit"
            message = "Follow-up selected — prior completed visit required to check free eligibility"
        else:
            prior = _last_completed_visit(db, hospital_id, patient_id, doctor.id, appointment_date)
            if prior is None:
                eligibility = "no_prior_visit"
                message = "No prior completed visit with this doctor — standard fee applies"
            else:
                last_visit_date = prior.appointment_date
                if followup_free_days is not None:
                    days_since = (appointment_date - prior.appointment_date).days
                    if days_since <= followup_free_days:
                        final_fee = 0.0
                        eligibility = "eligible"
                        message = f"Eligible for free follow-up (within {followup_free_days} days)"
                    else:
                        eligibility = "expired"
                        message = f"Follow-up period expired ({days_since} days since last visit; free within {followup_free_days} days)"
                else:
                    eligibility = "no_free_period"
                    message = "Follow-up type — free period not configured; standard fee applies"

    return {
        "consultation_fee": final_fee,
        "base_fee": base_fee,
        "pricing_found": pricing is not None,
        "followup_free_days": followup_free_days,
        "is_follow_up": is_follow_up,
        "followup_eligibility": eligibility,
        "followup_message": message,
        "last_completed_visit_date": last_visit_date,
        "appointment_type_name": appt_type.name if appt_type else None,
        "doctor_name": doctor.name,
    }


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
        appointment_type_id=getattr(a, "appointment_type_id", None),
        wing_id=getattr(a, "wing_id", None),
        department_id=getattr(a, "department_id", None),
        consultation_fee=float(getattr(a, "consultation_fee", None) or 0),
        followup_eligibility=getattr(a, "followup_eligibility", None),
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


@router.get("/wings")
def list_booking_wings(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    rows = (
        db.query(Wing)
        .filter(Wing.hospital_id == hospital_id, Wing.is_active.is_(True))
        .order_by(Wing.name.asc())
        .all()
    )
    return [{"id": str(w.id), "name": w.name} for w in rows]


@router.get("/departments")
def list_booking_departments(
    wing_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    q = db.query(Department).filter(Department.hospital_id == hospital_id, Department.is_active.is_(True))
    if wing_id:
        q = q.filter(or_(Department.wing_id == wing_id, Department.wing_id.is_(None)))
    rows = q.order_by(Department.name.asc()).all()
    return [{"id": str(d.id), "name": d.name, "wing_id": str(d.wing_id) if d.wing_id else None} for d in rows]


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
        return [
            {"name": "OPD", "slot_duration_minutes": 15, "is_follow_up": False},
            {"name": "Follow-up", "slot_duration_minutes": 10, "is_follow_up": True},
            {"name": "Emergency", "slot_duration_minutes": 20, "is_follow_up": False},
        ]
    return [
        {
            "name": t.name,
            "slot_duration_minutes": t.slot_duration_minutes,
            "id": str(t.id),
            "is_follow_up": bool(getattr(t, "is_follow_up", False) or _is_follow_up_type(t)),
        }
        for t in types
    ]


@router.get("/fee-preview", response_model=FeePreviewResponse)
def fee_preview(
    doctor_id: UUID = Query(...),
    appointment_type_id: UUID = Query(...),
    wing_id: UUID = Query(...),
    department_id: UUID = Query(...),
    patient_id: UUID | None = Query(default=None),
    appointment_date: date | None = Query(default=None, alias="date"),
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

    appt_type = (
        db.query(AppointmentType)
        .filter(
            AppointmentType.id == appointment_type_id,
            AppointmentType.hospital_id == hospital_id,
            AppointmentType.is_active.is_(True),
        )
        .first()
    )
    if not appt_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment type not found")

    wing = db.query(Wing).filter(Wing.id == wing_id, Wing.hospital_id == hospital_id).first()
    if not wing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wing not found")
    dept = (
        db.query(Department)
        .filter(Department.id == department_id, Department.hospital_id == hospital_id)
        .first()
    )
    if not dept:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    if patient_id:
        patient = (
            db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    on_date = appointment_date or date.today()
    result = resolve_consultation_fee(
        db,
        hospital_id,
        doctor=doctor,
        appt_type=appt_type,
        wing_id=wing_id,
        department_id=department_id,
        patient_id=patient_id,
        appointment_date=on_date,
    )
    return FeePreviewResponse(**result)


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
        .options(joinedload(HospitalUser.role), joinedload(HospitalUser.shift))
        .filter(HospitalUser.id == doctor_id, HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .first()
    )
    if not doctor or not _is_doctor(doctor):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

    shift_start, shift_end, _ = get_shift_bounds(doctor)

    holiday = _check_holiday(db, hospital_id, on_date)
    if holiday:
        return DoctorAvailability(
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            date=on_date,
            available=False,
            reason=f"Hospital holiday: {holiday.name}",
            booked_slots=[],
            leave_blocks=[],
            shift_start=fmt_time_hhmm(shift_start),
            shift_end=fmt_time_hhmm(shift_end),
        )

    leaves = list_leaves_for_date(db, hospital_id, doctor_id, on_date)
    leave_blocks = [
        LeaveBlock(start=fmt_time_hhmm(lv.start_time), end=fmt_time_hhmm(lv.end_time), reason=lv.reason)
        for lv in leaves
    ]

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
        leave_blocks=leave_blocks,
        shift_start=fmt_time_hhmm(shift_start),
        shift_end=fmt_time_hhmm(shift_end),
    )


def _resolve_or_register_patient(
    db: Session,
    hospital_id: UUID,
    user: dict,
    payload: BookAppointmentRequest,
) -> Patient:
    """Use existing patient_id, or find/create by mobile from new-patient fields."""
    if payload.patient_id is not None:
        patient = (
            db.query(Patient)
            .filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id)
            .first()
        )
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
        return patient

    mobile = (payload.mobile or "").strip()
    existing = (
        db.query(Patient)
        .filter(Patient.hospital_id == hospital_id, Patient.mobile == mobile)
        .first()
    )
    if existing:
        return existing

    first = (payload.first_name or "").strip()
    last = (payload.last_name or "").strip()
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
        gender=(payload.gender or "").strip(),
        address=payload.address.strip() if payload.address else None,
        emergency_contact=payload.emergency_contact.strip() if payload.emergency_contact else None,
        blood_group=payload.blood_group,
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
        summary=f"Auto-registered patient {patient.uhid} {patient.name} via appointment booking",
    )
    return patient


@router.post("", response_model=AppointmentListItem, status_code=status.HTTP_201_CREATED)
def book_appointment(
    payload: BookAppointmentRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = _resolve_or_register_patient(db, hospital_id, user, payload)

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

    slot_min = slot_duration_minutes(db, hospital_id)
    on_leave = leave_for_slot(
        db, hospital_id, payload.doctor_id, payload.appointment_date, payload.appointment_time, slot_min
    )
    if on_leave:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Doctor is on leave from {fmt_time_hhmm(on_leave.start_time)} to {fmt_time_hhmm(on_leave.end_time)}",
        )

    purpose = (payload.purpose or payload.visit_type or "Consultation").strip()

    appt_type: AppointmentType | None = None
    if payload.appointment_type_id:
        appt_type = (
            db.query(AppointmentType)
            .filter(
                AppointmentType.id == payload.appointment_type_id,
                AppointmentType.hospital_id == hospital_id,
            )
            .first()
        )
        if not appt_type:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Appointment type not found")
    else:
        # Resolve by visit_type name for backward compatibility
        appt_type = (
            db.query(AppointmentType)
            .filter(
                AppointmentType.hospital_id == hospital_id,
                AppointmentType.name == payload.visit_type.strip(),
                AppointmentType.is_active.is_(True),
            )
            .first()
        )

    wing_id = payload.wing_id
    department_id = payload.department_id
    if wing_id:
        wing = db.query(Wing).filter(Wing.id == wing_id, Wing.hospital_id == hospital_id).first()
        if not wing:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wing not found")
    if department_id:
        dept = (
            db.query(Department)
            .filter(Department.id == department_id, Department.hospital_id == hospital_id)
            .first()
        )
        if not dept:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Department not found")

    fee_info = resolve_consultation_fee(
        db,
        hospital_id,
        doctor=doctor,
        appt_type=appt_type,
        wing_id=wing_id,
        department_id=department_id,
        patient_id=patient.id,
        appointment_date=payload.appointment_date,
    )

    visit_type_label = (appt_type.name if appt_type else payload.visit_type).strip()
    appt = Appointment(
        hospital_id=hospital_id,
        doctor_id=payload.doctor_id,
        patient_id=patient.id,
        appointment_date=payload.appointment_date,
        appointment_time=payload.appointment_time,
        purpose=purpose if payload.purpose else visit_type_label,
        visit_type=visit_type_label,
        appointment_type_id=appt_type.id if appt_type else None,
        wing_id=wing_id,
        department_id=department_id,
        consultation_fee=float(fee_info["consultation_fee"]),
        followup_eligibility=fee_info.get("followup_eligibility"),
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
        summary=(
            f"Booked {patient.name} with {doctor.name} on {payload.appointment_date} "
            f"{payload.appointment_time} fee={appt.consultation_fee}"
        ),
    )
    from app.models import BillingSourceType
    from app.utils.billing import ensure_charge

    ensure_charge(
        db,
        hospital_id=hospital_id,
        patient_id=patient.id,
        source_type=BillingSourceType.consultation,
        source_id=appt.id,
        description=f"Consultation — {doctor.name} ({visit_type_label})",
        charge_amount=float(appt.consultation_fee or 0),
        created_by_name=str(user.get("name") or "Staff"),
        notes=fee_info.get("followup_eligibility"),
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

    # Scheduled → In Progress (stored as waiting)
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
        summary=f"Checked in {appt.patient.name if appt.patient else 'patient'} → In Progress (token #{appt.queue_token})",
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
    from app.utils.appointment_lifecycle import complete_appointment_record, status_display_label

    appt = _load_appt(db, appointment_id, hospital_id)
    ok, blockers = complete_appointment_record(db, hospital_id, appt)
    if not ok:
        detail = blockers[0] if len(blockers) == 1 else "Cannot complete yet: " + "; ".join(blockers)
        raise HTTPException(status_code=400, detail=detail)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="appointment",
        entity_id=appt.id,
        summary=f"Completed appointment for {appt.patient.name if appt.patient else 'patient'} ({status_display_label(appt.status)})",
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
    if appt.status in {AppointmentStatus.completed, AppointmentStatus.no_show}:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel appointment with status {appt.status.value}",
        )
    appt.status = AppointmentStatus.cancelled
    from app.models import BillingSourceType
    from app.utils.billing import cancel_charge_for_source

    cancel_charge_for_source(db, hospital_id, BillingSourceType.consultation, appt.id)
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


@router.post("/{appointment_id}/no-show", response_model=AppointmentListItem)
def mark_no_show(
    appointment_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    """Staff action: mark as No Show. Does not auto-apply — requires review."""
    appt = _load_appt(db, appointment_id, hospital_id)
    if appt.status != AppointmentStatus.scheduled:
        raise HTTPException(
            status_code=400,
            detail="Only Scheduled appointments (not checked in) can be marked No Show",
        )
    appt.status = AppointmentStatus.no_show
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="appointment",
        entity_id=appt.id,
        summary=f"Marked no-show for {appt.patient.name if appt.patient else 'patient'}",
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
    if appt.status in {AppointmentStatus.cancelled, AppointmentStatus.completed, AppointmentStatus.no_show}:
        raise HTTPException(status_code=400, detail="Cannot reschedule this appointment")

    holiday = _check_holiday(db, hospital_id, payload.appointment_date)
    if holiday:
        raise HTTPException(status_code=400, detail=f"Cannot reschedule to holiday: {holiday.name}")

    if _slot_conflict(db, hospital_id, appt.doctor_id, payload.appointment_date, payload.appointment_time, appt.id):
        raise HTTPException(status_code=409, detail="Doctor already has an appointment at this time slot")

    slot_min = slot_duration_minutes(db, hospital_id)
    on_leave = leave_for_slot(db, hospital_id, appt.doctor_id, payload.appointment_date, payload.appointment_time, slot_min)
    if on_leave:
        raise HTTPException(
            status_code=409,
            detail=f"Doctor is on leave from {fmt_time_hhmm(on_leave.start_time)} to {fmt_time_hhmm(on_leave.end_time)}",
        )

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
