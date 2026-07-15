from datetime import date, datetime, time, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Admission,
    AdmissionStatus,
    Appointment,
    AppointmentStatus,
    Bed,
    Department,
    HospitalUser,
    Patient,
    PatientStatus,
    Ward,
)
from app.schemas_mis import (
    AppointmentReportResponse,
    BedReportResponse,
    DailySummaryResponse,
    DoctorPerfRow,
    DoctorReportResponse,
    MetricRow,
    NamedCountRow,
    PatientReportResponse,
    WardOccupancyRow,
)
from app.utils.auth import get_hospital_context, require_hospital_user

router = APIRouter(prefix="/mis", tags=["mis"])


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def _day_end(d: date) -> datetime:
    return datetime.combine(d, time.max, tzinfo=timezone.utc)


def _filters_dict(
    date_from: date | None,
    date_to: date | None,
    department_id: UUID | None,
    doctor_id: UUID | None,
    patient_id: UUID | None,
    status: str | None,
) -> dict:
    return {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "department_id": str(department_id) if department_id else None,
        "doctor_id": str(doctor_id) if doctor_id else None,
        "patient_id": str(patient_id) if patient_id else None,
        "status": status,
    }


def _is_doctor(u: HospitalUser) -> bool:
    return bool(u.role and "doctor" in (u.role.name or "").lower())


def _consultation_fee(doctor: HospitalUser) -> float:
    cv = doctor.custom_values or {}
    for key in ("consultation_fee", "consultationFee", "fee", "consult_fee", "Consultation Fee"):
        val = cv.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return 0.0


def _ward_ids_for_department(db: Session, hospital_id: UUID, department_id: UUID | None) -> list[UUID] | None:
    if not department_id:
        return None
    rows = (
        db.query(Ward.id)
        .filter(Ward.hospital_id == hospital_id, Ward.department_id == department_id, Ward.is_active.is_(True))
        .all()
    )
    return [r[0] for r in rows]


@router.get("/filters/doctors")
def filter_doctors(
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
    return [{"id": str(d.id), "name": d.name} for d in users if _is_doctor(d)]


@router.get("/filters/departments")
def filter_departments(
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    deps = (
        db.query(Department)
        .filter(Department.hospital_id == hospital_id, Department.is_active.is_(True))
        .order_by(Department.name.asc())
        .all()
    )
    return [{"id": str(d.id), "name": d.name} for d in deps]


@router.get("/patients", response_model=PatientReportResponse)
def patient_reports(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = date.today()
    df = date_from or today.replace(day=1)
    dt = date_to or today

    q = db.query(Patient).filter(Patient.hospital_id == hospital_id)
    if patient_id:
        q = q.filter(Patient.id == patient_id)
    if status:
        try:
            q = q.filter(Patient.status == PatientStatus(status))
        except ValueError:
            pass

    total = q.count()

    new_today = (
        db.query(func.count(Patient.id))
        .filter(
            Patient.hospital_id == hospital_id,
            Patient.created_at >= _day_start(today),
            Patient.created_at <= _day_end(today),
        )
        .scalar()
        or 0
    )
    if patient_id:
        new_today = (
            db.query(func.count(Patient.id))
            .filter(
                Patient.hospital_id == hospital_id,
                Patient.id == patient_id,
                Patient.created_at >= _day_start(today),
                Patient.created_at <= _day_end(today),
            )
            .scalar()
            or 0
        )

    # New in range
    new_in_range = (
        db.query(func.count(Patient.id))
        .filter(
            Patient.hospital_id == hospital_id,
            Patient.created_at >= _day_start(df),
            Patient.created_at <= _day_end(dt),
        )
        .scalar()
        or 0
    )

    admitted = (
        db.query(func.count(Patient.id))
        .filter(Patient.hospital_id == hospital_id, Patient.status == PatientStatus.admitted)
        .scalar()
        or 0
    )

    # OPD = patients with appointments in range who are not currently admitted (or distinct OPD visits)
    opd_q = (
        db.query(func.count(func.distinct(Appointment.patient_id)))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date >= df,
            Appointment.appointment_date <= dt,
            Appointment.status != AppointmentStatus.cancelled,
        )
    )
    if doctor_id:
        opd_q = opd_q.filter(Appointment.doctor_id == doctor_id)
    if patient_id:
        opd_q = opd_q.filter(Appointment.patient_id == patient_id)
    opd = opd_q.scalar() or 0

    ipd_q = db.query(func.count(Admission.id)).filter(
        Admission.hospital_id == hospital_id,
        Admission.status == AdmissionStatus.admitted,
    )
    if doctor_id:
        ipd_q = ipd_q.filter(Admission.doctor_id == doctor_id)
    if patient_id:
        ipd_q = ipd_q.filter(Admission.patient_id == patient_id)
    ward_ids = _ward_ids_for_department(db, hospital_id, department_id)
    if ward_ids is not None:
        ipd_q = ipd_q.filter(Admission.ward_id.in_(ward_ids) if ward_ids else Admission.ward_id.is_(None))
    ipd = ipd_q.scalar() or 0

    discharged_today = (
        db.query(func.count(Admission.id))
        .filter(
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.discharged,
            Admission.discharged_at >= _day_start(today),
            Admission.discharged_at <= _day_end(today),
        )
        .scalar()
        or 0
    )

    discharged_range = (
        db.query(func.count(Admission.id))
        .filter(
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.discharged,
            Admission.discharged_at >= _day_start(df),
            Admission.discharged_at <= _day_end(dt),
        )
        .scalar()
        or 0
    )

    metrics = [
        MetricRow(metric="New Patients Today", count=int(new_today)),
        MetricRow(metric="New Patients (Range)", count=int(new_in_range)),
        MetricRow(metric="Total Patients", count=int(total)),
        MetricRow(metric="OPD Patients", count=int(opd)),
        MetricRow(metric="Admitted Patients (IPD)", count=int(admitted if not ipd else max(int(admitted), int(ipd)))),
        MetricRow(metric="IPD Admissions (Active)", count=int(ipd)),
        MetricRow(metric="Discharged Today", count=int(discharged_today)),
        MetricRow(metric="Discharged (Range)", count=int(discharged_range)),
    ]
    return PatientReportResponse(
        metrics=metrics,
        generated_at=datetime.now(timezone.utc).isoformat(),
        filters=_filters_dict(df, dt, department_id, doctor_id, patient_id, status),
    )


@router.get("/appointments", response_model=AppointmentReportResponse)
def appointment_reports(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = date.today()
    df = date_from or today
    dt = date_to or today

    base = db.query(Appointment).filter(
        Appointment.hospital_id == hospital_id,
        Appointment.appointment_date >= df,
        Appointment.appointment_date <= dt,
    )
    if doctor_id:
        base = base.filter(Appointment.doctor_id == doctor_id)
    if patient_id:
        base = base.filter(Appointment.patient_id == patient_id)
    if status:
        try:
            base = base.filter(Appointment.status == AppointmentStatus(status))
        except ValueError:
            pass

    all_rows = base.options(joinedload(Appointment.doctor)).all()

    completed = sum(1 for a in all_rows if a.status == AppointmentStatus.completed)
    cancelled = sum(1 for a in all_rows if a.status == AppointmentStatus.cancelled)
    no_show = sum(1 for a in all_rows if a.status == AppointmentStatus.no_show)
    waiting = sum(1 for a in all_rows if a.status == AppointmentStatus.waiting)
    scheduled = sum(1 for a in all_rows if a.status == AppointmentStatus.scheduled)

    by_doc: dict[UUID, NamedCountRow] = {}
    for a in all_rows:
        if a.status == AppointmentStatus.cancelled:
            continue
        did = a.doctor_id
        if did not in by_doc:
            by_doc[did] = NamedCountRow(name=a.doctor.name if a.doctor else "Unknown", count=0)
        by_doc[did].count += 1

    by_doctor = sorted(by_doc.values(), key=lambda x: x.count, reverse=True)

    today_q = db.query(func.count(Appointment.id)).filter(
        Appointment.hospital_id == hospital_id,
        Appointment.appointment_date == today,
    )
    if doctor_id:
        today_q = today_q.filter(Appointment.doctor_id == doctor_id)
    if patient_id:
        today_q = today_q.filter(Appointment.patient_id == patient_id)
    today_total = int(today_q.scalar() or 0)

    metrics = [
        MetricRow(metric="Today's Appointments", count=today_total),
        MetricRow(metric="Total Appointments (Range)", count=len(all_rows)),
        MetricRow(metric="Completed Appointments", count=completed),
        MetricRow(metric="Cancelled Appointments", count=cancelled),
        MetricRow(metric="No Shows", count=no_show),
        MetricRow(metric="Waiting / Checked In", count=waiting),
        MetricRow(metric="Scheduled", count=scheduled),
    ]

    return AppointmentReportResponse(
        metrics=metrics,
        by_doctor=by_doctor,
        generated_at=datetime.now(timezone.utc).isoformat(),
        filters=_filters_dict(df, dt, department_id, doctor_id, patient_id, status),
    )


@router.get("/beds", response_model=BedReportResponse)
def bed_reports(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    ward_ids = _ward_ids_for_department(db, hospital_id, department_id)

    bq = db.query(Bed).filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
    if ward_ids is not None:
        bq = bq.filter(Bed.ward_id.in_(ward_ids)) if ward_ids else bq.filter(False)
    if status == "available":
        bq = bq.filter(Bed.is_occupied.is_(False))
    elif status == "occupied":
        bq = bq.filter(Bed.is_occupied.is_(True))

    beds = bq.options(joinedload(Bed.ward)).all()
    total = len(beds)
    occupied = sum(1 for b in beds if b.is_occupied)
    available = total - occupied
    pct = round((occupied / total) * 100, 1) if total else 0.0

    ward_map: dict[UUID, WardOccupancyRow] = {}
    for b in beds:
        wid = b.ward_id
        if wid not in ward_map:
            ward_map[wid] = WardOccupancyRow(
                ward_name=b.ward.name if b.ward else "Unknown",
                occupied=0,
                available=0,
                total=0,
                occupancy_percent=0.0,
            )
        ward_map[wid].total += 1
        if b.is_occupied:
            ward_map[wid].occupied += 1
        else:
            ward_map[wid].available += 1
    by_ward = []
    for row in ward_map.values():
        row.occupancy_percent = round((row.occupied / row.total) * 100, 1) if row.total else 0.0
        by_ward.append(row)
    by_ward.sort(key=lambda x: x.ward_name)

    metrics = [
        MetricRow(metric="Total Beds", count=total),
        MetricRow(metric="Occupied Beds", count=occupied),
        MetricRow(metric="Available Beds", count=available),
        MetricRow(metric="Occupancy %", count=pct),
    ]
    return BedReportResponse(
        metrics=metrics,
        by_ward=by_ward,
        generated_at=datetime.now(timezone.utc).isoformat(),
        filters=_filters_dict(date_from, date_to, department_id, doctor_id, patient_id, status),
    )


@router.get("/doctors", response_model=DoctorReportResponse)
def doctor_reports(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    today = date.today()
    df = date_from or today.replace(day=1)
    dt = date_to or today

    doctors = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .all()
    )
    doctors = [d for d in doctors if _is_doctor(d)]
    if doctor_id:
        doctors = [d for d in doctors if d.id == doctor_id]

    rows: list[DoctorPerfRow] = []
    for d in doctors:
        aq = db.query(Appointment).filter(
            Appointment.hospital_id == hospital_id,
            Appointment.doctor_id == d.id,
            Appointment.appointment_date >= df,
            Appointment.appointment_date <= dt,
        )
        if patient_id:
            aq = aq.filter(Appointment.patient_id == patient_id)
        if status:
            try:
                aq = aq.filter(Appointment.status == AppointmentStatus(status))
            except ValueError:
                pass
        appts = aq.all()
        completed = [a for a in appts if a.status == AppointmentStatus.completed]
        patients_seen = len({a.patient_id for a in appts if a.status not in {AppointmentStatus.cancelled, AppointmentStatus.no_show}})
        fee = _consultation_fee(d)
        revenue = fee * len(completed)

        # Optional avg consult time from appointment type slot isn't on appointment — use None or 15 default when completed
        avg_mins = 15.0 if completed else None

        rows.append(
            DoctorPerfRow(
                doctor_id=d.id,
                doctor_name=d.name,
                patients_seen=patients_seen,
                appointments_completed=len(completed),
                appointments_total=len(appts),
                average_consultation_minutes=avg_mins,
                revenue=revenue,
            )
        )

    rows.sort(key=lambda r: r.patients_seen, reverse=True)
    metrics = [
        MetricRow(metric="Doctors", count=len(rows)),
        MetricRow(metric="Total Patients Seen", count=sum(r.patients_seen for r in rows)),
        MetricRow(metric="Appointments Completed", count=sum(r.appointments_completed for r in rows)),
        MetricRow(metric="Revenue Generated (₹)", count=round(sum(r.revenue for r in rows), 2)),
    ]
    return DoctorReportResponse(
        doctors=rows,
        metrics=metrics,
        generated_at=datetime.now(timezone.utc).isoformat(),
        filters=_filters_dict(df, dt, department_id, doctor_id, patient_id, status),
    )


@router.get("/daily-summary", response_model=DailySummaryResponse)
def daily_summary(
    on_date: date | None = Query(default=None, alias="date"),
    department_id: UUID | None = Query(default=None),
    doctor_id: UUID | None = Query(default=None),
    patient_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_hospital_user),
    hospital_id: UUID = Depends(get_hospital_context),
):
    day = on_date or date.today()

    new_patients = (
        db.query(func.count(Patient.id))
        .filter(
            Patient.hospital_id == hospital_id,
            Patient.created_at >= _day_start(day),
            Patient.created_at <= _day_end(day),
        )
        .scalar()
        or 0
    )

    appt_q = db.query(func.count(Appointment.id)).filter(
        Appointment.hospital_id == hospital_id,
        Appointment.appointment_date == day,
    )
    if doctor_id:
        appt_q = appt_q.filter(Appointment.doctor_id == doctor_id)
    if patient_id:
        appt_q = appt_q.filter(Appointment.patient_id == patient_id)
    appointments = appt_q.scalar() or 0

    adm_q = db.query(func.count(Admission.id)).filter(
        Admission.hospital_id == hospital_id,
        Admission.admitted_at >= _day_start(day),
        Admission.admitted_at <= _day_end(day),
    )
    if doctor_id:
        adm_q = adm_q.filter(Admission.doctor_id == doctor_id)
    admissions = adm_q.scalar() or 0

    dis_q = db.query(func.count(Admission.id)).filter(
        Admission.hospital_id == hospital_id,
        Admission.status == AdmissionStatus.discharged,
        Admission.discharged_at >= _day_start(day),
        Admission.discharged_at <= _day_end(day),
    )
    discharges = dis_q.scalar() or 0

    occupied = (
        db.query(func.count(Bed.id))
        .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True), Bed.is_occupied.is_(True))
        .scalar()
        or 0
    )

    # Revenue from completed appointments today × doctor consultation fee
    completed = (
        db.query(Appointment)
        .options(joinedload(Appointment.doctor))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date == day,
            Appointment.status == AppointmentStatus.completed,
        )
        .all()
    )
    if doctor_id:
        completed = [a for a in completed if a.doctor_id == doctor_id]
    revenue = 0.0
    for a in completed:
        if a.doctor:
            revenue += _consultation_fee(a.doctor)

    metrics = [
        MetricRow(metric="New Patients", count=int(new_patients)),
        MetricRow(metric="Appointments", count=int(appointments)),
        MetricRow(metric="Admissions", count=int(admissions)),
        MetricRow(metric="Discharges", count=int(discharges)),
        MetricRow(metric="Revenue", count=f"₹{revenue:,.0f}"),
        MetricRow(metric="Occupied Beds", count=int(occupied)),
    ]
    return DailySummaryResponse(
        summary_date=day,
        new_patients=int(new_patients),
        appointments=int(appointments),
        admissions=int(admissions),
        discharges=int(discharges),
        revenue=revenue,
        occupied_beds=int(occupied),
        metrics=metrics,
        generated_at=datetime.now(timezone.utc).isoformat(),
        filters=_filters_dict(day, day, department_id, doctor_id, patient_id, status),
    )
