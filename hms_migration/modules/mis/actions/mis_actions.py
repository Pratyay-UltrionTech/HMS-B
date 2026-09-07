"""
MIS domain reporting action handlers.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.appointments.entities.appointment import (
    Appointment,
    AppointmentStatus,
)
from hms_migration.modules.beds.entities.bed import Bed
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.entities.admission import (
    Admission,
    AdmissionStatus,
)
from hms_migration.modules.mis.contracts.mis_contracts import (
    AppointmentReportResponse,
    BedReportResponse,
    DailySummaryResponse,
    DoctorPerfRow,
    DoctorReportResponse,
    FilterItem,
    MetricRow,
    NamedCountRow,
    PatientReportResponse,
    WardOccupancyRow,
)
from hms_migration.modules.mis.services.mis_service import (
    bulk_doctor_consultation_billing_revenue,
    consultation_fee,
    day_end,
    day_start,
    filters_dict,
    is_doctor,
    sum_billing_charges,
    ward_ids_for_department,
)
from hms_migration.modules.masters.entities.organization_entities import Department
from hms_migration.modules.patients.entities.patient import Patient, PatientStatus


class MisActions:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user

    # ── Filter Options ────────────────────────────────────────────────────────

    def get_filter_doctors(self) -> list[FilterItem]:
        users = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role))
            .filter(HospitalUser.hospital_id == self.hospital_id, HospitalUser.is_active.is_(True))
            .all()
        )
        return [FilterItem(id=str(d.id), name=d.name) for d in users if is_doctor(d)]

    def get_filter_departments(self) -> list[FilterItem]:
        deps = (
            self.db.query(Department)
            .filter(Department.hospital_id == self.hospital_id, Department.is_active.is_(True))
            .order_by(Department.name.asc())
            .all()
        )
        return [FilterItem(id=str(d.id), name=d.name) for d in deps]

    # ── Patient Reports ───────────────────────────────────────────────────────

    def get_patient_reports(
        self,
        date_from: date | None,
        date_to: date | None,
        department_id: UUID | None,
        doctor_id: UUID | None,
        patient_id: UUID | None,
        status_filter: str | None,
    ) -> PatientReportResponse:
        today = date.today()
        df = date_from or today.replace(day=1)
        dt = date_to or today

        q = self.db.query(Patient).filter(Patient.hospital_id == self.hospital_id)
        if patient_id:
            q = q.filter(Patient.id == patient_id)
        if status_filter:
            try:
                q = q.filter(Patient.status == PatientStatus(status_filter))
            except ValueError:
                pass

        total = q.count()

        new_today_q = self.db.query(func.count(Patient.id)).filter(
            Patient.hospital_id == self.hospital_id,
            Patient.created_at >= day_start(today),
            Patient.created_at <= day_end(today),
        )
        if patient_id:
            new_today_q = new_today_q.filter(Patient.id == patient_id)
        new_today = new_today_q.scalar() or 0

        new_in_range_q = self.db.query(func.count(Patient.id)).filter(
            Patient.hospital_id == self.hospital_id,
            Patient.created_at >= day_start(df),
            Patient.created_at <= day_end(dt),
        )
        if patient_id:
            new_in_range_q = new_in_range_q.filter(Patient.id == patient_id)
        new_in_range = new_in_range_q.scalar() or 0

        admitted = (
            self.db.query(func.count(Patient.id))
            .filter(Patient.hospital_id == self.hospital_id, Patient.status == PatientStatus.admitted)
            .scalar()
            or 0
        )

        opd_q = (
            self.db.query(func.count(func.distinct(Appointment.patient_id)))
            .filter(
                Appointment.hospital_id == self.hospital_id,
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

        ipd_q = self.db.query(func.count(Admission.id)).filter(
            Admission.hospital_id == self.hospital_id,
            Admission.status == AdmissionStatus.admitted,
        )
        if doctor_id:
            ipd_q = ipd_q.filter(Admission.doctor_id == doctor_id)
        if patient_id:
            ipd_q = ipd_q.filter(Admission.patient_id == patient_id)
        ward_ids = ward_ids_for_department(self.db, self.hospital_id, department_id)
        if ward_ids is not None:
            ipd_q = ipd_q.filter(Admission.ward_id.in_(ward_ids) if ward_ids else Admission.ward_id.is_(None))
        ipd = ipd_q.scalar() or 0

        discharged_today = (
            self.db.query(func.count(Admission.id))
            .filter(
                Admission.hospital_id == self.hospital_id,
                Admission.status == AdmissionStatus.discharged,
                Admission.discharged_at >= day_start(today),
                Admission.discharged_at <= day_end(today),
            )
            .scalar()
            or 0
        )

        discharged_range = (
            self.db.query(func.count(Admission.id))
            .filter(
                Admission.hospital_id == self.hospital_id,
                Admission.status == AdmissionStatus.discharged,
                Admission.discharged_at >= day_start(df),
                Admission.discharged_at <= day_end(dt),
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
            filters=filters_dict(df, dt, department_id, doctor_id, patient_id, status_filter),
        )

    # ── Appointment Reports ───────────────────────────────────────────────────

    def get_appointment_reports(
        self,
        date_from: date | None,
        date_to: date | None,
        department_id: UUID | None,
        doctor_id: UUID | None,
        patient_id: UUID | None,
        status_filter: str | None,
    ) -> AppointmentReportResponse:
        today = date.today()
        df = date_from or today
        dt = date_to or today

        base = self.db.query(Appointment).filter(
            Appointment.hospital_id == self.hospital_id,
            Appointment.appointment_date >= df,
            Appointment.appointment_date <= dt,
        )
        if doctor_id:
            base = base.filter(Appointment.doctor_id == doctor_id)
        if patient_id:
            base = base.filter(Appointment.patient_id == patient_id)
        if status_filter:
            try:
                base = base.filter(Appointment.status == AppointmentStatus(status_filter))
            except ValueError:
                pass

        all_rows = base.all()
        doc_ids = {a.doctor_id for a in all_rows if a.doctor_id}
        doc_names: dict[UUID, str] = {}
        if doc_ids:
            docs = self.db.query(HospitalUser).filter(HospitalUser.id.in_(doc_ids)).all()
            doc_names = {d.id: d.name for d in docs}

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
                by_doc[did] = NamedCountRow(name=doc_names.get(did, "Unknown"), count=0)
            by_doc[did].count += 1

        by_doctor = sorted(by_doc.values(), key=lambda x: x.count, reverse=True)

        today_q = self.db.query(func.count(Appointment.id)).filter(
            Appointment.hospital_id == self.hospital_id,
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
            MetricRow(metric="In Progress / Checked In", count=waiting),
            MetricRow(metric="Scheduled", count=scheduled),
        ]

        return AppointmentReportResponse(
            metrics=metrics,
            by_doctor=by_doctor,
            generated_at=datetime.now(timezone.utc).isoformat(),
            filters=filters_dict(df, dt, department_id, doctor_id, patient_id, status_filter),
        )

    # ── Bed Reports ───────────────────────────────────────────────────────────

    def get_bed_reports(
        self,
        date_from: date | None,
        date_to: date | None,
        department_id: UUID | None,
        doctor_id: UUID | None,
        patient_id: UUID | None,
        status_filter: str | None,
    ) -> BedReportResponse:
        ward_ids = ward_ids_for_department(self.db, self.hospital_id, department_id)

        bq = self.db.query(Bed).filter(Bed.hospital_id == self.hospital_id, Bed.is_active.is_(True))
        if ward_ids is not None:
            bq = bq.filter(Bed.ward_id.in_(ward_ids)) if ward_ids else bq.filter(False)
        if status_filter == "available":
            bq = bq.filter(Bed.is_occupied.is_(False))
        elif status_filter == "occupied":
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
            filters=filters_dict(date_from, date_to, department_id, doctor_id, patient_id, status_filter),
        )

    # ── Doctor Reports ────────────────────────────────────────────────────────

    def get_doctor_reports(
        self,
        date_from: date | None,
        date_to: date | None,
        department_id: UUID | None,
        doctor_id: UUID | None,
        patient_id: UUID | None,
        status_filter: str | None,
    ) -> DoctorReportResponse:
        today = date.today()
        df = date_from or today.replace(day=1)
        dt = date_to or today

        doctors = (
            self.db.query(HospitalUser)
            .options(joinedload(HospitalUser.role))
            .filter(HospitalUser.hospital_id == self.hospital_id, HospitalUser.is_active.is_(True))
            .all()
        )
        doctors = [d for d in doctors if is_doctor(d)]
        if doctor_id:
            doctors = [d for d in doctors if d.id == doctor_id]

        if not doctors:
            return DoctorReportResponse(
                doctors=[],
                metrics=[
                    MetricRow(metric="Doctors", count=0),
                    MetricRow(metric="Total Patients Seen", count=0),
                    MetricRow(metric="Appointments Completed", count=0),
                    MetricRow(metric="Revenue Generated (₹)", count=0),
                ],
                generated_at=datetime.now(timezone.utc).isoformat(),
                filters=filters_dict(df, dt, department_id, doctor_id, patient_id, status_filter),
            )

        doctor_ids = [d.id for d in doctors]

        aq = self.db.query(Appointment).filter(
            Appointment.hospital_id == self.hospital_id,
            Appointment.doctor_id.in_(doctor_ids),
            Appointment.appointment_date >= df,
            Appointment.appointment_date <= dt,
        )
        if patient_id:
            aq = aq.filter(Appointment.patient_id == patient_id)
        if status_filter:
            try:
                aq = aq.filter(Appointment.status == AppointmentStatus(status_filter))
            except ValueError:
                pass
        all_appts = aq.all()

        appts_by_doctor: dict[UUID, list[Appointment]] = {d.id: [] for d in doctors}
        for a in all_appts:
            appts_by_doctor.setdefault(a.doctor_id, []).append(a)

        billing_by_doctor = bulk_doctor_consultation_billing_revenue(self.db, self.hospital_id, doctor_ids, df, dt)

        rows: list[DoctorPerfRow] = []
        for d in doctors:
            appts = appts_by_doctor.get(d.id, [])
            completed = [a for a in appts if a.status == AppointmentStatus.completed]
            patients_seen = len(
                {a.patient_id for a in appts if a.status not in {AppointmentStatus.cancelled, AppointmentStatus.no_show}}
            )
            billing_rev = billing_by_doctor.get(d.id, 0.0)
            revenue = billing_rev if billing_rev > 0 else (consultation_fee(d) * len(completed))
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
            filters=filters_dict(df, dt, department_id, doctor_id, patient_id, status_filter),
        )

    # ── Daily Summary ─────────────────────────────────────────────────────────

    def get_daily_summary(
        self,
        on_date: date | None,
        department_id: UUID | None,
        doctor_id: UUID | None,
        patient_id: UUID | None,
        status_filter: str | None,
    ) -> DailySummaryResponse:
        day = on_date or date.today()

        new_patients = (
            self.db.query(func.count(Patient.id))
            .filter(
                Patient.hospital_id == self.hospital_id,
                Patient.created_at >= day_start(day),
                Patient.created_at <= day_end(day),
            )
            .scalar()
            or 0
        )

        appt_q = self.db.query(func.count(Appointment.id)).filter(
            Appointment.hospital_id == self.hospital_id,
            Appointment.appointment_date == day,
        )
        if doctor_id:
            appt_q = appt_q.filter(Appointment.doctor_id == doctor_id)
        if patient_id:
            appt_q = appt_q.filter(Appointment.patient_id == patient_id)
        appointments = appt_q.scalar() or 0

        adm_q = self.db.query(func.count(Admission.id)).filter(
            Admission.hospital_id == self.hospital_id,
            Admission.admitted_at >= day_start(day),
            Admission.admitted_at <= day_end(day),
        )
        if doctor_id:
            adm_q = adm_q.filter(Admission.doctor_id == doctor_id)
        admissions = adm_q.scalar() or 0

        dis_q = self.db.query(func.count(Admission.id)).filter(
            Admission.hospital_id == self.hospital_id,
            Admission.status == AdmissionStatus.discharged,
            Admission.discharged_at >= day_start(day),
            Admission.discharged_at <= day_end(day),
        )
        discharges = dis_q.scalar() or 0

        occupied = (
            self.db.query(func.count(Bed.id))
            .filter(Bed.hospital_id == self.hospital_id, Bed.is_active.is_(True), Bed.is_occupied.is_(True))
            .scalar()
            or 0
        )

        billing_rev = sum_billing_charges(self.db, self.hospital_id, date_from=day, date_to=day, doctor_id=doctor_id)
        if billing_rev > 0:
            revenue = billing_rev
        else:
            completed_q = (
                self.db.query(Appointment)
                .filter(
                    Appointment.hospital_id == self.hospital_id,
                    Appointment.appointment_date == day,
                    Appointment.status == AppointmentStatus.completed,
                )
            )
            if doctor_id:
                completed_q = completed_q.filter(Appointment.doctor_id == doctor_id)
            completed = completed_q.all()
            comp_doc_ids = {a.doctor_id for a in completed if a.doctor_id}
            comp_docs: dict[UUID, HospitalUser] = {}
            if comp_doc_ids:
                docs = self.db.query(HospitalUser).filter(HospitalUser.id.in_(comp_doc_ids)).all()
                comp_docs = {d.id: d for d in docs}
            revenue = 0.0
            for a in completed:
                doc = comp_docs.get(a.doctor_id)
                if doc:
                    revenue += consultation_fee(doc)
                elif a.consultation_fee:
                    revenue += a.consultation_fee

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
            filters=filters_dict(day, day, department_id, doctor_id, patient_id, status_filter),
        )
