from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    Admission,
    AdmissionStatus,
    Appointment,
    AppointmentStatus,
    Bed,
    BillingCharge,
    BillingChargeStatus,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPayment,
    BillingReceipt,
    BillingReceiptStatus,
    BillingSourceType,
    Hospital,
    HospitalUser,
    LabOrder,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestStatus,
    OtSurgery,
    OtSurgeryStatus,
    Patient,
    RadiologyOrder,
    RadiologyOrderStatus,
)
from app.schemas import (
    DoctorRecentRevenueItem,
    HospitalCreate,
    HospitalCreateResponse,
    HospitalDashboardListItem,
    HospitalDashboardResponse,
    HospitalResponse,
    RoleDashboardListItem,
    RoleDashboardMetric,
    RoleDashboardResponse,
)
from app.schemas_admin import BASIC_MODULE_KEYS
from app.utils.auth import get_hospital_context, require_hospital_user, require_super_admin
from app.utils.hospital_id import generate_hospital_id
from app.utils.password import generate_temp_password, hash_password

router = APIRouter(prefix="/hospitals", tags=["hospitals"])


def _is_doctor_role(name: str | None) -> bool:
    return bool(name and "doctor" in name.lower())


def _normalize_role(name: str | None) -> str:
    return (name or "").strip().lower().replace("-", " ").replace("_", " ")


def _detect_persona(user: dict) -> str:
    """Map JWT user to dashboard persona without hardcoding role IDs."""
    if user.get("role") == "hospital_admin":
        return "admin"
    if user.get("role") != "hospital_staff":
        return "staff"
    role_name = _normalize_role(user.get("staff_role_name"))
    if "doctor" in role_name:
        return "doctor"
    if "nurse" in role_name or "nursing" in role_name:
        return "nurse"
    if any(k in role_name for k in ("reception", "front desk", "frontdesk", "front office")):
        return "reception"
    if any(k in role_name for k in ("lab technician", "laboratory", "pathology")) or (
        "lab" in role_name and "label" not in role_name
    ):
        return "lab"
    if any(k in role_name for k in ("radiology", "radiologist", "radiology technician", "imaging")):
        return "radiology"
    if any(
        k in role_name
        for k in ("operation theatre", "operation theater", "ot staff", "ot technician")
    ) or role_name == "ot" or role_name.startswith("ot "):
        return "ot"
    if any(k in role_name for k in ("billing", "accounts", "cashier", "finance")):
        return "billing"
    return "staff"


def _fmt_time(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    return str(value)[:5]


def _fmt_date(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)[:10]


def _day_start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time()).replace(tzinfo=timezone.utc)


def _day_end(d: date) -> datetime:
    return datetime.combine(d, datetime.max.time()).replace(tzinfo=timezone.utc)


def _doctor_practice_performance(
    db: Session, hospital_id: UUID, doctor_id: UUID | None
) -> dict:
    """Consultation revenue metrics for the logged-in doctor (read-only billing views)."""
    empty = {
        "today_revenue": 0.0,
        "month_revenue": 0.0,
        "patients_this_month": 0,
        "average_revenue_per_patient": 0.0,
        "recent_revenue": [],
    }
    if not doctor_id:
        return empty

    today = date.today()
    month_start = today.replace(day=1)

    def _consultation_revenue(*, created_from: date, created_to: date) -> float:
        total = (
            db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
            .join(Appointment, Appointment.id == BillingCharge.source_id)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.source_type == BillingSourceType.consultation,
                BillingCharge.status != BillingChargeStatus.cancelled,
                BillingCharge.created_at >= _day_start(created_from),
                BillingCharge.created_at <= _day_end(created_to),
                Appointment.hospital_id == hospital_id,
                Appointment.doctor_id == doctor_id,
            )
            .scalar()
        )
        return float(total or 0.0)

    today_revenue = _consultation_revenue(created_from=today, created_to=today)
    month_revenue = _consultation_revenue(created_from=month_start, created_to=today)

    patients_this_month = int(
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.doctor_id == doctor_id,
            Appointment.appointment_date >= month_start,
            Appointment.appointment_date <= today,
            Appointment.status == AppointmentStatus.completed,
        )
        .scalar()
        or 0
    )

    average_revenue_per_patient = (
        round(month_revenue / patients_this_month, 2) if patients_this_month > 0 else 0.0
    )

    recent_rows = (
        db.query(BillingCharge, Appointment, Patient)
        .join(Appointment, Appointment.id == BillingCharge.source_id)
        .join(Patient, Patient.id == Appointment.patient_id)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.source_type == BillingSourceType.consultation,
            BillingCharge.status != BillingChargeStatus.cancelled,
            Appointment.hospital_id == hospital_id,
            Appointment.doctor_id == doctor_id,
        )
        .order_by(BillingCharge.created_at.desc())
        .limit(10)
        .all()
    )
    recent_revenue = [
        DoctorRecentRevenueItem(
            patient_name=patient.name if patient else "Patient",
            appointment_date=_fmt_date(appt.appointment_date) if appt else None,
            amount=round(float(charge.net_amount or 0), 2),
            status=charge.status.value if charge.status else "pending",
        )
        for charge, appt, patient in recent_rows
    ]

    return {
        "today_revenue": round(today_revenue, 2),
        "month_revenue": round(month_revenue, 2),
        "patients_this_month": patients_this_month,
        "average_revenue_per_patient": average_revenue_per_patient,
        "recent_revenue": recent_revenue,
    }


def _build_doctor_dashboard(db: Session, hospital_id: UUID, user: dict) -> RoleDashboardResponse:
    doctor_id_raw = user.get("user_id")
    doctor_id = UUID(str(doctor_id_raw)) if doctor_id_raw else None
    today = date.today()
    week_end = today + timedelta(days=7)

    today_q = db.query(Appointment).options(joinedload(Appointment.patient)).filter(
        Appointment.hospital_id == hospital_id,
        Appointment.appointment_date == today,
        Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
    )
    if doctor_id:
        today_q = today_q.filter(Appointment.doctor_id == doctor_id)
    today_rows = today_q.order_by(Appointment.appointment_time.asc()).limit(20).all()

    pending = sum(1 for a in today_rows if a.status in (AppointmentStatus.scheduled, AppointmentStatus.waiting))
    completed_today = sum(1 for a in today_rows if a.status == AppointmentStatus.completed)
    unique_patients = {a.patient_id for a in today_rows}

    upcoming_q = db.query(Appointment).options(joinedload(Appointment.patient)).filter(
        Appointment.hospital_id == hospital_id,
        Appointment.appointment_date > today,
        Appointment.appointment_date <= week_end,
        Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
    )
    if doctor_id:
        upcoming_q = upcoming_q.filter(Appointment.doctor_id == doctor_id)
    upcoming_rows = upcoming_q.order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc()).limit(10).all()

    recent_q = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.status == AppointmentStatus.completed,
        )
    )
    if doctor_id:
        recent_q = recent_q.filter(Appointment.doctor_id == doctor_id)
    recent_rows = recent_q.order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc()).limit(8).all()

    ot_items: list[RoleDashboardListItem] = []
    if doctor_id:
        start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        end = datetime.combine(week_end, datetime.max.time()).replace(tzinfo=timezone.utc)
        ot_rows = (
            db.query(OtSurgery)
            .options(joinedload(OtSurgery.patient), joinedload(OtSurgery.ot_room_ref))
            .filter(
                OtSurgery.hospital_id == hospital_id,
                OtSurgery.surgeon_id == doctor_id,
                OtSurgery.scheduled_at >= start,
                OtSurgery.scheduled_at <= end,
                OtSurgery.status != OtSurgeryStatus.cancelled,
            )
            .order_by(OtSurgery.scheduled_at.asc())
            .limit(8)
            .all()
        )
        for s in ot_rows:
            room = s.ot_room_ref
            room_label = (room.code if room and room.code else None) or (room.name if room else None) or s.ot_room or "OT"
            ot_items.append(
                RoleDashboardListItem(
                    id=str(s.id),
                    title=s.surgery_type or "Surgery",
                    subtitle=s.patient.name if s.patient else None,
                    meta=room_label,
                    status=s.status.value if s.status else None,
                    time=_fmt_time(s.scheduled_at) if s.scheduled_at else None,
                )
            )

    practice = _doctor_practice_performance(db, hospital_id, doctor_id)

    return RoleDashboardResponse(
        persona="doctor",
        display_name=user.get("name") or "Doctor",
        staff_role_name=user.get("staff_role_name"),
        metrics=[
            RoleDashboardMetric(key="today_appts", label="Today's Appointments", value=len(today_rows), sub="Scheduled for today"),
            RoleDashboardMetric(key="pending", label="Pending Consultations", value=pending, sub="Scheduled or in progress"),
            RoleDashboardMetric(key="completed", label="Completed Today", value=completed_today, sub="Finished consultations"),
            RoleDashboardMetric(key="patients_today", label="Today's Patients", value=len(unique_patients), sub="Unique patients"),
        ],
        today_items=[
            RoleDashboardListItem(
                id=str(a.id),
                title=a.patient.name if a.patient else "Patient",
                subtitle=getattr(a.patient, "uhid", None) if a.patient else None,
                meta=a.visit_type,
                status="in_progress" if a.status == AppointmentStatus.waiting else (a.status.value if a.status else None),
                time=_fmt_time(a.appointment_time),
            )
            for a in today_rows
        ],
        upcoming_items=[
            RoleDashboardListItem(
                id=str(a.id),
                title=a.patient.name if a.patient else "Patient",
                subtitle=_fmt_date(a.appointment_date),
                meta=a.visit_type,
                status=a.status.value if a.status else None,
                time=_fmt_time(a.appointment_time),
            )
            for a in upcoming_rows
        ],
        recent_items=[
            RoleDashboardListItem(
                id=str(a.patient_id),
                title=a.patient.name if a.patient else "Patient",
                subtitle=getattr(a.patient, "uhid", None) if a.patient else None,
                meta=_fmt_date(a.appointment_date),
                status="completed",
                time=_fmt_time(a.appointment_time),
            )
            for a in recent_rows
        ],
        activity_items=ot_items,
        quick_actions=[
            {"id": "book", "label": "Book Appointment", "module": "appointment", "section": "book"},
            {"id": "patients", "label": "Open Patient Records", "module": "doctors", "section": "patients"},
            {"id": "rx", "label": "Create Prescription", "module": "doctors", "section": "patients"},
            {"id": "calendar", "label": "Open Calendar", "module": "doctors", "section": "calendar"},
        ],
        today_revenue=practice["today_revenue"],
        month_revenue=practice["month_revenue"],
        patients_this_month=practice["patients_this_month"],
        average_revenue_per_patient=practice["average_revenue_per_patient"],
        recent_revenue=practice["recent_revenue"],
    )


def _build_nurse_dashboard(db: Session, hospital_id: UUID, user: dict) -> RoleDashboardResponse:
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

    admissions = (
        db.query(Admission)
        .options(joinedload(Admission.patient), joinedload(Admission.bed), joinedload(Admission.ward))
        .filter(Admission.hospital_id == hospital_id, Admission.status == AdmissionStatus.admitted)
        .order_by(Admission.admitted_at.desc())
        .limit(20)
        .all()
    )
    beds_total = int(
        db.query(func.count(Bed.id)).filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True)).scalar() or 0
    )
    beds_occupied = int(
        db.query(func.count(Bed.id))
        .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True), Bed.is_occupied.is_(True))
        .scalar()
        or 0
    )
    occupancy = int(round((beds_occupied / beds_total) * 100)) if beds_total else 0

    # Multi-day stays are the practical discharge-review queue (no expected_discharge field yet)
    discharge_candidates = [
        a
        for a in admissions
        if a.admitted_at and (a.admitted_at.astimezone(timezone.utc).date() if a.admitted_at.tzinfo else a.admitted_at.date()) < today
    ][:12]

    recently_admitted = (
        db.query(Admission)
        .options(joinedload(Admission.patient), joinedload(Admission.bed), joinedload(Admission.ward))
        .filter(
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.admitted,
            Admission.admitted_at >= start,
            Admission.admitted_at <= end,
        )
        .order_by(Admission.admitted_at.desc())
        .limit(10)
        .all()
    )

    return RoleDashboardResponse(
        persona="nurse",
        display_name=user.get("name") or "Nurse",
        staff_role_name=user.get("staff_role_name"),
        metrics=[
            RoleDashboardMetric(
                key="admissions", label="Active Admissions", value=len(admissions), sub="Current inpatients"
            ),
            RoleDashboardMetric(
                key="beds_occupied", label="Beds Occupied", value=beds_occupied, sub=f"{occupancy}% occupancy"
            ),
            RoleDashboardMetric(
                key="beds_free",
                label="Beds Available",
                value=max(0, beds_total - beds_occupied),
                sub=f"of {beds_total} total",
            ),
            RoleDashboardMetric(
                key="discharge_pending",
                label="Discharges Pending Today",
                value=len(discharge_candidates),
                sub="Multi-day stays to review",
            ),
        ],
        today_items=[
            RoleDashboardListItem(
                id=str(a.id),
                title=a.patient.name if a.patient else "Patient",
                subtitle=getattr(a.patient, "uhid", None) if a.patient else None,
                meta=(a.ward.name if a.ward else None) or (a.bed.bed_code if a.bed else None),
                status=a.status.value if a.status else None,
                time=_fmt_time(a.admitted_at) if a.admitted_at else None,
            )
            for a in admissions[:12]
        ],
        upcoming_items=[
            RoleDashboardListItem(
                id=str(a.id),
                title=a.patient.name if a.patient else "Patient",
                subtitle=getattr(a.patient, "uhid", None) if a.patient else None,
                meta=(a.ward.name if a.ward else None) or (a.bed.bed_code if a.bed else None),
                status="review",
                time=_fmt_date(
                    a.admitted_at.astimezone(timezone.utc).date()
                    if a.admitted_at and a.admitted_at.tzinfo
                    else (a.admitted_at.date() if a.admitted_at else None)
                ),
            )
            for a in discharge_candidates
        ],
        recent_items=[
            RoleDashboardListItem(
                id=str(a.id),
                title=a.patient.name if a.patient else "Patient",
                subtitle=getattr(a.patient, "uhid", None) if a.patient else None,
                meta=(a.ward.name if a.ward else None) or (a.bed.bed_code if a.bed else None),
                status="admitted",
                time=_fmt_time(a.admitted_at) if a.admitted_at else None,
            )
            for a in recently_admitted
        ],
        activity_items=[
            RoleDashboardListItem(
                id="occ",
                title="Bed occupancy",
                subtitle=f"{beds_occupied} occupied · {max(0, beds_total - beds_occupied)} free",
                meta=f"{occupancy}%",
                status="summary",
            )
        ],
        quick_actions=[
            {"id": "admit", "label": "Admit Patient", "module": "bed", "section": "admit"},
            {"id": "transfer", "label": "Transfer Bed", "module": "bed", "section": "transfer"},
            {"id": "discharge", "label": "Discharge Patient", "module": "bed", "section": "discharge"},
            {"id": "docs", "label": "Open Patient File", "module": "dms", "section": None},
        ],
    )


def _build_reception_dashboard(db: Session, hospital_id: UUID, user: dict) -> RoleDashboardResponse:
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

    today_rows = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date == today,
            Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
        )
        .order_by(Appointment.appointment_time.asc())
        .limit(25)
        .all()
    )
    waiting = [a for a in today_rows if a.status == AppointmentStatus.waiting]
    completed = [a for a in today_rows if a.status == AppointmentStatus.completed]
    scheduled = [a for a in today_rows if a.status == AppointmentStatus.scheduled]
    no_shows = int(
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date == today,
            Appointment.status == AppointmentStatus.no_show,
        )
        .scalar()
        or 0
    )

    registrations_today = int(
        db.query(func.count(Patient.id))
        .filter(Patient.hospital_id == hospital_id, Patient.created_at >= start, Patient.created_at <= end)
        .scalar()
        or 0
    )
    admissions_today = int(
        db.query(func.count(Admission.id))
        .filter(Admission.hospital_id == hospital_id, Admission.admitted_at >= start, Admission.admitted_at <= end)
        .scalar()
        or 0
    )

    recent_patients = (
        db.query(Patient)
        .filter(Patient.hospital_id == hospital_id)
        .order_by(Patient.created_at.desc())
        .limit(8)
        .all()
    )

    return RoleDashboardResponse(
        persona="reception",
        display_name=user.get("name") or "Reception",
        staff_role_name=user.get("staff_role_name"),
        metrics=[
            RoleDashboardMetric(key="today", label="Today's Appointments", value=len(today_rows), sub=f"{len(scheduled)} still scheduled"),
            RoleDashboardMetric(key="waiting", label="In Progress / Waiting", value=len(waiting), sub="Checked in"),
            RoleDashboardMetric(key="completed", label="Completed Today", value=len(completed), sub="Finished consultations"),
            RoleDashboardMetric(key="registrations", label="New Registrations", value=registrations_today, sub=f"{admissions_today} admissions · {no_shows} no-shows"),
        ],
        today_items=[
            RoleDashboardListItem(
                id=str(a.id),
                title=a.patient.name if a.patient else "Patient",
                subtitle=a.doctor.name if a.doctor else None,
                meta=a.visit_type,
                status="in_progress" if a.status == AppointmentStatus.waiting else (a.status.value if a.status else None),
                time=_fmt_time(a.appointment_time),
            )
            for a in today_rows[:12]
        ],
        upcoming_items=[
            RoleDashboardListItem(
                id=str(a.id),
                title=a.patient.name if a.patient else "Patient",
                subtitle=f"Token {a.queue_token}" if a.queue_token else "In progress",
                meta=a.doctor.name if a.doctor else None,
                status="in_progress",
                time=_fmt_time(a.appointment_time),
            )
            for a in waiting[:10]
        ],
        recent_items=[
            RoleDashboardListItem(
                id=str(p.id),
                title=p.name,
                subtitle=getattr(p, "uhid", None),
                meta=p.mobile,
                status="registered",
                time=_fmt_date(p.created_at.date() if p.created_at else None),
            )
            for p in recent_patients
        ],
        activity_items=[
            RoleDashboardListItem(
                id="adm",
                title="Admissions today",
                subtitle=f"{admissions_today} patients admitted",
                meta=str(admissions_today),
                status="summary",
            )
        ],
        quick_actions=[
            {"id": "register", "label": "Register Patient", "module": "registration", "section": "register"},
            {"id": "book", "label": "Book Appointment", "module": "appointment", "section": "book"},
            {"id": "queue", "label": "View Queue", "module": "appointment", "section": "queue"},
            {"id": "directory", "label": "Patient Directory", "module": "registration", "section": "directory"},
        ],
    )


def _count_lab_today(db: Session, hospital_id: UUID, start: datetime, end: datetime, statuses: list | None = None) -> int:
    q = db.query(func.count(LabOrder.id)).filter(
        LabOrder.hospital_id == hospital_id,
        LabOrder.ordered_at >= start,
        LabOrder.ordered_at <= end,
    )
    if statuses:
        q = q.filter(LabOrder.status.in_(statuses))
    return int(q.scalar() or 0)


def _build_lab_dashboard(db: Session, hospital_id: UUID, user: dict) -> RoleDashboardResponse:
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

    todays_orders = _count_lab_today(db, hospital_id, start, end)
    pending = int(
        db.query(func.count(LabOrder.id))
        .filter(
            LabOrder.hospital_id == hospital_id,
            LabOrder.status.in_([LabOrderStatus.ordered, LabOrderStatus.sample_collected, LabOrderStatus.in_progress]),
        )
        .scalar()
        or 0
    )
    sample_collected = int(
        db.query(func.count(LabOrder.id))
        .filter(LabOrder.hospital_id == hospital_id, LabOrder.status == LabOrderStatus.sample_collected)
        .scalar()
        or 0
    )
    in_progress = int(
        db.query(func.count(LabOrder.id))
        .filter(LabOrder.hospital_id == hospital_id, LabOrder.status == LabOrderStatus.in_progress)
        .scalar()
        or 0
    )
    completed_today = int(
        db.query(func.count(LabOrder.id))
        .filter(
            LabOrder.hospital_id == hospital_id,
            LabOrder.status == LabOrderStatus.completed,
            LabOrder.ordered_at >= start,
            LabOrder.ordered_at <= end,
        )
        .scalar()
        or 0
    )
    # Prefer completed_at if present
    try:
        completed_today = int(
            db.query(func.count(LabOrder.id))
            .filter(
                LabOrder.hospital_id == hospital_id,
                LabOrder.status == LabOrderStatus.completed,
                LabOrder.completed_at >= start,
                LabOrder.completed_at <= end,
            )
            .scalar()
            or 0
        )
    except Exception:
        pass

    doctor_requests = (
        db.query(LabPrescriptionRequest)
        .options(joinedload(LabPrescriptionRequest.patient), joinedload(LabPrescriptionRequest.doctor))
        .filter(
            LabPrescriptionRequest.hospital_id == hospital_id,
            LabPrescriptionRequest.status.in_(
                [LabPrescriptionRequestStatus.pending, LabPrescriptionRequestStatus.partially_processed]
            ),
        )
        .order_by(LabPrescriptionRequest.created_at.desc())
        .limit(12)
        .all()
    )

    waiting_sample = (
        db.query(LabOrder)
        .options(joinedload(LabOrder.patient))
        .filter(LabOrder.hospital_id == hospital_id, LabOrder.status == LabOrderStatus.ordered)
        .order_by(LabOrder.ordered_at.asc())
        .limit(12)
        .all()
    )
    in_progress_rows = (
        db.query(LabOrder)
        .options(joinedload(LabOrder.patient))
        .filter(
            LabOrder.hospital_id == hospital_id,
            LabOrder.status.in_([LabOrderStatus.sample_collected, LabOrderStatus.in_progress]),
        )
        .order_by(LabOrder.ordered_at.desc())
        .limit(12)
        .all()
    )
    completed_rows = (
        db.query(LabOrder)
        .options(joinedload(LabOrder.patient))
        .filter(LabOrder.hospital_id == hospital_id, LabOrder.status == LabOrderStatus.completed)
        .order_by(LabOrder.completed_at.desc().nullslast(), LabOrder.ordered_at.desc())
        .limit(10)
        .all()
    )

    return RoleDashboardResponse(
        persona="lab",
        display_name=user.get("name") or "Lab Technician",
        staff_role_name=user.get("staff_role_name"),
        metrics=[
            RoleDashboardMetric(key="today", label="Today's Orders", value=todays_orders, sub="Created today"),
            RoleDashboardMetric(key="pending", label="Pending Orders", value=pending, sub="Open workflow"),
            RoleDashboardMetric(key="sample", label="Sample Collected", value=sample_collected, sub="Awaiting processing"),
            RoleDashboardMetric(key="in_progress", label="In Progress", value=in_progress, sub="Being processed"),
            RoleDashboardMetric(key="completed", label="Completed Today", value=completed_today, sub="Finished today"),
            RoleDashboardMetric(
                key="doctor_req", label="Pending Doctor Requests", value=len(doctor_requests), sub="From prescriptions"
            ),
        ],
        today_items=[
            RoleDashboardListItem(
                id=str(r.id),
                title=r.patient.name if r.patient else "Patient",
                subtitle=getattr(r.patient, "uhid", None) if r.patient else None,
                meta=r.doctor.name if r.doctor else "Doctor request",
                status=r.status.value if r.status else "pending",
                time=_fmt_time(r.created_at) if r.created_at else None,
            )
            for r in doctor_requests
        ],
        upcoming_items=[
            RoleDashboardListItem(
                id=str(o.id),
                title=o.order_no,
                subtitle=o.patient.name if o.patient else None,
                meta="Awaiting sample",
                status=o.status.value if o.status else None,
                time=_fmt_time(o.ordered_at) if o.ordered_at else None,
            )
            for o in waiting_sample
        ],
        recent_items=[
            RoleDashboardListItem(
                id=str(o.id),
                title=o.order_no,
                subtitle=o.patient.name if o.patient else None,
                meta=o.status.value.replace("_", " ") if o.status else None,
                status=o.status.value if o.status else None,
                time=_fmt_time(o.ordered_at) if o.ordered_at else None,
            )
            for o in in_progress_rows
        ],
        activity_items=[
            RoleDashboardListItem(
                id=str(o.id),
                title=o.order_no,
                subtitle=o.patient.name if o.patient else None,
                meta="Completed",
                status="completed",
                time=_fmt_time(o.completed_at or o.ordered_at) if (o.completed_at or o.ordered_at) else None,
            )
            for o in completed_rows
        ],
        quick_actions=[
            {"id": "create_order", "label": "Create Order", "module": "laboratory", "section": "orders"},
            {"id": "collect", "label": "Collect Sample", "module": "laboratory", "section": "orders"},
            {"id": "results", "label": "Enter Results", "module": "laboratory", "section": "orders"},
        ],
    )


def _build_radiology_dashboard(db: Session, hospital_id: UUID, user: dict) -> RoleDashboardResponse:
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

    todays_scans = int(
        db.query(func.count(RadiologyOrder.id))
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.ordered_at >= start,
            RadiologyOrder.ordered_at <= end,
            RadiologyOrder.status != RadiologyOrderStatus.cancelled,
        )
        .scalar()
        or 0
    )
    pending_scans = int(
        db.query(func.count(RadiologyOrder.id))
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.status.in_(
                [RadiologyOrderStatus.ordered, RadiologyOrderStatus.scheduled, RadiologyOrderStatus.in_progress]
            ),
        )
        .scalar()
        or 0
    )
    scheduled = int(
        db.query(func.count(RadiologyOrder.id))
        .filter(RadiologyOrder.hospital_id == hospital_id, RadiologyOrder.status == RadiologyOrderStatus.scheduled)
        .scalar()
        or 0
    )
    reports_pending = int(
        db.query(func.count(RadiologyOrder.id))
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.status.in_([RadiologyOrderStatus.in_progress, RadiologyOrderStatus.completed]),
            RadiologyOrder.report_file_data.is_(None),
        )
        .scalar()
        or 0
    )
    completed_today = int(
        db.query(func.count(RadiologyOrder.id))
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.status == RadiologyOrderStatus.completed,
            RadiologyOrder.completed_at >= start,
            RadiologyOrder.completed_at <= end,
        )
        .scalar()
        or 0
    )

    scheduled_today = (
        db.query(RadiologyOrder)
        .options(joinedload(RadiologyOrder.patient))
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.status.in_([RadiologyOrderStatus.ordered, RadiologyOrderStatus.scheduled]),
        )
        .order_by(RadiologyOrder.scheduled_at.asc().nullslast(), RadiologyOrder.ordered_at.asc())
        .limit(12)
        .all()
    )
    awaiting_report = (
        db.query(RadiologyOrder)
        .options(joinedload(RadiologyOrder.patient))
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.status.in_([RadiologyOrderStatus.in_progress, RadiologyOrderStatus.completed]),
            RadiologyOrder.report_file_data.is_(None),
        )
        .order_by(RadiologyOrder.ordered_at.desc())
        .limit(12)
        .all()
    )
    recently_completed = (
        db.query(RadiologyOrder)
        .options(joinedload(RadiologyOrder.patient))
        .filter(RadiologyOrder.hospital_id == hospital_id, RadiologyOrder.status == RadiologyOrderStatus.completed)
        .order_by(RadiologyOrder.completed_at.desc().nullslast(), RadiologyOrder.ordered_at.desc())
        .limit(10)
        .all()
    )

    return RoleDashboardResponse(
        persona="radiology",
        display_name=user.get("name") or "Radiology",
        staff_role_name=user.get("staff_role_name"),
        metrics=[
            RoleDashboardMetric(key="today", label="Today's Scans", value=todays_scans, sub="Ordered today"),
            RoleDashboardMetric(key="pending", label="Pending Scans", value=pending_scans, sub="Open workflow"),
            RoleDashboardMetric(key="scheduled", label="Scheduled Scans", value=scheduled, sub="On calendar"),
            RoleDashboardMetric(key="reports", label="Reports Pending", value=reports_pending, sub="Need report upload"),
            RoleDashboardMetric(key="completed", label="Completed Today", value=completed_today, sub="Finished today"),
        ],
        today_items=[
            RoleDashboardListItem(
                id=str(o.id),
                title=o.scan_name or o.order_no,
                subtitle=o.patient.name if o.patient else None,
                meta=o.order_no,
                status=o.status.value if o.status else None,
                time=_fmt_time(o.scheduled_at or o.ordered_at) if (o.scheduled_at or o.ordered_at) else None,
            )
            for o in scheduled_today
        ],
        upcoming_items=[
            RoleDashboardListItem(
                id=str(o.id),
                title=o.scan_name or o.order_no,
                subtitle=o.patient.name if o.patient else None,
                meta="Report pending",
                status="report_pending",
                time=_fmt_time(o.completed_at or o.ordered_at) if (o.completed_at or o.ordered_at) else None,
            )
            for o in awaiting_report
        ],
        recent_items=[
            RoleDashboardListItem(
                id=str(o.id),
                title=o.scan_name or o.order_no,
                subtitle=o.patient.name if o.patient else None,
                meta=o.order_no,
                status="completed",
                time=_fmt_time(o.completed_at or o.ordered_at) if (o.completed_at or o.ordered_at) else None,
            )
            for o in recently_completed
        ],
        activity_items=[],
        quick_actions=[
            {"id": "create_order", "label": "Create Order", "module": "radiology", "section": "orders"},
            {"id": "start_scan", "label": "Start Scan", "module": "radiology", "section": "orders"},
            {"id": "upload_report", "label": "Upload Report", "module": "radiology", "section": "orders"},
        ],
    )


def _build_ot_dashboard(db: Session, hospital_id: UUID, user: dict) -> RoleDashboardResponse:
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)
    week_end = datetime.combine(today + timedelta(days=7), datetime.max.time()).replace(tzinfo=timezone.utc)

    today_rows = (
        db.query(OtSurgery)
        .options(joinedload(OtSurgery.patient), joinedload(OtSurgery.ot_room_ref), joinedload(OtSurgery.surgeon))
        .filter(
            OtSurgery.hospital_id == hospital_id,
            OtSurgery.scheduled_at >= start,
            OtSurgery.scheduled_at <= end,
        )
        .order_by(OtSurgery.scheduled_at.asc())
        .all()
    )
    ongoing = [s for s in today_rows if s.status == OtSurgeryStatus.in_progress]
    scheduled = [s for s in today_rows if s.status in (OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed)]
    completed = [s for s in today_rows if s.status == OtSurgeryStatus.completed]
    cancelled = [s for s in today_rows if s.status == OtSurgeryStatus.cancelled]

    upcoming = (
        db.query(OtSurgery)
        .options(joinedload(OtSurgery.patient), joinedload(OtSurgery.ot_room_ref))
        .filter(
            OtSurgery.hospital_id == hospital_id,
            OtSurgery.scheduled_at > end,
            OtSurgery.scheduled_at <= week_end,
            OtSurgery.status.in_([OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed]),
        )
        .order_by(OtSurgery.scheduled_at.asc())
        .limit(12)
        .all()
    )

    def _ot_item(s: OtSurgery) -> RoleDashboardListItem:
        room = s.ot_room_ref
        room_label = (room.code if room and room.code else None) or (room.name if room else None) or s.ot_room or "OT"
        return RoleDashboardListItem(
            id=str(s.id),
            title=s.surgery_type or s.surgery_no or "Surgery",
            subtitle=s.patient.name if s.patient else None,
            meta=room_label,
            status=s.status.value if s.status else None,
            time=_fmt_time(s.scheduled_at) if s.scheduled_at else None,
        )

    return RoleDashboardResponse(
        persona="ot",
        display_name=user.get("name") or "OT Staff",
        staff_role_name=user.get("staff_role_name"),
        metrics=[
            RoleDashboardMetric(key="today", label="Today's Surgeries", value=len(today_rows), sub="On today's list"),
            RoleDashboardMetric(key="ongoing", label="Ongoing", value=len(ongoing), sub="In progress now"),
            RoleDashboardMetric(key="scheduled", label="Scheduled", value=len(scheduled), sub="Yet to start"),
            RoleDashboardMetric(key="completed", label="Completed", value=len(completed), sub="Finished today"),
            RoleDashboardMetric(key="cancelled", label="Cancelled", value=len(cancelled), sub="Cancelled today"),
        ],
        today_items=[_ot_item(s) for s in today_rows if s.status != OtSurgeryStatus.cancelled][:15],
        upcoming_items=[_ot_item(s) for s in ongoing],
        recent_items=[_ot_item(s) for s in upcoming],
        activity_items=[],
        quick_actions=[
            {"id": "book_surgery", "label": "Book Surgery", "module": "ot", "section": "schedule"},
            {"id": "schedule", "label": "Open Schedule", "module": "ot", "section": "schedule"},
            {"id": "notes", "label": "Enter Notes", "module": "ot", "section": "notes"},
        ],
    )


def _build_billing_dashboard(db: Session, hospital_id: UUID, user: dict) -> RoleDashboardResponse:
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

    todays_charges = float(
        db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
            BillingCharge.created_at >= start,
            BillingCharge.created_at <= end,
        )
        .scalar()
        or 0
    )
    todays_collections = float(
        db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
        .filter(BillingPayment.hospital_id == hospital_id, BillingPayment.payment_date == today)
        .scalar()
        or 0
    )
    total_net = float(
        db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .scalar()
        or 0
    )
    total_paid = float(
        db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
        .filter(BillingPayment.hospital_id == hospital_id)
        .scalar()
        or 0
    )
    outstanding = max(0.0, total_net - total_paid)
    pending_charges = int(
        db.query(func.count(BillingCharge.id))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
        )
        .scalar()
        or 0
    )
    today_invoices = int(
        db.query(func.count(BillingInvoice.id))
        .filter(
            BillingInvoice.hospital_id == hospital_id,
            BillingInvoice.invoice_date == today,
            BillingInvoice.status != BillingInvoiceStatus.cancelled,
        )
        .scalar()
        or 0
    )
    today_receipts = int(
        db.query(func.count(BillingReceipt.id))
        .filter(
            BillingReceipt.hospital_id == hospital_id,
            BillingReceipt.payment_date == today,
            BillingReceipt.status != BillingReceiptStatus.cancelled,
        )
        .scalar()
        or 0
    )

    recent_charges = (
        db.query(BillingCharge)
        .options(joinedload(BillingCharge.patient))
        .filter(BillingCharge.hospital_id == hospital_id)
        .order_by(BillingCharge.created_at.desc())
        .limit(12)
        .all()
    )
    recent_payments = (
        db.query(BillingPayment)
        .options(joinedload(BillingPayment.patient))
        .filter(BillingPayment.hospital_id == hospital_id)
        .order_by(BillingPayment.created_at.desc())
        .limit(12)
        .all()
    )

    # Unpaid patients: open charges grouped by patient
    open_charges = (
        db.query(BillingCharge)
        .options(joinedload(BillingCharge.patient))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
        )
        .order_by(BillingCharge.created_at.desc())
        .limit(80)
        .all()
    )
    unpaid_map: dict[str, dict] = {}
    for c in open_charges:
        pid = str(c.patient_id)
        due = max(0.0, float(c.net_amount or 0) - float(c.amount_paid or 0))
        if pid not in unpaid_map:
            unpaid_map[pid] = {
                "id": pid,
                "title": c.patient.name if c.patient else "Patient",
                "subtitle": c.patient.uhid if c.patient else None,
                "due": 0.0,
            }
        unpaid_map[pid]["due"] += due
    unpaid_patients = sorted(unpaid_map.values(), key=lambda x: x["due"], reverse=True)[:12]

    return RoleDashboardResponse(
        persona="billing",
        display_name=user.get("name") or "Billing",
        staff_role_name=user.get("staff_role_name"),
        metrics=[
            RoleDashboardMetric(key="charges", label="Today's Charges", value=int(round(todays_charges)), sub="₹ charged today"),
            RoleDashboardMetric(
                key="collections", label="Today's Collections", value=int(round(todays_collections)), sub="₹ collected today"
            ),
            RoleDashboardMetric(key="outstanding", label="Outstanding", value=int(round(outstanding)), sub="Hospital balance"),
            RoleDashboardMetric(key="pending", label="Pending Charges", value=pending_charges, sub="Open charge lines"),
            RoleDashboardMetric(key="invoices", label="Today Invoices", value=today_invoices, sub="Generated today"),
            RoleDashboardMetric(key="receipts", label="Today Receipts", value=today_receipts, sub="Issued today"),
        ],
        today_items=[
            RoleDashboardListItem(
                id=str(c.id),
                title=c.description[:80],
                subtitle=c.patient.name if c.patient else None,
                meta=f"₹{float(c.net_amount):,.0f}",
                status=c.status.value if c.status else None,
                time=_fmt_time(c.created_at) if c.created_at else None,
            )
            for c in recent_charges
        ],
        upcoming_items=[
            RoleDashboardListItem(
                id=str(p.id),
                title=p.patient.name if p.patient else "Payment",
                subtitle=p.payment_method.value.replace("_", " ") if p.payment_method else None,
                meta=f"₹{float(p.amount):,.0f}",
                status="received",
                time=_fmt_date(p.payment_date),
            )
            for p in recent_payments
        ],
        recent_items=[
            RoleDashboardListItem(
                id=u["id"],
                title=u["title"],
                subtitle=u["subtitle"],
                meta=f"₹{u['due']:,.0f} due",
                status="outstanding",
            )
            for u in unpaid_patients
        ],
        activity_items=[],
        quick_actions=[
            {"id": "payment", "label": "Record Payment", "module": "billing", "section": "payments"},
            {"id": "invoice", "label": "Generate Invoice", "module": "billing", "section": "ledger"},
            {"id": "ledger", "label": "Open Ledger", "module": "billing", "section": "ledger"},
        ],
    )


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


@router.get("/me/dashboard", response_model=HospitalDashboardResponse)
def hospital_dashboard(
    db: Session = Depends(get_db),
    hospital_id=Depends(get_hospital_context),
    _: dict = Depends(require_hospital_user),
):
    hospital = db.query(Hospital).filter(Hospital.id == hospital_id).first()
    if not hospital:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")

    staff_users = (
        db.query(HospitalUser)
        .options(joinedload(HospitalUser.role))
        .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
        .all()
    )
    staff_count = len(staff_users)
    doctor_count = sum(1 for u in staff_users if _is_doctor_role(u.role.name if u.role else None))

    patient_count = int(
        db.query(func.count(Patient.id)).filter(Patient.hospital_id == hospital_id).scalar() or 0
    )
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

    appointments_today = int(
        db.query(func.count(Appointment.id))
        .filter(Appointment.hospital_id == hospital_id, Appointment.appointment_date == today)
        .scalar()
        or 0
    )
    active_admissions = int(
        db.query(func.count(Admission.id))
        .filter(
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.admitted,
        )
        .scalar()
        or 0
    )
    beds_total = int(
        db.query(func.count(Bed.id))
        .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
        .scalar()
        or 0
    )
    beds_occupied = int(
        db.query(func.count(Bed.id))
        .filter(
            Bed.hospital_id == hospital_id,
            Bed.is_active.is_(True),
            Bed.is_occupied.is_(True),
        )
        .scalar()
        or 0
    )
    occupied_pct = int(round((beds_occupied / beds_total) * 100)) if beds_total else 0
    patients_registered_today = int(
        db.query(func.count(Patient.id))
        .filter(Patient.hospital_id == hospital_id, Patient.created_at >= start, Patient.created_at <= end)
        .scalar()
        or 0
    )
    lab_orders_today = int(
        db.query(func.count(LabOrder.id))
        .filter(
            LabOrder.hospital_id == hospital_id,
            LabOrder.ordered_at >= start,
            LabOrder.ordered_at <= end,
            LabOrder.status != LabOrderStatus.cancelled,
        )
        .scalar()
        or 0
    )
    radiology_orders_today = int(
        db.query(func.count(RadiologyOrder.id))
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.ordered_at >= start,
            RadiologyOrder.ordered_at <= end,
            RadiologyOrder.status != RadiologyOrderStatus.cancelled,
        )
        .scalar()
        or 0
    )
    ot_surgeries_today = int(
        db.query(func.count(OtSurgery.id))
        .filter(
            OtSurgery.hospital_id == hospital_id,
            OtSurgery.scheduled_at >= start,
            OtSurgery.scheduled_at <= end,
        )
        .scalar()
        or 0
    )
    charges_today = float(
        db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
            BillingCharge.created_at >= start,
            BillingCharge.created_at <= end,
        )
        .scalar()
        or 0
    )
    collections_today = float(
        db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
        .filter(BillingPayment.hospital_id == hospital_id, BillingPayment.payment_date == today)
        .scalar()
        or 0
    )
    total_net = float(
        db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .scalar()
        or 0
    )
    total_paid = float(
        db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
        .filter(BillingPayment.hospital_id == hospital_id)
        .scalar()
        or 0
    )

    recent_registrations = (
        db.query(Patient)
        .filter(Patient.hospital_id == hospital_id)
        .order_by(Patient.created_at.desc())
        .limit(8)
        .all()
    )
    upcoming_appts = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
        .filter(
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date >= today,
            Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show, AppointmentStatus.completed]),
        )
        .order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc())
        .limit(8)
        .all()
    )
    pending_lab = (
        db.query(LabOrder)
        .options(joinedload(LabOrder.patient))
        .filter(
            LabOrder.hospital_id == hospital_id,
            LabOrder.status.in_([LabOrderStatus.ordered, LabOrderStatus.sample_collected, LabOrderStatus.in_progress]),
        )
        .order_by(LabOrder.ordered_at.asc())
        .limit(8)
        .all()
    )
    pending_rad = (
        db.query(RadiologyOrder)
        .options(joinedload(RadiologyOrder.patient))
        .filter(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.status.in_([RadiologyOrderStatus.in_progress, RadiologyOrderStatus.completed]),
            RadiologyOrder.report_file_data.is_(None),
        )
        .order_by(RadiologyOrder.ordered_at.desc())
        .limit(8)
        .all()
    )

    return HospitalDashboardResponse(
        id=hospital.id,
        hospital_id=hospital.hospital_id,
        name=hospital.name,
        address=hospital.address,
        phone=hospital.phone,
        email=hospital.email,
        plan=hospital.plan,
        icon_url=hospital.icon_url,
        is_active=hospital.is_active,
        created_at=hospital.created_at,
        staff_count=staff_count,
        doctor_count=doctor_count,
        patient_count=patient_count,
        appointments_today=appointments_today,
        active_admissions=active_admissions,
        beds_total=beds_total,
        beds_occupied=beds_occupied,
        modules_available=len(BASIC_MODULE_KEYS),
        patients_registered_today=patients_registered_today,
        occupied_beds_pct=occupied_pct,
        lab_orders_today=lab_orders_today,
        radiology_orders_today=radiology_orders_today,
        ot_surgeries_today=ot_surgeries_today,
        charges_today=int(round(charges_today)),
        collections_today=int(round(collections_today)),
        outstanding_total=int(round(max(0.0, total_net - total_paid))),
        recent_registrations=[
            HospitalDashboardListItem(
                id=str(p.id),
                title=p.name,
                subtitle=getattr(p, "uhid", None),
                meta=p.mobile,
                status="registered",
                time=_fmt_date(p.created_at.date() if p.created_at else None),
            )
            for p in recent_registrations
        ],
        upcoming_appointments=[
            HospitalDashboardListItem(
                id=str(a.id),
                title=a.patient.name if a.patient else "Patient",
                subtitle=a.doctor.name if a.doctor else None,
                meta=_fmt_date(a.appointment_date),
                status=a.status.value if a.status else None,
                time=_fmt_time(a.appointment_time),
            )
            for a in upcoming_appts
        ],
        pending_lab_orders=[
            HospitalDashboardListItem(
                id=str(o.id),
                title=o.order_no,
                subtitle=o.patient.name if o.patient else None,
                meta=o.status.value.replace("_", " ") if o.status else None,
                status=o.status.value if o.status else None,
                time=_fmt_time(o.ordered_at) if o.ordered_at else None,
            )
            for o in pending_lab
        ],
        pending_radiology_reports=[
            HospitalDashboardListItem(
                id=str(o.id),
                title=o.scan_name or o.order_no,
                subtitle=o.patient.name if o.patient else None,
                meta=o.order_no,
                status="report_pending",
                time=_fmt_time(o.ordered_at) if o.ordered_at else None,
            )
            for o in pending_rad
        ],
    )


@router.get("/me/role-dashboard", response_model=RoleDashboardResponse)
def role_dashboard(
    db: Session = Depends(get_db),
    hospital_id: UUID = Depends(get_hospital_context),
    user: dict = Depends(require_hospital_user),
):
    """Lightweight role-specific home summary. Hospital-scoped via JWT."""
    persona = _detect_persona(user)
    if persona == "doctor":
        return _build_doctor_dashboard(db, hospital_id, user)
    if persona == "nurse":
        return _build_nurse_dashboard(db, hospital_id, user)
    if persona == "reception":
        return _build_reception_dashboard(db, hospital_id, user)
    if persona == "lab":
        return _build_lab_dashboard(db, hospital_id, user)
    if persona == "radiology":
        return _build_radiology_dashboard(db, hospital_id, user)
    if persona == "ot":
        return _build_ot_dashboard(db, hospital_id, user)
    if persona == "billing":
        return _build_billing_dashboard(db, hospital_id, user)

    # Admin / generic staff: thin redirect payload; frontend keeps HospitalHomeDashboard
    return RoleDashboardResponse(
        persona=persona,
        display_name=user.get("name") or "User",
        staff_role_name=user.get("staff_role_name"),
        metrics=[],
        quick_actions=[],
    )


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
