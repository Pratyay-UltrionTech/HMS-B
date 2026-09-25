"""
Database repository for Operation Theatre (OT) domain.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, joinedload

from modules.clinical_records.entities.clinical_record import Prescription
from modules.doctors.entities.doctor import HospitalUser
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.masters.entities.organization_entities import Department, Wing
from modules.ot.entities.ot_entities import (
    OtRoom,
    OtSurgery,
    OtSurgeryStatus,
)
from modules.patients.entities.patient import Patient


class OtRepository:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def next_surgery_no(self) -> str:
        count = (
            self.db.query(func.count(OtSurgery.id))
            .filter(OtSurgery.hospital_id == self.hospital_id)
            .scalar()
            or 0
        )
        return f"OT{int(count) + 1:04d}"

    def get_surgery(self, surgery_id: UUID) -> OtSurgery | None:
        return (
            self.db.query(OtSurgery)
            .options(
                joinedload(OtSurgery.patient),
                joinedload(OtSurgery.surgeon),
                joinedload(OtSurgery.department),
                joinedload(OtSurgery.ot_room_ref),
            )
            .filter(
                OtSurgery.id == surgery_id,
                OtSurgery.hospital_id == self.hospital_id,
            )
            .first()
        )

    def list_surgeries(
        self,
        status_filter: OtSurgeryStatus | None = None,
        patient_id: UUID | None = None,
        ward_id: UUID | None = None,
        search: str | None = None,
        schedule_only: bool | None = None,
        ongoing_only: bool | None = None,
        history_only: bool | None = None,
        notes_pending: bool | None = None,
        limit: int = 300,
    ) -> list[OtSurgery]:
        q = (
            self.db.query(OtSurgery)
            .options(
                joinedload(OtSurgery.patient),
                joinedload(OtSurgery.surgeon),
                joinedload(OtSurgery.department),
                joinedload(OtSurgery.ot_room_ref),
            )
            .filter(OtSurgery.hospital_id == self.hospital_id)
        )
        if patient_id:
            q = q.filter(OtSurgery.patient_id == patient_id)
        if ward_id:
            from modules.inpatient.entities.admission import Admission, AdmissionStatus
            q = q.join(
                Admission,
                (Admission.patient_id == OtSurgery.patient_id)
                & (Admission.hospital_id == OtSurgery.hospital_id)
                & (Admission.ward_id == ward_id)
                & (Admission.status.in_([AdmissionStatus.admitted, AdmissionStatus.discharge_requested])),
            )
        if status_filter:
            q = q.filter(OtSurgery.status == status_filter)
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
        if search and search.strip():
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
        return q.order_by(OtSurgery.scheduled_at.desc()).limit(limit).all()

    def resolve_ot_room(
        self,
        ot_room_id: UUID,
        department_id: UUID | None = None,
        for_update: bool = False,
    ) -> OtRoom | None:
        q = self.db.query(OtRoom).filter(
            OtRoom.id == ot_room_id,
            OtRoom.hospital_id == self.hospital_id,
            OtRoom.is_active.is_(True),
        )
        if department_id:
            q = q.filter(OtRoom.department_id == department_id)
        if for_update:
            q = q.with_for_update()
        return q.first()

    def check_ot_room_conflict(
        self,
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
            self.db.query(OtSurgery)
            .filter(
                OtSurgery.hospital_id == self.hospital_id,
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

    def list_calendar_surgeries(
        self, end: datetime, start: datetime | None = None, ot_room_id: UUID | None = None
    ) -> list[OtSurgery]:
        """
        `start` bounds how far back the query looks. The caller (ExecuteCalendar
        in ot_actions.py) additionally keeps a surgery that started just before
        `start` but ends inside the visible window — that's a Python-side
        post-filter on `surgery_end(item) >= start`, unchanged by this method.
        To keep that post-filter correct, `start` here is widened by a fixed
        buffer (24h, comfortably larger than any real OT booking duration) so
        the SQL query can never exclude a row the post-filter would have kept.
        Previously this had no lower bound at all, scanning every surgery ever
        booked on every calendar render.
        """
        q = (
            self.db.query(OtSurgery)
            .options(
                joinedload(OtSurgery.patient),
                joinedload(OtSurgery.surgeon),
                joinedload(OtSurgery.ot_room_ref),
            )
            .filter(
                OtSurgery.hospital_id == self.hospital_id,
                OtSurgery.scheduled_at <= end,
                OtSurgery.status != OtSurgeryStatus.cancelled,
            )
        )
        if start is not None:
            q = q.filter(OtSurgery.scheduled_at >= start - timedelta(hours=24))
        if ot_room_id:
            q = q.filter(OtSurgery.ot_room_id == ot_room_id)
        return q.order_by(OtSurgery.scheduled_at.asc()).all()

    def get_dashboard_metrics(self) -> dict[str, int]:
        """
        Single grouped query instead of 5 separate COUNT(*) queries.

        All 5 original counts scanned the same ot_surgeries rows for this
        hospital_id, just with different filters — conditional aggregation
        (SUM of a per-bucket CASE) computes all 5 in one pass over the table
        instead of 5 round trips, using the same (hospital_id, scheduled_at)
        index for the "todays" bucket. Bucket definitions are unchanged from
        the original per-count filters (see git history for the prior form).
        """
        today = date.today()
        start = datetime.combine(today, time.min).replace(tzinfo=timezone.utc)
        end = datetime.combine(today, time.max).replace(tzinfo=timezone.utc)

        row = (
            self.db.query(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (OtSurgery.scheduled_at >= start) & (OtSurgery.scheduled_at <= end),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(case((OtSurgery.status == OtSurgeryStatus.completed, 1), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(case((OtSurgery.status == OtSurgeryStatus.in_progress, 1), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                OtSurgery.status.in_([OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed]),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(case((OtSurgery.status == OtSurgeryStatus.cancelled, 1), else_=0)), 0
                ),
            )
            .filter(OtSurgery.hospital_id == self.hospital_id)
            .one()
        )
        todays, completed, ongoing, scheduled, cancelled = row
        return {
            "todays_surgeries": int(todays),
            "completed": int(completed),
            "ongoing": int(ongoing),
            "scheduled": int(scheduled),
            "cancelled": int(cancelled),
        }

    def get_patient(self, patient_id: UUID) -> Patient | None:
        return (
            self.db.query(Patient)
            .filter(
                Patient.id == patient_id,
                Patient.hospital_id == self.hospital_id,
            )
            .first()
        )

    def check_active_admission(self, patient_id: UUID) -> bool:
        return (
            self.db.query(Admission.id)
            .filter(
                Admission.hospital_id == self.hospital_id,
                Admission.patient_id == patient_id,
                # Open-episode set: a requested admission already blocks a
                # second episode, so OT booking sees it as IPD (Invariant 1).
                Admission.status.in_(
                    [
                        AdmissionStatus.requested,
                        AdmissionStatus.admitted,
                        AdmissionStatus.discharge_requested,
                    ]
                ),
            )
            .first()
            is not None
        )

    def check_prescription_exists(self, patient_id: UUID) -> bool:
        return (
            self.db.query(Prescription.id)
            .filter(
                Prescription.hospital_id == self.hospital_id,
                Prescription.patient_id == patient_id,
            )
            .first()
            is not None
        )

    def get_surgeon(self, surgeon_id: UUID) -> HospitalUser | None:
        return (
            self.db.query(HospitalUser)
            .filter(
                HospitalUser.id == surgeon_id,
                HospitalUser.hospital_id == self.hospital_id,
            )
            .first()
        )

    def get_department(self, department_id: UUID) -> Department | None:
        return (
            self.db.query(Department)
            .filter(
                Department.id == department_id,
                Department.hospital_id == self.hospital_id,
            )
            .first()
        )

    # ── OT Rooms ──────────────────────────────────────────────────────────────

    def list_ot_rooms(self, active_only: bool = False) -> list[OtRoom]:
        q = (
            self.db.query(OtRoom)
            .options(joinedload(OtRoom.wing), joinedload(OtRoom.department))
            .filter(OtRoom.hospital_id == self.hospital_id)
        )
        if active_only:
            q = q.filter(OtRoom.is_active.is_(True))
        return q.order_by(OtRoom.code.asc()).all()

    def get_ot_room(self, ot_room_id: UUID) -> OtRoom | None:
        return (
            self.db.query(OtRoom)
            .options(joinedload(OtRoom.wing), joinedload(OtRoom.department))
            .filter(
                OtRoom.id == ot_room_id,
                OtRoom.hospital_id == self.hospital_id,
            )
            .first()
        )
