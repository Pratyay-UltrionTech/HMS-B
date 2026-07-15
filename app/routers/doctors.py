from datetime import date, timedelta
from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.schemas_doctors import (
    AppointmentCreate,
    AppointmentResponse,
    AppointmentUpdate,
    DoctorSummary,
    HospitalClinicProfile,
    MedicalRecordCreate,
    MedicalRecordResponse,
    PatientCreate,
    PatientHistoryResponse,
    PatientResponse,
    PatientUpdate,
    PrescriptionCreate,
    PrescriptionResponse,
)
from app.routers.laboratory import _order_to_response as _lab_order_to_response
from app.routers.radiology import _order_to_response as _rad_order_to_response
from app.routers.ot import _surgery_to_response as _ot_surgery_to_response
from app.models import (
    Appointment,
    AppointmentStatus,
    Hospital,
    HospitalUser,
    LabOrder,
    MedicalRecord,
    OtSurgery,
    Patient,
    PatientStatus,
    Prescription,
    RadiologyOrder,
)
from app.utils.audit import write_audit
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/doctors", tags=["doctors"])


def _is_doctor_role(name: str | None) -> bool:
    return bool(name and "doctor" in name.lower())


def _resolve_doctor_id(user: dict, doctor_id: UUID | None, hospital_id: UUID, db: Session) -> UUID:
    """Staff may only access their own id. Admins may pick any doctor in the hospital."""
    if user.get("role") == "hospital_staff":
        try:
            own_id = UUID(str(user["user_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Staff identity missing") from exc
        if doctor_id and doctor_id != own_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only view your own records")
        return own_id

    if not doctor_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="doctor_id is required")
    doctor = (
        db.query(HospitalUser)
        .filter(HospitalUser.id == doctor_id, HospitalUser.hospital_id == hospital_id)
        .first()
    )
    if not doctor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")
    return doctor_id


def _get_doctor(db: Session, doctor_id: UUID, hospital_id: UUID) -> HospitalUser:
    doctor = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.id == doctor_id, HospitalUser.hospital_id == hospital_id)
        .first()
    )
    if not doctor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")
    return doctor


def _patient_response(p: Patient, last_visit: date | None = None, last_diagnosis: str | None = None) -> PatientResponse:
    return PatientResponse(
        id=p.id,
        hospital_id=p.hospital_id,
        name=p.name,
        mobile=p.mobile,
        age=p.age,
        gender=p.gender,
        address=p.address,
        created_at=p.created_at,
        last_visit=last_visit,
        last_diagnosis=last_diagnosis,
        uhid=getattr(p, "uhid", None),
    )


def _next_uhid(db: Session, hospital_id: UUID) -> str:
    count = db.query(func.count(Patient.id)).filter(Patient.hospital_id == hospital_id).scalar() or 0
    for i in range(1, 100_000):
        uhid = f"P{count + i:04d}"
        if not db.query(Patient.id).filter(Patient.hospital_id == hospital_id, Patient.uhid == uhid).first():
            return uhid
    raise HTTPException(status_code=500, detail="Unable to generate UHID")


def _split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _appointment_response(a: Appointment) -> AppointmentResponse:
    return AppointmentResponse(
        id=a.id,
        hospital_id=a.hospital_id,
        doctor_id=a.doctor_id,
        patient_id=a.patient_id,
        appointment_date=a.appointment_date,
        appointment_time=a.appointment_time,
        purpose=a.purpose,
        status=a.status,
        notes=a.notes,
        created_at=a.created_at,
        patient_name=a.patient.name if a.patient else None,
        patient_mobile=a.patient.mobile if a.patient else None,
        doctor_name=a.doctor.name if a.doctor else None,
    )


def _prescription_response(p: Prescription) -> PrescriptionResponse:
    return PrescriptionResponse(
        id=p.id,
        hospital_id=p.hospital_id,
        doctor_id=p.doctor_id,
        patient_id=p.patient_id,
        appointment_id=p.appointment_id,
        symptoms=p.symptoms,
        diagnosis=p.diagnosis,
        medicines=p.medicines,
        dosage=p.dosage,
        advice=p.advice,
        follow_up_date=p.follow_up_date,
        created_at=p.created_at,
        patient_name=p.patient.name if p.patient else None,
        patient_mobile=p.patient.mobile if p.patient else None,
        doctor_name=p.doctor.name if p.doctor else None,
    )


def _record_response(r: MedicalRecord) -> MedicalRecordResponse:
    return MedicalRecordResponse(
        id=r.id,
        hospital_id=r.hospital_id,
        doctor_id=r.doctor_id,
        patient_id=r.patient_id,
        report_type=r.report_type,
        title=r.title,
        notes=r.notes,
        file_name=r.file_name,
        has_file=bool(r.file_data),
        created_at=r.created_at,
        patient_name=r.patient.name if r.patient else None,
        doctor_name=r.doctor.name if r.doctor else None,
    )


# ── Doctors list ───────────────────────────────────────────────────────────────

@router.get("", response_model=list[DoctorSummary])
def list_doctors(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = date.today()
    q = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
    )

    if user.get("role") == "hospital_staff":
        own_id = UUID(str(user["user_id"]))
        q = q.filter(HospitalUser.id == own_id)
    else:
        # Admin: all staff whose role name contains "doctor"
        doctors = [u for u in q.all() if _is_doctor_role(u.role.name if u.role else None)]
        return [_doctor_summary(db, d, today) for d in doctors]

    users = q.all()
    return [_doctor_summary(db, d, today) for d in users]


@router.get("/hospital-profile", response_model=HospitalClinicProfile)
def hospital_profile(
    db: Session = Depends(get_db),
    _: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    if not hospital:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")
    return HospitalClinicProfile(
        id=hospital.id,
        hospital_id=hospital.hospital_id,
        name=hospital.name,
        address=hospital.address,
        phone=hospital.phone,
        email=hospital.email,
        slogan="Caring for life",
        website=None,
    )


def _doctor_summary(db: Session, doctor: HospitalUser, today: date) -> DoctorSummary:
    patient_ids = (
        db.query(Appointment.patient_id)
        .filter(Appointment.doctor_id == doctor.id, Appointment.hospital_id == doctor.hospital_id)
        .distinct()
        .all()
    )
    today_count = (
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.doctor_id == doctor.id,
            Appointment.hospital_id == doctor.hospital_id,
            Appointment.appointment_date == today,
            Appointment.status != AppointmentStatus.cancelled,
        )
        .scalar()
        or 0
    )
    return DoctorSummary(
        id=doctor.id,
        name=doctor.name,
        email=doctor.email,
        phone=doctor.phone,
        role_name=doctor.role.name if doctor.role else None,
        custom_values=doctor.custom_values or {},
        is_active=doctor.is_active,
        patient_count=len(patient_ids),
        today_appointment_count=int(today_count),
    )


# ── Patients ───────────────────────────────────────────────────────────────────

@router.get("/patients/search", response_model=list[PatientResponse])
def search_patients(
    q: str = Query(min_length=1),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    term = f"%{q.strip()}%"
    patients = (
        db.query(Patient)
        .filter(Patient.hospital_id == hospital_id, or_(Patient.name.ilike(term), Patient.mobile.ilike(term)))
        .order_by(Patient.name.asc())
        .limit(30)
        .all()
    )
    return [_patient_response(p) for p in patients]


@router.put("/patients/{patient_id}", response_model=PatientResponse)
def update_patient(
    patient_id: UUID,
    payload: PatientUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        patient.name = data["name"].strip()
    if "mobile" in data and data["mobile"]:
        patient.mobile = data["mobile"].strip()
    if "age" in data:
        patient.age = data["age"]
    if "gender" in data:
        patient.gender = data["gender"].strip() if data["gender"] else None
    if "address" in data:
        patient.address = data["address"].strip() if data["address"] else None
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="patient",
        entity_id=patient.id,
        summary=f"Updated patient {patient.name}",
    )
    db.commit()
    db.refresh(patient)
    return _patient_response(patient)


@router.post("/patients", response_model=PatientResponse, status_code=status.HTTP_201_CREATED)
def create_patient(
    payload: PatientCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    mobile = payload.mobile.strip()
    existing = (
        db.query(Patient)
        .filter(Patient.hospital_id == hospital_id, Patient.mobile == mobile)
        .first()
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Patient with this mobile already exists")

    first, last = _split_name(payload.name)
    patient = Patient(
        hospital_id=hospital_id,
        uhid=_next_uhid(db, hospital_id),
        first_name=first,
        last_name=last,
        name=payload.name.strip(),
        mobile=mobile,
        age=payload.age,
        gender=payload.gender.strip() if payload.gender else None,
        address=payload.address.strip() if payload.address else None,
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
        summary=f"Created patient {patient.name}",
    )
    db.commit()
    db.refresh(patient)
    return _patient_response(patient)


@router.get("/{doctor_id}/patients", response_model=list[PatientResponse])
def list_doctor_patients(
    doctor_id: UUID,
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    patient_ids_q = (
        db.query(Appointment.patient_id)
        .filter(Appointment.doctor_id == resolved, Appointment.hospital_id == hospital_id)
        .distinct()
    )
    # Also include patients this doctor prescribed for (even without appointment)
    rx_ids = (
        db.query(Prescription.patient_id)
        .filter(Prescription.doctor_id == resolved, Prescription.hospital_id == hospital_id)
        .distinct()
    )
    ids = {row[0] for row in patient_ids_q.all()} | {row[0] for row in rx_ids.all()}
    if not ids:
        return []

    q = db.query(Patient).filter(Patient.hospital_id == hospital_id, Patient.id.in_(ids))
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(or_(Patient.name.ilike(term), Patient.mobile.ilike(term)))
    patients = q.order_by(Patient.name.asc()).all()

    results: list[PatientResponse] = []
    for p in patients:
        last_appt = (
            db.query(Appointment)
            .filter(
                Appointment.patient_id == p.id,
                Appointment.doctor_id == resolved,
                Appointment.status != AppointmentStatus.cancelled,
            )
            .order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
            .first()
        )
        last_rx = (
            db.query(Prescription)
            .filter(Prescription.patient_id == p.id, Prescription.doctor_id == resolved)
            .order_by(Prescription.created_at.desc())
            .first()
        )
        results.append(
            _patient_response(
                p,
                last_visit=last_appt.appointment_date if last_appt else None,
                last_diagnosis=last_rx.diagnosis if last_rx else None,
            )
        )
    return results


@router.get("/{doctor_id}/patients/{patient_id}", response_model=PatientHistoryResponse)
def get_patient_history(
    doctor_id: UUID,
    patient_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    # Ensure this doctor has treated the patient (or admin viewing)
    linked = (
        db.query(Appointment.id)
        .filter(Appointment.doctor_id == resolved, Appointment.patient_id == patient_id)
        .first()
        or db.query(Prescription.id)
        .filter(Prescription.doctor_id == resolved, Prescription.patient_id == patient_id)
        .first()
        or db.query(MedicalRecord.id)
        .filter(MedicalRecord.doctor_id == resolved, MedicalRecord.patient_id == patient_id)
        .first()
        or db.query(LabOrder.id)
        .filter(LabOrder.doctor_id == resolved, LabOrder.patient_id == patient_id)
        .first()
        or db.query(RadiologyOrder.id)
        .filter(RadiologyOrder.doctor_id == resolved, RadiologyOrder.patient_id == patient_id)
        .first()
        or db.query(OtSurgery.id)
        .filter(OtSurgery.surgeon_id == resolved, OtSurgery.patient_id == patient_id)
        .first()
    )
    if not linked and user.get("role") != "hospital_admin":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found for this doctor")

    appointments = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(Appointment.doctor_id == resolved, Appointment.patient_id == patient_id)
        .order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
        .all()
    )
    prescriptions = (
        db.query(Prescription)
        .options(joinedload(Prescription.patient), joinedload(Prescription.doctor))
        .filter(Prescription.doctor_id == resolved, Prescription.patient_id == patient_id)
        .order_by(Prescription.created_at.desc())
        .all()
    )
    records = (
        db.query(MedicalRecord)
        .options(joinedload(MedicalRecord.patient), joinedload(MedicalRecord.doctor))
        .filter(MedicalRecord.doctor_id == resolved, MedicalRecord.patient_id == patient_id)
        .order_by(MedicalRecord.created_at.desc())
        .all()
    )
    lab_orders = (
        db.query(LabOrder)
        .options(
            joinedload(LabOrder.patient),
            joinedload(LabOrder.doctor),
            joinedload(LabOrder.items),
            joinedload(LabOrder.results),
        )
        .filter(LabOrder.hospital_id == hospital_id, LabOrder.patient_id == patient_id)
        .order_by(LabOrder.ordered_at.desc())
        .all()
    )
    radiology_orders = (
        db.query(RadiologyOrder)
        .options(joinedload(RadiologyOrder.patient), joinedload(RadiologyOrder.doctor))
        .filter(RadiologyOrder.hospital_id == hospital_id, RadiologyOrder.patient_id == patient_id)
        .order_by(RadiologyOrder.ordered_at.desc())
        .all()
    )
    ot_surgeries = (
        db.query(OtSurgery)
        .options(joinedload(OtSurgery.patient), joinedload(OtSurgery.surgeon))
        .filter(OtSurgery.hospital_id == hospital_id, OtSurgery.patient_id == patient_id)
        .order_by(OtSurgery.scheduled_at.desc())
        .all()
    )

    last_appt = appointments[0] if appointments else None
    last_rx = prescriptions[0] if prescriptions else None
    return PatientHistoryResponse(
        patient=_patient_response(
            patient,
            last_visit=last_appt.appointment_date if last_appt else None,
            last_diagnosis=last_rx.diagnosis if last_rx else None,
        ),
        appointments=[_appointment_response(a) for a in appointments],
        prescriptions=[_prescription_response(p) for p in prescriptions],
        medical_records=[_record_response(r) for r in records],
        lab_orders=[_lab_order_to_response(o) for o in lab_orders],
        radiology_orders=[_rad_order_to_response(o) for o in radiology_orders],
        ot_surgeries=[_ot_surgery_to_response(o) for o in ot_surgeries],
    )


# ── Appointments / Calendar ────────────────────────────────────────────────────

@router.post("/{doctor_id}/appointments", response_model=AppointmentResponse, status_code=status.HTTP_201_CREATED)
def create_appointment(
    doctor_id: UUID,
    payload: AppointmentCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    _get_doctor(db, resolved, hospital_id)
    patient = db.query(Patient).filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    appt = Appointment(
        hospital_id=hospital_id,
        doctor_id=resolved,
        patient_id=payload.patient_id,
        appointment_date=payload.appointment_date,
        appointment_time=payload.appointment_time,
        purpose=payload.purpose.strip(),
        visit_type="OPD",
        status=payload.status,
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
        summary=f"Scheduled appointment for {patient.name} on {payload.appointment_date}",
    )
    db.commit()
    appt = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(Appointment.id == appt.id)
        .first()
    )
    return _appointment_response(appt)


@router.get("/{doctor_id}/appointments", response_model=list[AppointmentResponse])
def list_appointments(
    doctor_id: UUID,
    on_date: date | None = Query(default=None, alias="date"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    status_filter: AppointmentStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    q = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(Appointment.doctor_id == resolved, Appointment.hospital_id == hospital_id)
    )
    if on_date:
        q = q.filter(Appointment.appointment_date == on_date)
    if date_from:
        q = q.filter(Appointment.appointment_date >= date_from)
    if date_to:
        q = q.filter(Appointment.appointment_date <= date_to)
    if status_filter:
        q = q.filter(Appointment.status == status_filter)
    rows = q.order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc()).all()
    return [_appointment_response(a) for a in rows]


@router.get("/{doctor_id}/calendar", response_model=list[AppointmentResponse])
def get_calendar(
    doctor_id: UUID,
    week_start: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    start = week_start or (date.today() - timedelta(days=date.today().weekday()))
    end = start + timedelta(days=6)
    rows = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(
            Appointment.doctor_id == resolved,
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date >= start,
            Appointment.appointment_date <= end,
            Appointment.status != AppointmentStatus.cancelled,
        )
        .order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc())
        .all()
    )
    return [_appointment_response(a) for a in rows]


@router.put("/{doctor_id}/appointments/{appointment_id}", response_model=AppointmentResponse)
def update_appointment(
    doctor_id: UUID,
    appointment_id: UUID,
    payload: AppointmentUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    appt = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(
            Appointment.id == appointment_id,
            Appointment.doctor_id == resolved,
            Appointment.hospital_id == hospital_id,
        )
        .first()
    )
    if not appt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key == "purpose" and value:
            setattr(appt, key, value.strip())
        elif key == "notes":
            setattr(appt, key, value.strip() if value else None)
        else:
            setattr(appt, key, value)
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="update",
        entity_type="appointment",
        entity_id=appt.id,
        summary=f"Updated appointment {appointment_id}",
    )
    db.commit()
    db.refresh(appt)
    return _appointment_response(appt)


# ── Prescriptions ──────────────────────────────────────────────────────────────

@router.post("/{doctor_id}/prescriptions", response_model=PrescriptionResponse, status_code=status.HTTP_201_CREATED)
def create_prescription(
    doctor_id: UUID,
    payload: PrescriptionCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    patient = db.query(Patient).filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    if payload.appointment_id:
        appt = (
            db.query(Appointment)
            .filter(
                Appointment.id == payload.appointment_id,
                Appointment.doctor_id == resolved,
                Appointment.hospital_id == hospital_id,
            )
            .first()
        )
        if not appt:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")

    rx = Prescription(
        hospital_id=hospital_id,
        doctor_id=resolved,
        patient_id=payload.patient_id,
        appointment_id=payload.appointment_id,
        symptoms=payload.symptoms.strip(),
        diagnosis=payload.diagnosis.strip(),
        medicines=payload.medicines.strip(),
        dosage=payload.dosage.strip(),
        advice=payload.advice.strip() if payload.advice else None,
        follow_up_date=payload.follow_up_date,
    )
    db.add(rx)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="prescription",
        entity_id=rx.id,
        summary=f"Prescription for {patient.name}: {payload.diagnosis.strip()[:80]}",
    )
    db.commit()
    rx = (
        db.query(Prescription)
        .options(joinedload(Prescription.patient), joinedload(Prescription.doctor))
        .filter(Prescription.id == rx.id)
        .first()
    )
    return _prescription_response(rx)


@router.get("/{doctor_id}/prescriptions", response_model=list[PrescriptionResponse])
def list_prescriptions(
    doctor_id: UUID,
    patient_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    q = (
        db.query(Prescription)
        .options(joinedload(Prescription.patient), joinedload(Prescription.doctor))
        .filter(Prescription.doctor_id == resolved, Prescription.hospital_id == hospital_id)
    )
    if patient_id:
        q = q.filter(Prescription.patient_id == patient_id)
    rows = q.order_by(Prescription.created_at.desc()).all()
    return [_prescription_response(p) for p in rows]


@router.get("/{doctor_id}/prescriptions/{prescription_id}/pdf")
def prescription_pdf(
    doctor_id: UUID,
    prescription_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    rx = (
        db.query(Prescription)
        .options(joinedload(Prescription.patient), joinedload(Prescription.doctor))
        .filter(
            Prescription.id == prescription_id,
            Prescription.doctor_id == resolved,
            Prescription.hospital_id == hospital_id,
        )
        .first()
    )
    if not rx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prescription not found")

    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    html = _prescription_html(rx, hospital)
    # Return printable HTML; browser "Save as PDF" / print works without extra deps
    return StreamingResponse(
        BytesIO(html.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="prescription-{prescription_id}.html"'},
    )


def _prescription_html(rx: Prescription, hospital: Hospital | None = None) -> str:
    follow = rx.follow_up_date.isoformat() if rx.follow_up_date else "—"
    created = rx.created_at.strftime("%d %b %Y") if rx.created_at else ""
    doctor = rx.doctor
    patient = rx.patient
    cv = (doctor.custom_values or {}) if doctor else {}

    def cv_get(*keys: str) -> str:
        for key in keys:
            for ck, val in cv.items():
                if str(ck).strip().lower().replace(" ", "_") == key.lower().replace(" ", "_") and val not in (None, ""):
                    return str(val)
        return ""

    qualification = cv_get("qualification", "qualifications", "degree", "specialization") or "Physician"
    certification = cv_get(
        "certification",
        "registration_number",
        "registration_no",
        "medical_registration",
        "license_number",
        "reg_no",
    )
    hosp_name = hospital.name if hospital else "Hospital"
    hosp_phone = hospital.phone if hospital else ""
    hosp_email = hospital.email if hospital else ""
    hosp_address = hospital.address if hospital else ""
    cert_line = f"Certification: {certification}" if certification else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Prescription</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 0; background: #eef2f7; color: #1e293b; }}
  .pad {{ max-width: 780px; margin: 24px auto; background: #fff; border-left: 10px solid #2563eb; border-right: 10px solid #2563eb; min-height: 920px; display: flex; flex-direction: column; }}
  .header {{ display: flex; justify-content: space-between; padding: 28px 36px 16px; background: linear-gradient(135deg, #dbeafe 0%, #fff 55%); }}
  .doc-name {{ font-size: 28px; font-weight: 800; color: #1d4ed8; margin: 0; }}
  .doc-meta {{ color: #64748b; font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; margin-top: 4px; }}
  .caduceus {{ width: 64px; height: 64px; border-radius: 50%; background: #2563eb; color: #fff; display: flex; align-items: center; justify-content: center; font-size: 28px; font-weight: 700; }}
  .body {{ padding: 8px 36px 24px; flex: 1; }}
  .line {{ display: flex; align-items: baseline; gap: 8px; margin: 10px 0; font-size: 14px; }}
  .line label {{ font-weight: 600; color: #334155; white-space: nowrap; }}
  .line .fill {{ flex: 1; border-bottom: 1px solid #94a3b8; min-height: 22px; padding: 2px 4px; }}
  .row {{ display: flex; gap: 24px; }}
  .row .line {{ flex: 1; }}
  .rx {{ margin-top: 18px; position: relative; min-height: 280px; padding: 8px 8px 8px 56px; }}
  .rx-mark {{ position: absolute; left: 0; top: 0; font-size: 42px; font-weight: 800; color: #2563eb; font-family: Georgia, serif; }}
  .rx-content {{ white-space: pre-wrap; font-size: 15px; line-height: 1.6; }}
  .sign {{ margin-top: 48px; text-align: right; padding-right: 12px; }}
  .sign-line {{ display: inline-block; width: 180px; border-top: 1px solid #64748b; padding-top: 6px; font-size: 11px; letter-spacing: 0.12em; color: #64748b; text-align: center; }}
  .footer {{ margin-top: auto; background: linear-gradient(90deg, #dbeafe, #eff6ff); padding: 16px 36px; display: flex; gap: 20px; align-items: flex-start; border-top: 2px solid #93c5fd; }}
  .footer h3 {{ margin: 0; color: #1d4ed8; font-size: 16px; }}
  .footer p {{ margin: 2px 0; font-size: 12px; color: #475569; }}
  .divider {{ width: 2px; background: #60a5fa; align-self: stretch; }}
  @media print {{ body {{ background: #fff; }} .pad {{ margin: 0; max-width: none; }} }}
</style></head><body>
  <div class="pad">
    <div class="header">
      <div>
        <p class="doc-name">Dr. {doctor.name if doctor else "—"}</p>
        <p class="doc-meta">{qualification}</p>
        <p class="doc-meta">{cert_line}</p>
      </div>
      <div class="caduceus">⚕</div>
    </div>
    <div class="body">
      <div class="line"><label>Patient Name:</label><div class="fill">{patient.name if patient else "—"}</div></div>
      <div class="line"><label>Address:</label><div class="fill">{(patient.address if patient and patient.address else "—")}</div></div>
      <div class="row">
        <div class="line"><label>Age:</label><div class="fill">{patient.age if patient and patient.age is not None else "—"}</div></div>
        <div class="line"><label>Date:</label><div class="fill">{created}</div></div>
      </div>
      <div class="line"><label>Diagnosis:</label><div class="fill">{rx.diagnosis}</div></div>
      <div class="rx">
        <div class="rx-mark">℞</div>
        <div class="rx-content"><strong>Medicines:</strong>
{rx.medicines}

<strong>Dosage:</strong>
{rx.dosage}

<strong>Symptoms:</strong>
{rx.symptoms}

<strong>Advice:</strong>
{rx.advice or "—"}

<strong>Follow-up:</strong> {follow}</div>
      </div>
      <div class="sign"><div class="sign-line">SIGNATURE</div></div>
    </div>
    <div class="footer">
      <div>
        <h3>{hosp_name}</h3>
        <p>Caring for life</p>
      </div>
      <div class="divider"></div>
      <div>
        <p>☎ {hosp_phone}</p>
        <p>✉ {hosp_email}</p>
        <p>📍 {hosp_address}</p>
      </div>
    </div>
  </div>
  <script>window.onload = function() {{ window.print(); }}</script>
</body></html>"""


# ── Medical records ────────────────────────────────────────────────────────────

@router.post("/{doctor_id}/records", response_model=MedicalRecordResponse, status_code=status.HTTP_201_CREATED)
def create_medical_record(
    doctor_id: UUID,
    payload: MedicalRecordCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    patient = db.query(Patient).filter(Patient.id == payload.patient_id, Patient.hospital_id == hospital_id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    # Cap base64 payload (~2MB text) to avoid blowing up the DB
    file_data = payload.file_data
    if file_data and len(file_data) > 2_500_000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File too large (max ~1.5MB)")

    record = MedicalRecord(
        hospital_id=hospital_id,
        doctor_id=resolved,
        patient_id=payload.patient_id,
        report_type=payload.report_type.strip(),
        title=payload.title.strip(),
        notes=payload.notes.strip() if payload.notes else None,
        file_name=payload.file_name,
        file_data=file_data,
    )
    db.add(record)
    db.flush()
    write_audit(
        db,
        hospital_id=hospital_id,
        actor=user,
        action="create",
        entity_type="medical_record",
        entity_id=record.id,
        summary=f"Added {payload.report_type} for {patient.name}",
    )
    db.commit()
    record = (
        db.query(MedicalRecord)
        .options(joinedload(MedicalRecord.patient), joinedload(MedicalRecord.doctor))
        .filter(MedicalRecord.id == record.id)
        .first()
    )
    return _record_response(record)


@router.get("/{doctor_id}/records", response_model=list[MedicalRecordResponse])
def list_medical_records(
    doctor_id: UUID,
    patient_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    q = (
        db.query(MedicalRecord)
        .options(joinedload(MedicalRecord.patient), joinedload(MedicalRecord.doctor))
        .filter(MedicalRecord.doctor_id == resolved, MedicalRecord.hospital_id == hospital_id)
    )
    if patient_id:
        q = q.filter(MedicalRecord.patient_id == patient_id)
    rows = q.order_by(MedicalRecord.created_at.desc()).all()
    return [_record_response(r) for r in rows]


@router.get("/{doctor_id}/records/{record_id}/file")
def get_record_file(
    doctor_id: UUID,
    record_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    resolved = _resolve_doctor_id(user, doctor_id, hospital_id, db)
    record = (
        db.query(MedicalRecord)
        .filter(
            MedicalRecord.id == record_id,
            MedicalRecord.doctor_id == resolved,
            MedicalRecord.hospital_id == hospital_id,
        )
        .first()
    )
    if not record or not record.file_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return {"file_name": record.file_name, "file_data": record.file_data}
