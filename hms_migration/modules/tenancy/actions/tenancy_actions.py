"""
Tenancy and Hospital domain actions and business logic.

Conforms to UltrionTech-Backend-Template modules/tenancy/actions/ specification.
Provides complete business implementation for hospital management,
analytics dashboards, and role-scoped home dashboards.
"""

from __future__ import annotations

from collections.abc import Callable
import concurrent.futures
from datetime import date, datetime, timedelta, timezone
import secrets
import string
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from hms_migration.modules.admin.contracts.admin_contracts import BASIC_MODULE_KEYS
from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.appointments.entities.enums import AppointmentStatus
from hms_migration.modules.beds.entities.bed import Bed, Ward
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPayment,
    BillingReceipt,
    BillingReceiptStatus,
    BillingSourceType,
)
from hms_migration.modules.doctors.entities.doctor import HospitalUser, StaffRole
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestStatus,
)
from hms_migration.modules.ot.entities.ot_entities import OtRoom, OtSurgery, OtSurgeryStatus
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
)
from hms_migration.modules.tenancy.contracts.tenancy_contracts import (
    DoctorRecentRevenueItem,
    HospitalCreate,
    HospitalCreateResponse,
    HospitalDashboardListItem,
    HospitalDashboardResponse,
    HospitalDashboardSummaryResponse,
    HospitalResponse,
    RoleDashboardListItem,
    RoleDashboardMetric,
    RoleDashboardResponse,
)
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth.security import generate_temp_password, hash_password

HOSPITAL_ID_CHARS = string.ascii_uppercase + string.digits


def generate_hospital_id(db: Session) -> str:
    for _ in range(20):
        code = "HMS-" + "".join(secrets.choice(HOSPITAL_ID_CHARS) for _ in range(6))
        exists = db.query(Hospital.id).filter(Hospital.hospital_id == code).first()
        if not exists:
            return code
    raise RuntimeError("Unable to generate unique hospital ID")


def _is_doctor_role(name: str | None) -> bool:
    return bool(name and "doctor" in name.lower())


def _normalize_role(name: str | None) -> str:
    return (name or "").strip().lower().replace("-", " ").replace("_", " ")


def _detect_persona(user: dict[str, Any]) -> str:
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


def _fmt_time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    return str(value)[:5]


def _fmt_date(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)[:10]


def _day_start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time()).replace(tzinfo=timezone.utc)


def _day_end(d: date) -> datetime:
    return datetime.combine(d, datetime.max.time()).replace(tzinfo=timezone.utc)


def _status_display_label(st: Any) -> str | None:
    if not st:
        return None
    val = st.value if hasattr(st, "value") else str(st)
    return "in_progress" if val == "waiting" else val


class TenancyActions:
    def __init__(self, db: Session) -> None:
        self.db = db

    # Caps how many NEW DB connections one _run_parallel batch will open at
    # once. This dev DB server caps out at 50 total connections *shared
    # across every user session on the whole app*, not just this request —
    # a batch of 17 briefly used here during development tripped "remaining
    # connection slots are reserved for SUPERUSER" under only light
    # concurrent load. Keep batches well under that so one dashboard load
    # can't meaningfully dent the server's shared connection budget.
    _MAX_PARALLEL_CONNECTIONS = 8

    def _run_parallel(self, jobs: list[Callable[[Session], Any]]) -> list[Any]:
        """Run independent read-only queries concurrently, each on its own Session.

        SQLAlchemy Sessions are not thread-safe, so each job gets a fresh
        Session from the shared connection pool rather than reusing self.db.
        This exists because the dashboard-building actions below fire many
        queries that don't depend on each other's results (e.g. 12 separate
        "detail list" queries) but were running one after another. On this
        deployment, each round trip to the DB costs ~800ms of network latency
        regardless of query complexity (measured directly — a no-DB endpoint
        responds in ~4ms, a trivial single-row query takes ~800ms), so the
        real lever is collapsing N sequential round trips into one overlapped
        batch, not further query tuning.

        Concurrency is capped at _MAX_PARALLEL_CONNECTIONS: the session for
        each job is opened lazily inside the worker thread (not eagerly
        before submission), so limiting max_workers actually limits how many
        new connections can be in flight at once, rather than just how many
        threads process already-open ones.

        New sessions are bound to self.db's own engine (via sessionmaker,
        not the global get_transitional_sync_session_factory()) so this
        works correctly under test fixtures that override self.db onto a
        different engine (e.g. SQLite), not just against the real DB.
        """
        if not jobs:
            return []
        factory = sessionmaker(bind=self.db.get_bind())
        results: list[Any] = [None] * len(jobs)
        max_workers = min(len(jobs), self._MAX_PARALLEL_CONNECTIONS)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(TenancyActions._run_one, job, factory): idx for idx, job in enumerate(jobs)
            }
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()
        return results

    @staticmethod
    def _run_one(job: Callable[[Session], Any], factory: sessionmaker[Session]) -> Any:
        session = factory()
        try:
            return job(session)
        finally:
            session.close()

    # ── CRUD Operations ───────────────────────────────────────────────────────
    def create_hospital(self, payload: HospitalCreate) -> HospitalCreateResponse:
        email = payload.email.strip().lower()
        existing = self.db.query(Hospital).filter(Hospital.email == email).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A hospital with this email already exists.",
            )

        generated_password = generate_temp_password(5)
        hospital = Hospital(
            hospital_id=generate_hospital_id(self.db),
            name=payload.name.strip(),
            address=payload.address.strip(),
            phone=payload.phone.strip(),
            email=email,
            password_hash=hash_password(generated_password),
            plan=payload.plan,
            icon_url=payload.icon_url,
        )
        self.db.add(hospital)
        self.db.commit()
        self.db.refresh(hospital)

        return HospitalCreateResponse(
            **HospitalResponse.model_validate(hospital).model_dump(),
            generated_password=generated_password,
        )

    def list_hospitals(self, search: str | None = None) -> list[HospitalResponse]:
        query = self.db.query(Hospital).order_by(Hospital.created_at.desc())
        if search:
            term = f"%{search.strip().lower()}%"
            query = query.filter(
                (Hospital.name.ilike(term))
                | (Hospital.email.ilike(term))
                | (Hospital.hospital_id.ilike(term))
            )
        rows = query.all()
        return [HospitalResponse.model_validate(r) for r in rows]

    def get_hospital(self, hospital_uuid: str) -> HospitalResponse:
        try:
            uid = UUID(str(hospital_uuid))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")
        hospital = self.db.query(Hospital).filter(Hospital.id == uid).first()
        if not hospital:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")
        return HospitalResponse.model_validate(hospital)

    def delete_hospital(self, hospital_uuid: str) -> None:
        try:
            uid = UUID(str(hospital_uuid))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")
        hospital = self.db.query(Hospital).filter(Hospital.id == uid).first()
        if not hospital:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")
        self.db.delete(hospital)
        self.db.commit()

    # ── Role Dashboard Logic ──────────────────────────────────────────────────
    def get_role_dashboard(self, hospital_id: UUID, user: dict[str, Any]) -> RoleDashboardResponse:
        persona = _detect_persona(user)
        if persona == "doctor":
            return self._build_doctor_dashboard(hospital_id, user)
        if persona == "nurse":
            return self._build_nurse_dashboard(hospital_id, user)
        if persona == "reception":
            return self._build_reception_dashboard(hospital_id, user)
        if persona == "lab":
            return self._build_lab_dashboard(hospital_id, user)
        if persona == "radiology":
            return self._build_radiology_dashboard(hospital_id, user)
        if persona == "ot":
            return self._build_ot_dashboard(hospital_id, user)
        if persona == "billing":
            return self._build_billing_dashboard(hospital_id, user)

        # Admin / generic staff fallback
        return RoleDashboardResponse(
            persona=persona,
            display_name=user.get("name") or "User",
            staff_role_name=user.get("staff_role_name"),
            metrics=[],
            quick_actions=[],
        )

    def _doctor_practice_performance(self, hospital_id: UUID, doctor_id: UUID | None) -> dict[str, Any]:
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
                self.db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
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
            self.db.query(func.count(Appointment.id))
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

        recent_charges = (
            self.db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.source_type == BillingSourceType.consultation,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .order_by(BillingCharge.created_at.desc())
            .limit(10)
            .all()
        )
        recent_revenue: list[DoctorRecentRevenueItem] = []
        if recent_charges:
            appt_ids = {c.source_id for c in recent_charges if c.source_id}
            appts = (
                self.db.query(Appointment)
                .filter(Appointment.id.in_(appt_ids), Appointment.doctor_id == doctor_id)
                .all()
            )
            appt_map = {a.id: a for a in appts}
            patient_ids = {a.patient_id for a in appts if a.patient_id}
            patients = self.db.query(Patient).filter(Patient.id.in_(patient_ids)).all()
            patient_map = {p.id: p for p in patients}

            for charge in recent_charges:
                appt = appt_map.get(charge.source_id)
                if not appt:
                    continue
                patient = patient_map.get(appt.patient_id)
                recent_revenue.append(
                    DoctorRecentRevenueItem(
                        patient_name=patient.name if patient else "Patient",
                        appointment_date=_fmt_date(appt.appointment_date),
                        amount=round(float(charge.net_amount or 0), 2),
                        status=charge.status.value if charge.status else "pending",
                    )
                )

        return {
            "today_revenue": round(today_revenue, 2),
            "month_revenue": round(month_revenue, 2),
            "patients_this_month": patients_this_month,
            "average_revenue_per_patient": average_revenue_per_patient,
            "recent_revenue": recent_revenue,
        }

    def _build_doctor_dashboard(self, hospital_id: UUID, user: dict[str, Any]) -> RoleDashboardResponse:
        doctor_id_raw = user.get("user_id")
        doctor_id = UUID(str(doctor_id_raw)) if doctor_id_raw else None
        today = date.today()
        week_end = today + timedelta(days=7)

        today_q = self.db.query(Appointment).filter(
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

        upcoming_q = self.db.query(Appointment).filter(
            Appointment.hospital_id == hospital_id,
            Appointment.appointment_date > today,
            Appointment.appointment_date <= week_end,
            Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show]),
        )
        if doctor_id:
            upcoming_q = upcoming_q.filter(Appointment.doctor_id == doctor_id)
        upcoming_rows = upcoming_q.order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc()).limit(10).all()

        recent_q = self.db.query(Appointment).filter(
            Appointment.hospital_id == hospital_id,
            Appointment.status == AppointmentStatus.completed,
        )
        if doctor_id:
            recent_q = recent_q.filter(Appointment.doctor_id == doctor_id)
        recent_rows = recent_q.order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc()).limit(8).all()

        all_patient_ids = {a.patient_id for a in today_rows + upcoming_rows + recent_rows if a.patient_id}
        patient_map = {}
        if all_patient_ids:
            patients = self.db.query(Patient).filter(Patient.id.in_(all_patient_ids)).all()
            patient_map = {p.id: p for p in patients}

        ot_items: list[RoleDashboardListItem] = []
        if doctor_id:
            start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
            end = datetime.combine(week_end, datetime.max.time()).replace(tzinfo=timezone.utc)
            ot_rows = (
                self.db.query(OtSurgery)
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
            ot_patient_ids = {s.patient_id for s in ot_rows if s.patient_id}
            ot_room_ids = {s.ot_room_id for s in ot_rows if s.ot_room_id}
            ot_patients = {p.id: p for p in self.db.query(Patient).filter(Patient.id.in_(ot_patient_ids)).all()} if ot_patient_ids else {}
            ot_rooms = {r.id: r for r in self.db.query(OtRoom).filter(OtRoom.id.in_(ot_room_ids)).all()} if ot_room_ids else {}

            for s in ot_rows:
                room = ot_rooms.get(s.ot_room_id)
                patient = ot_patients.get(s.patient_id)
                room_label = (room.code if room and room.code else None) or (room.name if room else None) or s.ot_room or "OT"
                ot_items.append(
                    RoleDashboardListItem(
                        id=str(s.id),
                        title=s.surgery_type or "Surgery",
                        subtitle=patient.name if patient else None,
                        meta=room_label,
                        status=s.status.value if s.status else None,
                        time=_fmt_time(s.scheduled_at) if s.scheduled_at else None,
                    )
                )

        practice = None
        show_financial = True
        if doctor_id:
            doctor_row = self.db.query(HospitalUser).filter(HospitalUser.id == doctor_id, HospitalUser.hospital_id == hospital_id).first()
            show_financial = bool(getattr(doctor_row, "show_financial_details", True)) if doctor_row else True
            if show_financial:
                practice = self._doctor_practice_performance(hospital_id, doctor_id)

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
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=getattr(patient_map.get(a.patient_id), "uhid", None),
                    meta=a.visit_type,
                    status="in_progress" if a.status == AppointmentStatus.waiting else (a.status.value if a.status else None),
                    time=_fmt_time(a.appointment_time),
                )
                for a in today_rows
            ],
            upcoming_items=[
                RoleDashboardListItem(
                    id=str(a.id),
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
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
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=getattr(patient_map.get(a.patient_id), "uhid", None),
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
            today_revenue=practice["today_revenue"] if practice else None,
            month_revenue=practice["month_revenue"] if practice else None,
            patients_this_month=practice["patients_this_month"] if practice else None,
            average_revenue_per_patient=practice["average_revenue_per_patient"] if practice else None,
            recent_revenue=practice["recent_revenue"] if practice else None,
            show_financial_details=show_financial,
        )

    def _build_nurse_dashboard(self, hospital_id: UUID, user: dict[str, Any]) -> RoleDashboardResponse:
        today = date.today()
        start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

        admissions = (
            self.db.query(Admission)
            .filter(Admission.hospital_id == hospital_id, Admission.status == AdmissionStatus.admitted)
            .order_by(Admission.admitted_at.desc())
            .limit(20)
            .all()
        )
        beds_total_sq = (
            self.db.query(func.count(Bed.id))
            .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
            .scalar_subquery()
        )
        beds_occupied_sq = (
            self.db.query(func.count(Bed.id))
            .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True), Bed.is_occupied.is_(True))
            .scalar_subquery()
        )
        beds_total, beds_occupied = self.db.query(beds_total_sq, beds_occupied_sq).one()
        beds_total = int(beds_total or 0)
        beds_occupied = int(beds_occupied or 0)
        occupancy = int(round((beds_occupied / beds_total) * 100)) if beds_total else 0

        discharge_candidates = [
            a
            for a in admissions
            if a.admitted_at and (a.admitted_at.astimezone(timezone.utc).date() if a.admitted_at.tzinfo else a.admitted_at.date()) < today
        ][:12]

        recently_admitted = (
            self.db.query(Admission)
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

        all_adm = list({a.id: a for a in admissions + discharge_candidates + recently_admitted}.values())
        patient_ids = {a.patient_id for a in all_adm if a.patient_id}
        bed_ids = {a.bed_id for a in all_adm if a.bed_id}
        ward_ids = {a.ward_id for a in all_adm if a.ward_id}

        patient_map = {p.id: p for p in self.db.query(Patient).filter(Patient.id.in_(patient_ids)).all()} if patient_ids else {}
        bed_map = {b.id: b for b in self.db.query(Bed).filter(Bed.id.in_(bed_ids)).all()} if bed_ids else {}
        ward_map = {w.id: w for w in self.db.query(Ward).filter(Ward.id.in_(ward_ids)).all()} if ward_ids else {}

        def _adm_meta(a: Admission) -> str | None:
            ward = ward_map.get(a.ward_id)
            bed = bed_map.get(a.bed_id)
            return (ward.name if ward else None) or (bed.bed_code if bed else None)

        return RoleDashboardResponse(
            persona="nurse",
            display_name=user.get("name") or "Nurse",
            staff_role_name=user.get("staff_role_name"),
            metrics=[
                RoleDashboardMetric(key="admissions", label="Active Admissions", value=len(admissions), sub="Current inpatients"),
                RoleDashboardMetric(key="beds_occupied", label="Beds Occupied", value=beds_occupied, sub=f"{occupancy}% occupancy"),
                RoleDashboardMetric(key="beds_free", label="Beds Available", value=max(0, beds_total - beds_occupied), sub=f"of {beds_total} total"),
                RoleDashboardMetric(key="discharge_pending", label="Discharges Pending Today", value=len(discharge_candidates), sub="Multi-day stays to review"),
            ],
            today_items=[
                RoleDashboardListItem(
                    id=str(a.id),
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=getattr(patient_map.get(a.patient_id), "uhid", None),
                    meta=_adm_meta(a),
                    status=a.status.value if a.status else None,
                    time=_fmt_time(a.admitted_at) if a.admitted_at else None,
                )
                for a in admissions[:12]
            ],
            upcoming_items=[
                RoleDashboardListItem(
                    id=str(a.id),
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=getattr(patient_map.get(a.patient_id), "uhid", None),
                    meta=_adm_meta(a),
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
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=getattr(patient_map.get(a.patient_id), "uhid", None),
                    meta=_adm_meta(a),
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

    def _build_reception_dashboard(self, hospital_id: UUID, user: dict[str, Any]) -> RoleDashboardResponse:
        today = date.today()
        start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

        today_rows = (
            self.db.query(Appointment)
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
        no_shows_sq = (
            self.db.query(func.count(Appointment.id))
            .filter(
                Appointment.hospital_id == hospital_id,
                Appointment.appointment_date == today,
                Appointment.status == AppointmentStatus.no_show,
            )
            .scalar_subquery()
        )
        registrations_today_sq = (
            self.db.query(func.count(Patient.id))
            .filter(Patient.hospital_id == hospital_id, Patient.created_at >= start, Patient.created_at <= end)
            .scalar_subquery()
        )
        admissions_today_sq = (
            self.db.query(func.count(Admission.id))
            .filter(Admission.hospital_id == hospital_id, Admission.admitted_at >= start, Admission.admitted_at <= end)
            .scalar_subquery()
        )
        no_shows, registrations_today, admissions_today = self.db.query(
            no_shows_sq, registrations_today_sq, admissions_today_sq
        ).one()
        no_shows = int(no_shows or 0)
        registrations_today = int(registrations_today or 0)
        admissions_today = int(admissions_today or 0)

        recent_patients = (
            self.db.query(Patient)
            .filter(Patient.hospital_id == hospital_id)
            .order_by(Patient.created_at.desc())
            .limit(8)
            .all()
        )

        all_patient_ids = {a.patient_id for a in today_rows if a.patient_id}
        doctor_ids = {a.doctor_id for a in today_rows if a.doctor_id}
        patient_map = {p.id: p for p in self.db.query(Patient).filter(Patient.id.in_(all_patient_ids)).all()} if all_patient_ids else {}
        doctor_map = {d.id: d for d in self.db.query(HospitalUser).filter(HospitalUser.id.in_(doctor_ids)).all()} if doctor_ids else {}

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
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=doctor_map[a.doctor_id].name if a.doctor_id in doctor_map else None,
                    meta=a.visit_type,
                    status="in_progress" if a.status == AppointmentStatus.waiting else (a.status.value if a.status else None),
                    time=_fmt_time(a.appointment_time),
                )
                for a in today_rows[:12]
            ],
            upcoming_items=[
                RoleDashboardListItem(
                    id=str(a.id),
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=f"Token {getattr(a, 'queue_token', 'In progress')}",
                    meta=doctor_map[a.doctor_id].name if a.doctor_id in doctor_map else None,
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

    def _build_lab_dashboard(self, hospital_id: UUID, user: dict[str, Any]) -> RoleDashboardResponse:
        today = date.today()
        start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

        def _lab_count(*conds):
            return (
                self.db.query(func.count(LabOrder.id))
                .filter(LabOrder.hospital_id == hospital_id, *conds)
                .scalar_subquery()
            )

        todays_orders_sq = _lab_count(LabOrder.ordered_at >= start, LabOrder.ordered_at <= end)
        pending_sq = _lab_count(
            LabOrder.status.in_([LabOrderStatus.ordered, LabOrderStatus.sample_collected, LabOrderStatus.in_progress])
        )
        sample_collected_sq = _lab_count(LabOrder.status == LabOrderStatus.sample_collected)
        in_progress_sq = _lab_count(LabOrder.status == LabOrderStatus.in_progress)
        completed_today_sq = _lab_count(
            LabOrder.status == LabOrderStatus.completed,
            LabOrder.ordered_at >= start,
            LabOrder.ordered_at <= end,
        )
        (todays_orders, pending, sample_collected, in_progress, completed_today) = self.db.query(
            todays_orders_sq, pending_sq, sample_collected_sq, in_progress_sq, completed_today_sq
        ).one()
        todays_orders = int(todays_orders or 0)
        pending = int(pending or 0)
        sample_collected = int(sample_collected or 0)
        in_progress = int(in_progress or 0)
        completed_today = int(completed_today or 0)

        doctor_requests = (
            self.db.query(LabPrescriptionRequest)
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
            self.db.query(LabOrder)
            .filter(LabOrder.hospital_id == hospital_id, LabOrder.status == LabOrderStatus.ordered)
            .order_by(LabOrder.ordered_at.asc())
            .limit(12)
            .all()
        )
        in_progress_rows = (
            self.db.query(LabOrder)
            .filter(
                LabOrder.hospital_id == hospital_id,
                LabOrder.status.in_([LabOrderStatus.sample_collected, LabOrderStatus.in_progress]),
            )
            .order_by(LabOrder.ordered_at.desc())
            .limit(12)
            .all()
        )
        completed_rows = (
            self.db.query(LabOrder)
            .filter(LabOrder.hospital_id == hospital_id, LabOrder.status == LabOrderStatus.completed)
            .order_by(LabOrder.ordered_at.desc())
            .limit(10)
            .all()
        )

        patient_ids = {o.patient_id for o in waiting_sample + in_progress_rows + completed_rows if o.patient_id}
        patient_ids.update({r.patient_id for r in doctor_requests if r.patient_id})
        doctor_ids = {r.doctor_id for r in doctor_requests if r.doctor_id}

        patient_map = {p.id: p for p in self.db.query(Patient).filter(Patient.id.in_(patient_ids)).all()} if patient_ids else {}
        doctor_map = {d.id: d for d in self.db.query(HospitalUser).filter(HospitalUser.id.in_(doctor_ids)).all()} if doctor_ids else {}

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
                RoleDashboardMetric(key="doctor_req", label="Pending Doctor Requests", value=len(doctor_requests), sub="From prescriptions"),
            ],
            today_items=[
                RoleDashboardListItem(
                    id=str(r.id),
                    title=patient_map[r.patient_id].name if r.patient_id in patient_map else "Patient",
                    subtitle=getattr(patient_map.get(r.patient_id), "uhid", None),
                    meta=doctor_map[r.doctor_id].name if r.doctor_id in doctor_map else "Doctor request",
                    status=r.status.value if r.status else "pending",
                    time=_fmt_time(r.created_at) if r.created_at else None,
                )
                for r in doctor_requests
            ],
            upcoming_items=[
                RoleDashboardListItem(
                    id=str(o.id),
                    title=o.order_no,
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
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
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
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
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
                    meta="Completed",
                    status="completed",
                    time=_fmt_time(o.ordered_at) if o.ordered_at else None,
                )
                for o in completed_rows
            ],
            quick_actions=[
                {"id": "create_order", "label": "Create Order", "module": "laboratory", "section": "orders"},
                {"id": "collect", "label": "Collect Sample", "module": "laboratory", "section": "orders"},
                {"id": "results", "label": "Enter Results", "module": "laboratory", "section": "orders"},
            ],
        )

    def _build_radiology_dashboard(self, hospital_id: UUID, user: dict[str, Any]) -> RoleDashboardResponse:
        today = date.today()
        start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

        def _rad_count(*conds):
            return (
                self.db.query(func.count(RadiologyOrder.id))
                .filter(RadiologyOrder.hospital_id == hospital_id, *conds)
                .scalar_subquery()
            )

        todays_scans_sq = _rad_count(
            RadiologyOrder.ordered_at >= start,
            RadiologyOrder.ordered_at <= end,
            RadiologyOrder.status != RadiologyOrderStatus.cancelled,
        )
        pending_scans_sq = _rad_count(
            RadiologyOrder.status.in_(
                [RadiologyOrderStatus.ordered, RadiologyOrderStatus.scheduled, RadiologyOrderStatus.in_progress]
            )
        )
        scheduled_sq = _rad_count(RadiologyOrder.status == RadiologyOrderStatus.scheduled)
        reports_pending_sq = _rad_count(
            RadiologyOrder.status.in_([RadiologyOrderStatus.in_progress, RadiologyOrderStatus.completed]),
            RadiologyOrder.report_file_data.is_(None),
        )
        completed_today_sq = _rad_count(
            RadiologyOrder.status == RadiologyOrderStatus.completed,
            RadiologyOrder.ordered_at >= start,
            RadiologyOrder.ordered_at <= end,
        )
        (todays_scans, pending_scans, scheduled, reports_pending, completed_today) = self.db.query(
            todays_scans_sq, pending_scans_sq, scheduled_sq, reports_pending_sq, completed_today_sq
        ).one()
        todays_scans = int(todays_scans or 0)
        pending_scans = int(pending_scans or 0)
        scheduled = int(scheduled or 0)
        reports_pending = int(reports_pending or 0)
        completed_today = int(completed_today or 0)

        scheduled_today = (
            self.db.query(RadiologyOrder)
            .filter(
                RadiologyOrder.hospital_id == hospital_id,
                RadiologyOrder.status.in_([RadiologyOrderStatus.ordered, RadiologyOrderStatus.scheduled]),
            )
            .order_by(RadiologyOrder.scheduled_at.asc().nullslast(), RadiologyOrder.ordered_at.asc())
            .limit(12)
            .all()
        )
        awaiting_report = (
            self.db.query(RadiologyOrder)
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
            self.db.query(RadiologyOrder)
            .filter(RadiologyOrder.hospital_id == hospital_id, RadiologyOrder.status == RadiologyOrderStatus.completed)
            .order_by(RadiologyOrder.ordered_at.desc())
            .limit(10)
            .all()
        )

        patient_ids = {o.patient_id for o in scheduled_today + awaiting_report + recently_completed if o.patient_id}
        patient_map = {p.id: p for p in self.db.query(Patient).filter(Patient.id.in_(patient_ids)).all()} if patient_ids else {}

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
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
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
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
                    meta="Report pending",
                    status="report_pending",
                    time=_fmt_time(o.ordered_at) if o.ordered_at else None,
                )
                for o in awaiting_report
            ],
            recent_items=[
                RoleDashboardListItem(
                    id=str(o.id),
                    title=o.scan_name or o.order_no,
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
                    meta=o.order_no,
                    status="completed",
                    time=_fmt_time(o.ordered_at) if o.ordered_at else None,
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

    def _build_ot_dashboard(self, hospital_id: UUID, user: dict[str, Any]) -> RoleDashboardResponse:
        today = date.today()
        start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)
        week_end = datetime.combine(today + timedelta(days=7), datetime.max.time()).replace(tzinfo=timezone.utc)

        today_rows = (
            self.db.query(OtSurgery)
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
            self.db.query(OtSurgery)
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

        patient_ids = {s.patient_id for s in today_rows + upcoming if s.patient_id}
        room_ids = {s.ot_room_id for s in today_rows + upcoming if s.ot_room_id}
        patient_map = {p.id: p for p in self.db.query(Patient).filter(Patient.id.in_(patient_ids)).all()} if patient_ids else {}
        room_map = {r.id: r for r in self.db.query(OtRoom).filter(OtRoom.id.in_(room_ids)).all()} if room_ids else {}

        def _ot_item(s: OtSurgery) -> RoleDashboardListItem:
            room = room_map.get(s.ot_room_id)
            patient = patient_map.get(s.patient_id)
            room_label = (room.code if room and room.code else None) or (room.name if room else None) or s.ot_room or "OT"
            return RoleDashboardListItem(
                id=str(s.id),
                title=s.surgery_type or s.surgery_no or "Surgery",
                subtitle=patient.name if patient else None,
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

    def _build_billing_dashboard(self, hospital_id: UUID, user: dict[str, Any]) -> RoleDashboardResponse:
        today = date.today()
        start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        end = datetime.combine(today, datetime.max.time()).replace(tzinfo=timezone.utc)

        todays_charges_sq = (
            self.db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.status != BillingChargeStatus.cancelled,
                BillingCharge.created_at >= start,
                BillingCharge.created_at <= end,
            )
            .scalar_subquery()
        )
        todays_collections_sq = (
            self.db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
            .filter(BillingPayment.hospital_id == hospital_id, BillingPayment.payment_date == today)
            .scalar_subquery()
        )
        total_net_sq = (
            self.db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .scalar_subquery()
        )
        total_paid_sq = (
            self.db.query(func.coalesce(func.sum(BillingPayment.amount), 0.0))
            .filter(BillingPayment.hospital_id == hospital_id)
            .scalar_subquery()
        )
        pending_charges_sq = (
            self.db.query(func.count(BillingCharge.id))
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
            )
            .scalar_subquery()
        )
        today_invoices_sq = (
            self.db.query(func.count(BillingInvoice.id))
            .filter(
                BillingInvoice.hospital_id == hospital_id,
                BillingInvoice.invoice_date == today,
                BillingInvoice.status != BillingInvoiceStatus.cancelled,
            )
            .scalar_subquery()
        )
        today_receipts_sq = (
            self.db.query(func.count(BillingReceipt.id))
            .filter(
                BillingReceipt.hospital_id == hospital_id,
                BillingReceipt.payment_date == today,
                BillingReceipt.status != BillingReceiptStatus.cancelled,
            )
            .scalar_subquery()
        )

        (
            todays_charges,
            todays_collections,
            total_net,
            total_paid,
            pending_charges,
            today_invoices,
            today_receipts,
        ) = self.db.query(
            todays_charges_sq,
            todays_collections_sq,
            total_net_sq,
            total_paid_sq,
            pending_charges_sq,
            today_invoices_sq,
            today_receipts_sq,
        ).one()
        todays_charges = float(todays_charges or 0)
        todays_collections = float(todays_collections or 0)
        total_net = float(total_net or 0)
        total_paid = float(total_paid or 0)
        outstanding = max(0.0, total_net - total_paid)
        pending_charges = int(pending_charges or 0)
        today_invoices = int(today_invoices or 0)
        today_receipts = int(today_receipts or 0)

        recent_charges = (
            self.db.query(BillingCharge)
            .filter(BillingCharge.hospital_id == hospital_id)
            .order_by(BillingCharge.created_at.desc())
            .limit(12)
            .all()
        )
        recent_payments = (
            self.db.query(BillingPayment)
            .filter(BillingPayment.hospital_id == hospital_id)
            .order_by(BillingPayment.created_at.desc())
            .limit(12)
            .all()
        )

        open_charges = (
            self.db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.status.in_([BillingChargeStatus.pending, BillingChargeStatus.partially_paid]),
            )
            .order_by(BillingCharge.created_at.desc())
            .limit(80)
            .all()
        )

        patient_ids = {c.patient_id for c in recent_charges + open_charges if c.patient_id}
        patient_ids.update({p.patient_id for p in recent_payments if p.patient_id})
        patient_map = {p.id: p for p in self.db.query(Patient).filter(Patient.id.in_(patient_ids)).all()} if patient_ids else {}

        unpaid_map: dict[str, dict[str, Any]] = {}
        for c in open_charges:
            pid = str(c.patient_id)
            due = max(0.0, float(c.net_amount or 0) - float(c.amount_paid or 0))
            pat = patient_map.get(c.patient_id)
            if pid not in unpaid_map:
                unpaid_map[pid] = {
                    "id": pid,
                    "title": pat.name if pat else "Patient",
                    "subtitle": pat.uhid if pat else None,
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
                RoleDashboardMetric(key="collections", label="Today's Collections", value=int(round(todays_collections)), sub="₹ collected today"),
                RoleDashboardMetric(key="outstanding", label="Outstanding", value=int(round(outstanding)), sub="Hospital balance"),
                RoleDashboardMetric(key="pending", label="Pending Charges", value=pending_charges, sub="Open charge lines"),
                RoleDashboardMetric(key="invoices", label="Today Invoices", value=today_invoices, sub="Generated today"),
                RoleDashboardMetric(key="receipts", label="Today Receipts", value=today_receipts, sub="Issued today"),
            ],
            today_items=[
                RoleDashboardListItem(
                    id=str(c.id),
                    title=c.description[:80] if c.description else "Charge",
                    subtitle=patient_map[c.patient_id].name if c.patient_id in patient_map else None,
                    meta=f"₹{float(c.net_amount):,.0f}",
                    status=c.status.value if c.status else None,
                    time=_fmt_time(c.created_at) if c.created_at else None,
                )
                for c in recent_charges
            ],
            upcoming_items=[
                RoleDashboardListItem(
                    id=str(p.id),
                    title=patient_map[p.patient_id].name if p.patient_id in patient_map else "Payment",
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

    # ── Hospital Overall Dashboard ─────────────────────────────────────────────
    def get_hospital_dashboard(
        self,
        hospital_id: UUID,
        date_from: date | None = None,
        date_to: date | None = None,
        on_date: date | None = None,
        doctor_id: UUID | None = None,
        wing_id: UUID | None = None,
    ) -> HospitalDashboardResponse:
        hospital = self.db.query(Hospital).filter(Hospital.id == hospital_id).first()
        if not hospital:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hospital not found")

        staff_users = (
            self.db.query(HospitalUser)
            .filter(HospitalUser.hospital_id == hospital_id, HospitalUser.is_active.is_(True))
            .all()
        )
        staff_count = len(staff_users)
        role_ids = {u.role_id for u in staff_users if u.role_id}
        roles = {r.id: r for r in self.db.query(StaffRole).filter(StaffRole.id.in_(role_ids)).all()} if role_ids else {}
        doctor_count = sum(1 for u in staff_users if _is_doctor_role(roles.get(u.role_id).name if u.role_id in roles else None))

        today = date.today()
        if on_date and not date_from and not date_to:
            range_from = on_date
            range_to = on_date
        else:
            range_from = date_from or today
            range_to = date_to or range_from
        if range_to < range_from:
            range_from, range_to = range_to, range_from

        start = _day_start(range_from)
        end = _day_end(range_to)

        # Independent scalar counts merged into a single round trip. Each of
        # these used to be its own query; on tiny tables (tens-to-hundreds of
        # rows) network round-trip latency to the DB dominates over actual
        # query execution time, so collapsing 6 sequential round trips into 1
        # matters far more here than any index would.
        patient_count_stmt = select(func.count(Patient.id)).where(Patient.hospital_id == hospital_id)

        patients_today_stmt = select(func.count(Patient.id)).where(
            Patient.hospital_id == hospital_id,
            Patient.created_at >= start,
            Patient.created_at <= end,
        )

        admissions_stmt = select(func.count(Admission.id)).where(
            Admission.hospital_id == hospital_id,
            Admission.status == AdmissionStatus.admitted,
        )
        if doctor_id:
            admissions_stmt = admissions_stmt.where(Admission.doctor_id == doctor_id)
        if wing_id:
            admissions_stmt = admissions_stmt.join(Ward, Ward.id == Admission.ward_id).where(
                Ward.wing_id == wing_id
            )

        lab_today_stmt = select(func.count(LabOrder.id)).where(
            LabOrder.hospital_id == hospital_id,
            LabOrder.ordered_at >= start,
            LabOrder.ordered_at <= end,
            LabOrder.status != LabOrderStatus.cancelled,
        )
        if doctor_id:
            lab_today_stmt = lab_today_stmt.where(LabOrder.doctor_id == doctor_id)

        rad_today_stmt = select(func.count(RadiologyOrder.id)).where(
            RadiologyOrder.hospital_id == hospital_id,
            RadiologyOrder.ordered_at >= start,
            RadiologyOrder.ordered_at <= end,
            RadiologyOrder.status != RadiologyOrderStatus.cancelled,
        )
        if doctor_id:
            rad_today_stmt = rad_today_stmt.where(RadiologyOrder.doctor_id == doctor_id)

        ot_today_stmt = select(func.count(OtSurgery.id)).where(
            OtSurgery.hospital_id == hospital_id,
            OtSurgery.scheduled_at >= start,
            OtSurgery.scheduled_at <= end,
            OtSurgery.status != OtSurgeryStatus.cancelled,
        )
        if doctor_id:
            ot_today_stmt = ot_today_stmt.where(OtSurgery.surgeon_id == doctor_id)
        if wing_id:
            ot_today_stmt = ot_today_stmt.join(
                OtRoom, OtRoom.id == OtSurgery.ot_room_id, isouter=True
            ).where(OtRoom.wing_id == wing_id)

        # These 5 aggregate queries (merged-6-count, appointment counts, bed
        # counts, charges, payments) are independent of each other, so they
        # run as one parallel batch instead of 5 sequential round trips.
        def _job_merged_counts(db: Session) -> tuple[int, ...]:
            row = db.execute(
                select(
                    patient_count_stmt.scalar_subquery(),
                    patients_today_stmt.scalar_subquery(),
                    admissions_stmt.scalar_subquery(),
                    lab_today_stmt.scalar_subquery(),
                    rad_today_stmt.scalar_subquery(),
                    ot_today_stmt.scalar_subquery(),
                )
            ).one()
            return tuple(int(v or 0) for v in row)

        def _job_appt_counts(db: Session) -> tuple[int, int, int, int]:
            appt_base = db.query(Appointment).filter(
                Appointment.hospital_id == hospital_id,
                Appointment.appointment_date >= range_from,
                Appointment.appointment_date <= range_to,
                Appointment.status != AppointmentStatus.cancelled,
            )
            if doctor_id:
                appt_base = appt_base.filter(Appointment.doctor_id == doctor_id)
            if wing_id:
                appt_base = appt_base.filter(Appointment.wing_id == wing_id)
            row = appt_base.with_entities(
                func.count(Appointment.id),
                func.count(Appointment.id).filter(Appointment.status == AppointmentStatus.scheduled),
                func.count(Appointment.id).filter(Appointment.status == AppointmentStatus.waiting),
                func.count(Appointment.id).filter(Appointment.status == AppointmentStatus.completed),
            ).first() or (0, 0, 0, 0)
            return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0))

        def _job_bed_counts(db: Session) -> tuple[int, int]:
            beds_q = db.query(Bed).filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
            if wing_id:
                beds_q = beds_q.join(Ward, Ward.id == Bed.ward_id).filter(Ward.wing_id == wing_id)
            row = beds_q.with_entities(
                func.count(Bed.id),
                func.count(Bed.id).filter(Bed.is_occupied.is_(True)),
            ).first() or (0, 0)
            return (int(row[0] or 0), int(row[1] or 0))

        def _job_charges(db: Session) -> tuple[float, float]:
            row = (
                db.query(
                    func.coalesce(func.sum(BillingCharge.net_amount), 0.0),
                    func.coalesce(
                        func.sum(BillingCharge.net_amount).filter(
                            BillingCharge.created_at >= start, BillingCharge.created_at <= end
                        ),
                        0.0,
                    ),
                )
                .filter(
                    BillingCharge.hospital_id == hospital_id,
                    BillingCharge.status != BillingChargeStatus.cancelled,
                )
                .first()
                or (0.0, 0.0)
            )
            return (float(row[0] or 0), float(row[1] or 0))

        def _job_payments(db: Session) -> tuple[float, float]:
            row = (
                db.query(
                    func.coalesce(func.sum(BillingPayment.amount), 0.0),
                    func.coalesce(
                        func.sum(BillingPayment.amount).filter(
                            BillingPayment.payment_date >= range_from,
                            BillingPayment.payment_date <= range_to,
                        ),
                        0.0,
                    ),
                )
                .filter(BillingPayment.hospital_id == hospital_id)
                .first()
                or (0.0, 0.0)
            )
            return (float(row[0] or 0), float(row[1] or 0))

        # Lists — these 12 queries don't depend on each other, only on the
        # filter values above, so they run as one parallel batch (each on its
        # own Session) instead of 12 sequential round trips. See
        # _run_parallel's docstring for why this matters more than query
        # tuning at this point.
        def _job_recent_registrations(db: Session) -> list[Patient]:
            rows = (
                db.query(Patient)
                .filter(Patient.hospital_id == hospital_id, Patient.created_at >= start, Patient.created_at <= end)
                .order_by(Patient.created_at.desc())
                .limit(50)
                .all()
            )
            if not rows:
                rows = (
                    db.query(Patient)
                    .filter(Patient.hospital_id == hospital_id)
                    .order_by(Patient.created_at.desc())
                    .limit(8)
                    .all()
                )
            return rows

        def _job_upcoming_appts(db: Session) -> list[Appointment]:
            q = db.query(Appointment).filter(
                Appointment.hospital_id == hospital_id,
                Appointment.appointment_date >= range_from,
                Appointment.appointment_date <= range_to,
                Appointment.status.notin_([AppointmentStatus.cancelled, AppointmentStatus.no_show, AppointmentStatus.completed]),
            )
            if doctor_id:
                q = q.filter(Appointment.doctor_id == doctor_id)
            if wing_id:
                q = q.filter(Appointment.wing_id == wing_id)
            return q.order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc()).limit(8).all()

        def _job_pending_lab(db: Session) -> list[LabOrder]:
            q = db.query(LabOrder).filter(
                LabOrder.hospital_id == hospital_id,
                LabOrder.status.in_([LabOrderStatus.ordered, LabOrderStatus.sample_collected, LabOrderStatus.in_progress]),
            )
            if doctor_id:
                q = q.filter(LabOrder.doctor_id == doctor_id)
            return q.order_by(LabOrder.ordered_at.asc()).limit(8).all()

        def _job_pending_rad(db: Session) -> list[RadiologyOrder]:
            q = db.query(RadiologyOrder).filter(
                RadiologyOrder.hospital_id == hospital_id,
                RadiologyOrder.status.in_([RadiologyOrderStatus.in_progress, RadiologyOrderStatus.completed]),
                RadiologyOrder.report_file_data.is_(None),
            )
            if doctor_id:
                q = q.filter(RadiologyOrder.doctor_id == doctor_id)
            return q.order_by(RadiologyOrder.ordered_at.desc()).limit(8).all()

        def _job_appointments_detail(db: Session) -> list[Appointment]:
            q = db.query(Appointment).filter(
                Appointment.hospital_id == hospital_id,
                Appointment.appointment_date >= range_from,
                Appointment.appointment_date <= range_to,
                Appointment.status != AppointmentStatus.cancelled,
            )
            if doctor_id:
                q = q.filter(Appointment.doctor_id == doctor_id)
            if wing_id:
                q = q.filter(Appointment.wing_id == wing_id)
            return q.order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc()).limit(100).all()

        def _job_admissions_detail(db: Session) -> list[Admission]:
            q = db.query(Admission).filter(
                Admission.hospital_id == hospital_id,
                Admission.status == AdmissionStatus.admitted,
            )
            if doctor_id:
                q = q.filter(Admission.doctor_id == doctor_id)
            if wing_id:
                q = q.join(Ward, Ward.id == Admission.ward_id).filter(Ward.wing_id == wing_id)
            return q.order_by(Admission.admitted_at.desc()).limit(100).all()

        def _job_beds_detail(db: Session) -> list[Bed]:
            q = db.query(Bed).filter(
                Bed.hospital_id == hospital_id,
                Bed.is_active.is_(True),
                Bed.is_occupied.is_(True),
            )
            if wing_id:
                q = q.join(Ward, Ward.id == Bed.ward_id).filter(Ward.wing_id == wing_id)
            return q.limit(100).all()

        def _job_lab_detail(db: Session) -> list[LabOrder]:
            q = db.query(LabOrder).filter(
                LabOrder.hospital_id == hospital_id,
                LabOrder.ordered_at >= start,
                LabOrder.ordered_at <= end,
                LabOrder.status != LabOrderStatus.cancelled,
            )
            if doctor_id:
                q = q.filter(LabOrder.doctor_id == doctor_id)
            return q.order_by(LabOrder.ordered_at.desc()).limit(100).all()

        def _job_rad_detail(db: Session) -> list[RadiologyOrder]:
            q = db.query(RadiologyOrder).filter(
                RadiologyOrder.hospital_id == hospital_id,
                RadiologyOrder.ordered_at >= start,
                RadiologyOrder.ordered_at <= end,
                RadiologyOrder.status != RadiologyOrderStatus.cancelled,
            )
            if doctor_id:
                q = q.filter(RadiologyOrder.doctor_id == doctor_id)
            return q.order_by(RadiologyOrder.ordered_at.desc()).limit(100).all()

        def _job_ot_detail(db: Session) -> list[OtSurgery]:
            q = db.query(OtSurgery).filter(
                OtSurgery.hospital_id == hospital_id,
                OtSurgery.scheduled_at >= start,
                OtSurgery.scheduled_at <= end,
                OtSurgery.status != OtSurgeryStatus.cancelled,
            )
            if doctor_id:
                q = q.filter(OtSurgery.surgeon_id == doctor_id)
            if wing_id:
                q = q.outerjoin(OtRoom, OtRoom.id == OtSurgery.ot_room_id).filter(OtRoom.wing_id == wing_id)
            return q.order_by(OtSurgery.scheduled_at.desc()).limit(100).all()

        def _job_charges_detail(db: Session) -> list[BillingCharge]:
            return (
                db.query(BillingCharge)
                .filter(
                    BillingCharge.hospital_id == hospital_id,
                    BillingCharge.status != BillingChargeStatus.cancelled,
                    BillingCharge.created_at >= start,
                    BillingCharge.created_at <= end,
                )
                .order_by(BillingCharge.created_at.desc())
                .limit(100)
                .all()
            )

        def _job_collections_detail(db: Session) -> list[BillingPayment]:
            return (
                db.query(BillingPayment)
                .filter(
                    BillingPayment.hospital_id == hospital_id,
                    BillingPayment.payment_date >= range_from,
                    BillingPayment.payment_date <= range_to,
                )
                .order_by(BillingPayment.created_at.desc())
                .limit(100)
                .all()
            )

        # Wave A (5 aggregate jobs, defined above) and Wave B (these 12 list
        # jobs) don't depend on each other's results, only on the filter
        # values computed earlier. They were briefly merged into one 17-job
        # batch, but this Azure server caps out at 50 total connections
        # server-wide (shared across every user session, not just this
        # request) and a single dashboard load spiking to 17 simultaneous new
        # connections risks exhausting that under real concurrent traffic —
        # confirmed by hitting "remaining connection slots are reserved for
        # SUPERUSER" during testing. Kept as two separate batches (5 then 12)
        # to cap the peak simultaneous connections this endpoint opens.
        (
            (
                patient_count,
                patients_registered_today,
                active_admissions,
                lab_orders_today,
                radiology_orders_today,
                ot_surgeries_today,
            ),
            (appointments_today, appointments_scheduled, appointments_in_progress, appointments_completed),
            (beds_total, beds_occupied),
            (total_net, charges_today),
            (total_paid, collections_today),
        ) = self._run_parallel(
            [_job_merged_counts, _job_appt_counts, _job_bed_counts, _job_charges, _job_payments]
        )
        occupied_pct = int(round((beds_occupied / beds_total) * 100)) if beds_total else 0

        (
            recent_registrations_rows,
            pending_lab,
            pending_rad,
            appointments_detail_rows,
            admissions_detail_rows,
            beds_detail_rows,
            lab_detail_rows,
            rad_detail_rows,
            ot_detail_rows,
            charges_detail_rows,
            collections_detail_rows,
        ) = self._run_parallel(
            [
                _job_recent_registrations,
                _job_pending_lab,
                _job_pending_rad,
                _job_appointments_detail,
                _job_admissions_detail,
                _job_beds_detail,
                _job_lab_detail,
                _job_rad_detail,
                _job_ot_detail,
                _job_charges_detail,
                _job_collections_detail,
            ]
        )

        # upcoming_appts is a strict subset of appointments_detail_rows (same
        # hospital/date-range/doctor/wing filters, just narrower status
        # exclusion and a smaller limit) — derive it in Python instead of a
        # separate round trip. Falls back to a real query only if the detail
        # rows were truncated at their 100-row limit AND that wasn't enough
        # to find 8 upcoming ones, so behavior can't silently diverge for
        # unusually large multi-day ranges.
        _excluded_upcoming_statuses = {AppointmentStatus.cancelled, AppointmentStatus.no_show, AppointmentStatus.completed}
        upcoming_appts = [a for a in appointments_detail_rows if a.status not in _excluded_upcoming_statuses][:8]
        if len(upcoming_appts) < 8 and len(appointments_detail_rows) >= 100:
            upcoming_appts = _job_upcoming_appts(self.db)

        # Batch lookup patients, doctors, wards
        all_patient_ids = {
            p.id for p in recent_registrations_rows
        }
        for item_list in (
            upcoming_appts, appointments_detail_rows, admissions_detail_rows,
            lab_detail_rows, rad_detail_rows, ot_detail_rows, charges_detail_rows,
            collections_detail_rows, pending_lab, pending_rad
        ):
            all_patient_ids.update({getattr(x, "patient_id", None) for x in item_list if getattr(x, "patient_id", None)})

        doctor_ids = {a.doctor_id for a in upcoming_appts + appointments_detail_rows if getattr(a, "doctor_id", None)}
        doctor_ids.update({s.surgeon_id for s in ot_detail_rows if getattr(s, "surgeon_id", None)})
        ward_ids = {a.ward_id for a in admissions_detail_rows if getattr(a, "ward_id", None)}
        ward_ids.update({b.ward_id for b in beds_detail_rows if getattr(b, "ward_id", None)})
        bed_ids = {a.bed_id for a in admissions_detail_rows if getattr(a, "bed_id", None)}

        # Only `.name` (and `.bed_code` for beds) is read from these maps below —
        # select just those columns instead of full ORM rows to cut both DB and
        # serialization work for a query already fetching several id sets. The
        # four lookups are independent of each other, so run them as one
        # parallel batch instead of 4 sequential round trips.
        lookup_jobs: list[Callable[[Session], Any]] = []
        lookup_names: list[str] = []
        if all_patient_ids:
            lookup_jobs.append(lambda db: db.query(Patient.id, Patient.name).filter(Patient.id.in_(all_patient_ids)).all())
            lookup_names.append("patient")
        if doctor_ids:
            lookup_jobs.append(lambda db: db.query(HospitalUser.id, HospitalUser.name).filter(HospitalUser.id.in_(doctor_ids)).all())
            lookup_names.append("doctor")
        if ward_ids:
            lookup_jobs.append(lambda db: db.query(Ward.id, Ward.name).filter(Ward.id.in_(ward_ids)).all())
            lookup_names.append("ward")
        if bed_ids:
            lookup_jobs.append(lambda db: db.query(Bed.id, Bed.bed_code).filter(Bed.id.in_(bed_ids)).all())
            lookup_names.append("bed")

        lookup_results = dict(zip(lookup_names, self._run_parallel(lookup_jobs)))
        patient_map = {row.id: row for row in lookup_results.get("patient", [])}
        doctor_map = {row.id: row for row in lookup_results.get("doctor", [])}
        ward_map = {row.id: row for row in lookup_results.get("ward", [])}
        bed_map = {row.id: row for row in lookup_results.get("bed", [])}

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
            filter_date=on_date,
            filter_date_from=range_from,
            filter_date_to=range_to,
            filter_doctor_id=doctor_id,
            filter_wing_id=wing_id,
            patients_registered_today=patients_registered_today,
            occupied_beds_pct=occupied_pct,
            appointments_scheduled=appointments_scheduled,
            appointments_in_progress=appointments_in_progress,
            appointments_completed=appointments_completed,
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
                    time=_fmt_time(p.created_at) if p.created_at else None,
                )
                for p in recent_registrations_rows
            ],
            upcoming_appointments=[
                HospitalDashboardListItem(
                    id=str(a.id),
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=doctor_map[a.doctor_id].name if a.doctor_id in doctor_map else None,
                    meta=f"{_fmt_date(a.appointment_date)} · {a.purpose}" if a.purpose else _fmt_date(a.appointment_date),
                    status=_status_display_label(a.status),
                    time=_fmt_time(a.appointment_time),
                )
                for a in upcoming_appts
            ],
            pending_lab_orders=[
                HospitalDashboardListItem(
                    id=str(o.id),
                    title=o.order_no,
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
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
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
                    meta=o.order_no,
                    status="report_pending",
                    time=_fmt_time(o.ordered_at) if o.ordered_at else None,
                )
                for o in pending_rad
            ],
            appointments_detail=[
                HospitalDashboardListItem(
                    id=str(a.id),
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=doctor_map[a.doctor_id].name if a.doctor_id in doctor_map else None,
                    meta=f"{_fmt_date(a.appointment_date)} · {a.purpose}" if a.purpose else _fmt_date(a.appointment_date),
                    status=_status_display_label(a.status),
                    time=_fmt_time(a.appointment_time),
                )
                for a in appointments_detail_rows
            ],
            admissions_detail=[
                HospitalDashboardListItem(
                    id=str(a.id),
                    title=patient_map[a.patient_id].name if a.patient_id in patient_map else "Patient",
                    subtitle=ward_map[a.ward_id].name if a.ward_id in ward_map else None,
                    meta=f"Bed {bed_map[a.bed_id].bed_code}" if a.bed_id in bed_map else None,
                    status=a.status.value if a.status else None,
                    time=_fmt_date(a.admitted_at.date() if a.admitted_at else None),
                )
                for a in admissions_detail_rows
            ],
            beds_detail=[
                HospitalDashboardListItem(
                    id=str(b.id),
                    title=f"Bed {b.bed_code}",
                    subtitle=ward_map[b.ward_id].name if b.ward_id in ward_map else None,
                    meta=None,
                    status="occupied",
                    time=None,
                )
                for b in beds_detail_rows
            ],
            lab_orders_detail=[
                HospitalDashboardListItem(
                    id=str(o.id),
                    title=o.order_no,
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
                    meta=o.status.value.replace("_", " ") if o.status else None,
                    status=o.status.value if o.status else None,
                    time=_fmt_time(o.ordered_at) if o.ordered_at else None,
                )
                for o in lab_detail_rows
            ],
            radiology_orders_detail=[
                HospitalDashboardListItem(
                    id=str(o.id),
                    title=o.scan_name or o.order_no,
                    subtitle=patient_map[o.patient_id].name if o.patient_id in patient_map else None,
                    meta=o.order_no,
                    status=o.status.value if o.status else None,
                    time=_fmt_time(o.ordered_at) if o.ordered_at else None,
                )
                for o in rad_detail_rows
            ],
            ot_surgeries_detail=[
                HospitalDashboardListItem(
                    id=str(s.id),
                    title=s.surgery_type or s.surgery_no,
                    subtitle=patient_map[s.patient_id].name if s.patient_id in patient_map else None,
                    meta=doctor_map[s.surgeon_id].name if s.surgeon_id in doctor_map else s.ot_room,
                    status=s.status.value if s.status else None,
                    time=_fmt_time(s.scheduled_at) if s.scheduled_at else None,
                )
                for s in ot_detail_rows
            ],
            charges_detail=[
                HospitalDashboardListItem(
                    id=str(c.id),
                    title=c.description[:80] if c.description else "Charge",
                    subtitle=patient_map[c.patient_id].name if c.patient_id in patient_map else None,
                    meta=f"₹{int(round(c.net_amount)):,}",
                    status=c.status.value if c.status else None,
                    time=_fmt_time(c.created_at) if c.created_at else None,
                )
                for c in charges_detail_rows
            ],
            collections_detail=[
                HospitalDashboardListItem(
                    id=str(p.id),
                    title=patient_map[p.patient_id].name if p.patient_id in patient_map else "Payment",
                    subtitle=p.payment_method.value if p.payment_method else None,
                    meta=f"₹{int(round(p.amount)):,}",
                    status="paid",
                    time=_fmt_time(p.created_at) if p.created_at else None,
                )
                for p in collections_detail_rows
            ],
        )

    def get_hospital_dashboard_summary(self, hospital_id: UUID) -> HospitalDashboardSummaryResponse:
        """Counters-only dashboard view: ~8 queries instead of the ~19 the full
        dashboard runs, none of which build detail lists or cross-reference
        patients/doctors/wards/beds. Meant for call sites (e.g. a sidebar
        badge) that only display a couple of numbers, not the full screen.
        """
        today = date.today()
        start = _day_start(today)
        end = _day_end(today)

        patient_count = int(
            self.db.query(func.count(Patient.id)).filter(Patient.hospital_id == hospital_id).scalar() or 0
        )

        appt_counts = (
            self.db.query(
                func.count(Appointment.id),
                func.count(Appointment.id).filter(Appointment.status == AppointmentStatus.scheduled),
                func.count(Appointment.id).filter(Appointment.status == AppointmentStatus.waiting),
                func.count(Appointment.id).filter(Appointment.status == AppointmentStatus.completed),
            )
            .filter(
                Appointment.hospital_id == hospital_id,
                Appointment.appointment_date == today,
                Appointment.status != AppointmentStatus.cancelled,
            )
            .first()
            or (0, 0, 0, 0)
        )

        active_admissions = int(
            self.db.query(func.count(Admission.id))
            .filter(Admission.hospital_id == hospital_id, Admission.status == AdmissionStatus.admitted)
            .scalar()
            or 0
        )

        bed_counts = (
            self.db.query(
                func.count(Bed.id),
                func.count(Bed.id).filter(Bed.is_occupied.is_(True)),
            )
            .filter(Bed.hospital_id == hospital_id, Bed.is_active.is_(True))
            .first()
            or (0, 0)
        )
        beds_total, beds_occupied = int(bed_counts[0] or 0), int(bed_counts[1] or 0)

        patients_registered_today = int(
            self.db.query(func.count(Patient.id))
            .filter(Patient.hospital_id == hospital_id, Patient.created_at >= start, Patient.created_at <= end)
            .scalar()
            or 0
        )
        lab_orders_today = int(
            self.db.query(func.count(LabOrder.id))
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
            self.db.query(func.count(RadiologyOrder.id))
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
            self.db.query(func.count(OtSurgery.id))
            .filter(
                OtSurgery.hospital_id == hospital_id,
                OtSurgery.scheduled_at >= start,
                OtSurgery.scheduled_at <= end,
                OtSurgery.status != OtSurgeryStatus.cancelled,
            )
            .scalar()
            or 0
        )

        charges_row = (
            self.db.query(
                func.coalesce(func.sum(BillingCharge.net_amount), 0.0),
                func.coalesce(
                    func.sum(BillingCharge.net_amount).filter(
                        BillingCharge.created_at >= start, BillingCharge.created_at <= end
                    ),
                    0.0,
                ),
            )
            .filter(
                BillingCharge.hospital_id == hospital_id,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .first()
            or (0.0, 0.0)
        )
        total_net, charges_today = float(charges_row[0] or 0), float(charges_row[1] or 0)

        payments_row = (
            self.db.query(
                func.coalesce(func.sum(BillingPayment.amount), 0.0),
                func.coalesce(
                    func.sum(BillingPayment.amount).filter(BillingPayment.payment_date == today),
                    0.0,
                ),
            )
            .filter(BillingPayment.hospital_id == hospital_id)
            .first()
            or (0.0, 0.0)
        )
        total_paid, collections_today = float(payments_row[0] or 0), float(payments_row[1] or 0)

        return HospitalDashboardSummaryResponse(
            patient_count=patient_count,
            appointments_today=int(appt_counts[0] or 0),
            appointments_scheduled=int(appt_counts[1] or 0),
            appointments_in_progress=int(appt_counts[2] or 0),
            appointments_completed=int(appt_counts[3] or 0),
            active_admissions=active_admissions,
            beds_total=beds_total,
            beds_occupied=beds_occupied,
            occupied_beds_pct=int(round((beds_occupied / beds_total) * 100)) if beds_total else 0,
            patients_registered_today=patients_registered_today,
            lab_orders_today=lab_orders_today,
            radiology_orders_today=radiology_orders_today,
            ot_surgeries_today=ot_surgeries_today,
            charges_today=int(round(charges_today)),
            collections_today=int(round(collections_today)),
            outstanding_total=int(round(max(0.0, total_net - total_paid))),
        )
