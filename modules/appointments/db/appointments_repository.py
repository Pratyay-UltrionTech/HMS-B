"""
Database repository for the Appointments module.

Conforms to UltrionTech-Backend-Template modules/appointments/db/ specification.
Manages queries, status persistence, queue tokens, and encounter numbering for appointments.
"""

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Date, Integer, String, column, func, or_, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session, joinedload

from infrastructure.postgres.base_repository import BaseRepository
from modules.appointments.contracts.appointments_contracts import (
    AppointmentListItem,
)
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.appointment_type import AppointmentType
from modules.appointments.entities.enums import AppointmentStatus
from modules.patients.entities.patient import Patient

# SQLAlchemy Core table references for joined lookups
_hospital_users = table(
    "hospital_users",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("role_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("phone", String),
    column("email", String),
    column("specialization", String),
    column("qualification", String),
    column("medical_registration_number", String),
    column("years_of_experience", Integer),
    column("consultation_room", String),
    column("is_active", Boolean),
)

_staff_roles = table(
    "staff_roles",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("name", String),
)

_wings = table(
    "wings",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("is_active", Boolean),
)

_departments = table(
    "departments",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("is_active", Boolean),
    column("wing_id", PG_UUID(as_uuid=True)),
)

_admissions = table(
    "admissions",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("ip_id", String),
)


class AppointmentsRepository(BaseRepository[Appointment]):
    """Repository managing appointments persistence and composite lookups."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        super().__init__(session=db, hospital_id=hospital_id)
        self.db = db

    def get_by_id(self, appointment_id: UUID) -> Appointment | None:
        """Fetch an appointment by ID within current hospital tenant."""
        return (
            self.db.query(Appointment)
            .filter(
                Appointment.id == appointment_id,
                Appointment.hospital_id == self.hospital_id,
            )
            .first()
        )

    def next_encounter_id(self, kind: str = "OP") -> str:
        """Generate human-readable encounter sequence (e.g. OP-2026-00001)."""
        year = date.today().year
        prefix = f"{kind}-{year}-"
        current = (
            self.db.query(func.max(Appointment.op_id))
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.op_id.like(f"{prefix}%"),
            )
            .scalar()
        )
        seq = 0
        if current:
            try:
                seq = int(str(current).rsplit("-", 1)[-1])
            except ValueError:
                seq = 0
        return f"{prefix}{seq + 1:05d}"

    def next_queue_token(self, on_date: date, doctor_id: UUID) -> int:
        """Sequential queue token per doctor on a given date."""
        cur = (
            self.db.query(func.max(Appointment.queue_token))
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.doctor_id == doctor_id,
                Appointment.appointment_date == on_date,
            )
            .scalar()
        )
        return (cur or 0) + 1

    def list_today(
        self,
        doctor_id: UUID | None = None,
        status: AppointmentStatus | None = None,
    ) -> list[Appointment]:
        """List today's appointments ordered chronologically."""
        today = date.today()
        q = (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.appointment_date == today,
            )
        )
        if doctor_id:
            q = q.filter(Appointment.doctor_id == doctor_id)
        if status:
            q = q.filter(Appointment.status == status)
        return q.order_by(Appointment.appointment_time.asc()).all()

    def list_calendar(
        self,
        week_start: date,
        doctor_id: UUID | None = None,
    ) -> list[Appointment]:
        """List appointments for 7 days starting from week_start."""
        week_end = week_start + timedelta(days=7)
        q = (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.appointment_date >= week_start,
                Appointment.appointment_date < week_end,
                Appointment.status != AppointmentStatus.cancelled,
            )
        )
        if doctor_id:
            q = q.filter(Appointment.doctor_id == doctor_id)
        return q.order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc()).all()

    def list_queue(self, doctor_id: UUID | None = None) -> list[Appointment]:
        """List waiting / in-progress appointments for queue display."""
        today = date.today()
        q = (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.appointment_date == today,
                Appointment.status.in_([AppointmentStatus.waiting, AppointmentStatus.scheduled]),
            )
        )
        if doctor_id:
            q = q.filter(Appointment.doctor_id == doctor_id)
        return q.order_by(
            Appointment.queue_token.asc().nullslast(),
            Appointment.appointment_time.asc(),
        ).all()

    def list_history(
        self,
        patient_id: UUID | None = None,
        doctor_id: UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        status: AppointmentStatus | None = None,
        patient_search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Appointment]:
        """List historical appointments matching filters."""
        q = self.db.query(Appointment).filter(Appointment.hospital_id == self.hospital_id)
        if patient_id:
            q = q.filter(Appointment.patient_id == patient_id)
        if doctor_id:
            q = q.filter(Appointment.doctor_id == doctor_id)
        if from_date:
            q = q.filter(Appointment.appointment_date >= from_date)
        if to_date:
            q = q.filter(Appointment.appointment_date <= to_date)
        if status:
            q = q.filter(Appointment.status == status)
        if patient_search:
            term = f"%{patient_search.strip()}%"
            q = q.join(Patient, Patient.id == Appointment.patient_id).filter(
                or_(
                    Patient.name.ilike(term),
                    Patient.uhid.ilike(term),
                    Patient.mobile.ilike(term),
                )
            )
        return (
            q.order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    def list_ipd_requests(self) -> list[Appointment]:
        """List appointments requesting IPD bed transfer.
        Excludes appointments for patients who already have an open inpatient
        episode (requested/admitted/discharge_requested — Invariant 1).
        """
        from modules.inpatient.entities.admission import Admission, AdmissionStatus

        active_patient_ids = (
            self.db.query(Admission.patient_id)
            .filter(
                Admission.hospital_id == self.hospital_id,
                Admission.status.in_(
                    [
                        AdmissionStatus.requested,
                        AdmissionStatus.admitted,
                        AdmissionStatus.discharge_requested,
                    ]
                ),
            )
        )

        return (
            self.db.query(Appointment)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.status == AppointmentStatus.ipd_transfer_requested,
                ~Appointment.patient_id.in_(active_patient_ids),
            )
            .order_by(Appointment.created_at.desc())
            .all()
        )

    # -----------------------------------------------------------------------
    # Metadata Helper Queries
    # -----------------------------------------------------------------------
    def list_active_doctors(self) -> list[dict[str, Any]]:
        """List active doctors for appointment booking."""
        from modules.doctors.entities.doctor import HospitalUser
        from modules.doctors.services.doctor_eligibility import doctor_eligibility_clause
        fields = ("id", "name", "phone", "email", "specialization", "qualification",
                  "medical_registration_number", "years_of_experience", "consultation_room")
        stmt = select(*(getattr(HospitalUser, f) for f in fields)).where(
            doctor_eligibility_clause(self.hospital_id)
        ).order_by(HospitalUser.name)
        return [{**dict(r), "id": str(r["id"])} for r in self.db.execute(stmt).mappings().all()]

    def list_active_nurses(self) -> list[dict[str, Any]]:
        """List active nurses for assignment."""
        stmt = (
            select(
                _hospital_users.c.id,
                _hospital_users.c.name,
                _hospital_users.c.phone,
                _hospital_users.c.email,
            )
            .select_from(
                _hospital_users.join(
                    _staff_roles, _hospital_users.c.role_id == _staff_roles.c.id
                )
            )
            .where(
                _hospital_users.c.hospital_id == self.hospital_id,
                _hospital_users.c.is_active.is_(True),
                or_(
                    func.lower(_staff_roles.c.name).like("%nurse%"),
                    func.lower(_staff_roles.c.name).like("%nursing%"),
                ),
            )
            .order_by(_hospital_users.c.name.asc())
        )
        return [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "phone": r["phone"],
                "email": r["email"],
            }
            for r in self.db.execute(stmt).mappings().all()
        ]

    def list_wings(self) -> list[dict[str, Any]]:
        """List hospital wings."""
        stmt = (
            select(_wings.c.id, _wings.c.name)
            .where(_wings.c.hospital_id == self.hospital_id, _wings.c.is_active.is_(True))
            .order_by(_wings.c.name.asc())
        )
        return [dict(r) for r in self.db.execute(stmt).mappings().all()]

    def list_departments(self) -> list[dict[str, Any]]:
        """List hospital departments."""
        stmt = (
            select(_departments.c.id, _departments.c.name, _departments.c.wing_id)
            .where(_departments.c.hospital_id == self.hospital_id, _departments.c.is_active.is_(True))
            .order_by(_departments.c.name.asc())
        )
        return [dict(r) for r in self.db.execute(stmt).mappings().all()]

    def list_visit_types(self) -> list[dict[str, Any]]:
        """List active appointment types."""
        types = (
            self.db.query(AppointmentType)
            .filter(
                AppointmentType.hospital_id == self.hospital_id,
                AppointmentType.is_active.is_(True),
            )
            .order_by(AppointmentType.name.asc())
            .all()
        )
        return [
            {
                "id": str(t.id),
                "name": t.name,
                "slot_duration_minutes": t.slot_duration_minutes,
                "description": t.description,
                "is_follow_up": t.is_follow_up,
            }
            for t in types
        ]

    def hydrate_appointment_items(self, appointments: list[Appointment]) -> list[AppointmentListItem]:
        """Convert a list of Appointment ORM entities into AppointmentListItem DTOs."""
        if not appointments:
            return []

        # Collect foreign keys for bulk hydration
        patient_ids = list({a.patient_id for a in appointments if a.patient_id})
        user_ids = list(
            {a.doctor_id for a in appointments if a.doctor_id}
            | {a.nurse_id for a in appointments if a.nurse_id}
        )
        admission_ids = list({a.admission_id for a in appointments if a.admission_id})

        # Bulk fetch patients
        patients_map: dict[UUID, Any] = {}
        if patient_ids:
            p_rows = self.db.query(Patient).filter(Patient.id.in_(patient_ids)).all()
            patients_map = {p.id: p for p in p_rows}

        # Bulk fetch users (doctors/nurses)
        users_map: dict[UUID, str] = {}
        if user_ids:
            stmt = select(_hospital_users.c.id, _hospital_users.c.name).where(
                _hospital_users.c.id.in_(user_ids)
            )
            users_map = {r[0]: r[1] for r in self.db.execute(stmt).all()}

        # Bulk fetch admissions (for ip_id, ward, bed, status)
        admissions_map: dict[UUID, dict[str, Any]] = {}
        if admission_ids:
            try:
                from modules.inpatient.entities.admission import Admission
                adm_rows = (
                    self.db.query(Admission)
                    .options(
                        joinedload(Admission.ward),
                        joinedload(Admission.bed),
                    )
                    .filter(Admission.id.in_(admission_ids))
                    .all()
                )
                for adm in adm_rows:
                    adm_st = (
                        adm.status.value if hasattr(adm.status, "value") else str(adm.status)
                    ) if getattr(adm, "status", None) is not None else None
                    admissions_map[adm.id] = {
                        "ip_id": adm.ip_id,
                        "ward": adm.ward.name if adm.ward else None,
                        "bed": adm.bed.bed_code if adm.bed else None,
                        "status": adm_st,
                    }
            except Exception:
                stmt = select(_admissions.c.id, _admissions.c.ip_id).where(
                    _admissions.c.id.in_(admission_ids)
                )
                admissions_map = {
                    r[0]: {"ip_id": r[1], "ward": None, "bed": None, "status": None}
                    for r in self.db.execute(stmt).all()
                }

        # Bulk fetch ledger totals
        ledger_map: dict[UUID, dict[str, Any]] = {}
        ledger_error = False
        if patient_ids:
            try:
                from modules.billing.services.billing_service import patient_ledger_totals_bulk
                ledger_map = patient_ledger_totals_bulk(self.db, self.hospital_id, patient_ids)
            except Exception:
                ledger_map = {}
                ledger_error = True

        # Bulk fetch consultation settlement details (charges and receipts)
        settlement_map: dict[UUID, dict[str, Any]] = {}
        appt_ids = [a.id for a in appointments]
        if appt_ids:
            try:
                from modules.billing.entities.billing_entities import (
                    BillingCharge,
                    BillingPayment,
                    BillingPaymentAllocation,
                    BillingReceipt,
                    BillingReceiptStatus,
                    BillingSourceType,
                )
                charge_rows = (
                    self.db.query(BillingCharge)
                    .filter(
                        BillingCharge.hospital_id == self.hospital_id,
                        BillingCharge.source_type == BillingSourceType.consultation,
                        BillingCharge.source_id.in_(appt_ids),
                    )
                    .all()
                )
                if charge_rows:
                    c_by_appt = {c.source_id: c for c in charge_rows}
                    c_ids = [c.id for c in charge_rows]
                    alloc_rows = (
                        self.db.query(BillingPaymentAllocation, BillingPayment, BillingReceipt)
                        .join(BillingPayment, BillingPayment.id == BillingPaymentAllocation.payment_id)
                        .outerjoin(
                            BillingReceipt,
                            (BillingReceipt.payment_id == BillingPayment.id)
                            & (BillingReceipt.status != BillingReceiptStatus.cancelled),
                        )
                        .filter(BillingPaymentAllocation.charge_id.in_(c_ids))
                        .all()
                    )
                    pay_by_charge: dict[UUID, tuple[BillingPayment, BillingReceipt | None]] = {}
                    for alloc, pay, rcp in alloc_rows:
                        if alloc.charge_id not in pay_by_charge:
                            pay_by_charge[alloc.charge_id] = (pay, rcp)

                    for a_id, chg in c_by_appt.items():
                        pay_info = pay_by_charge.get(chg.id)
                        p_obj = pay_info[0] if pay_info else None
                        r_obj = pay_info[1] if pay_info else None
                        p_method = None
                        if p_obj and getattr(p_obj, "payment_method", None):
                            p_method = p_obj.payment_method.value if hasattr(p_obj.payment_method, "value") else str(p_obj.payment_method)
                        settlement_map[a_id] = {
                            "charge_id": chg.id,
                            "payment_method": p_method,
                            "payment_amount": float(p_obj.amount) if p_obj else None,
                            "payment_reference": p_obj.reference_number if p_obj else None,
                            "receipt_number": r_obj.receipt_number if r_obj else None,
                            "receipt_id": r_obj.id if r_obj else None,
                            "remaining_balance": max(0.0, round(float(chg.net_amount - chg.amount_paid), 2)),
                        }
            except Exception:
                settlement_map = {}

        # Bulk derive decoupled workflow states (consultation vs investigation vs overall)
        workflow_map: dict[UUID, Any] = {}
        workflow_error = False
        try:
            from modules.appointments.services.appointment_workflow_service import (
                AppointmentWorkflowService,
            )
            wf_svc = AppointmentWorkflowService(self.db, self.hospital_id)
            workflow_map = wf_svc.bulk_derive_workflows(appointments)
        except Exception:
            workflow_map = {}
            workflow_error = True

        items: list[AppointmentListItem] = []
        for a in appointments:
            pat = patients_map.get(a.patient_id)
            doc_name = users_map.get(a.doctor_id)
            nurse_name = users_map.get(a.nurse_id) if a.nurse_id else None
            adm_info = admissions_map.get(a.admission_id) if a.admission_id else None
            ip_id = adm_info.get("ip_id") if adm_info else None
            adm_ward = adm_info.get("ward") if adm_info else None
            adm_bed = adm_info.get("bed") if adm_info else None
            adm_status = adm_info.get("status") if adm_info else None

            if ledger_error or (patient_ids and a.patient_id not in ledger_map):
                out = None
                tot_chg = None
                tot_pd = None
                led_status = "unavailable"
            else:
                led = ledger_map.get(a.patient_id, {})
                out = float(led.get("outstanding", 0.0) or 0.0)
                tot_chg = float(led.get("total_charges", 0.0) or 0.0)
                tot_pd = float(led.get("total_paid", 0.0) or 0.0)
                if out <= 0.009:
                    led_status = "unbilled" if tot_chg == 0 else "cleared"
                else:
                    led_status = "outstanding"

            st_info = settlement_map.get(a.id, {})
            if workflow_error:
                c_status = "unavailable"
                inv_status = "unavailable"
                overall_status = "workflow_unavailable"
                wf_label = "Clinical Status Unavailable"
                diag_summary = None
            else:
                wf = workflow_map.get(a.id)
                c_status = (
                    wf.consultation_status
                    if wf
                    else (getattr(a, "consultation_status", None) or "scheduled")
                )
                inv_status = wf.investigation_status if wf else "none"
                overall_status = wf.overall_workflow_status if wf else a.status.value
                wf_label = wf.workflow_status_label if wf else a.status.value.title()
                diag_summary = wf.diagnostic_summary if wf else None

            items.append(
                AppointmentListItem(
                    id=a.id,
                    hospital_id=a.hospital_id,
                    doctor_id=a.doctor_id,
                    patient_id=a.patient_id,
                    appointment_date=a.appointment_date,
                    appointment_time=a.appointment_time,
                    purpose=a.purpose or "",
                    visit_type=a.visit_type or "OPD",
                    appointment_type_id=a.appointment_type_id,
                    wing_id=a.wing_id,
                    department_id=a.department_id,
                    consultation_fee=float(a.consultation_fee or 0.0),
                    followup_eligibility=a.followup_eligibility,
                    status=a.status,
                    booking_kind=a.booking_kind or "future",
                    notes=a.notes,
                    initial_findings=a.initial_findings,
                    queue_token=a.queue_token,
                    checked_in_at=a.checked_in_at,
                    created_at=a.created_at,
                    patient_name=pat.name if pat else None,
                    patient_uhid=pat.uhid if pat else None,
                    patient_mobile=pat.mobile if pat else None,
                    doctor_name=doc_name,
                    op_id=a.op_id,
                    admission_id=a.admission_id,
                    ip_id=ip_id,
                    admission_ward=adm_ward,
                    admission_bed=adm_bed,
                    admission_status=adm_status,
                    nurse_id=a.nurse_id,
                    nurse_name=nurse_name,
                    ledger_outstanding=out,
                    ledger_total_charges=tot_chg,
                    ledger_total_paid=tot_pd,
                    ledger_status=led_status,
                    charge_id=st_info.get("charge_id"),
                    payment_method=st_info.get("payment_method"),
                    payment_amount=st_info.get("payment_amount"),
                    payment_reference=st_info.get("payment_reference"),
                    receipt_number=st_info.get("receipt_number"),
                    receipt_id=st_info.get("receipt_id"),
                    remaining_balance=st_info.get("remaining_balance"),
                    patient_outstanding=out,
                    consultation_status=c_status,
                    investigation_status=inv_status,
                    diagnostic_review_status=getattr(a, "diagnostic_review_status", None),
                    overall_workflow_status=overall_status,
                    workflow_status_label=wf_label,
                    diagnostic_summary=diag_summary,
                    clinical_override_reason=getattr(a, "clinical_override_reason", None),
                    clinical_override_actor=getattr(a, "clinical_override_actor", None),
                    clinical_override_at=getattr(a, "clinical_override_at", None),
                    diagnostics_reviewed_by=getattr(a, "diagnostics_reviewed_by", None),
                    diagnostics_reviewed_at=getattr(a, "diagnostics_reviewed_at", None),
                    diagnostic_review_notes=getattr(a, "diagnostic_review_notes", None),
                )
            )
        return items
