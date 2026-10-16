"""
Database reader for consultation pricing, department/wing resolution, and follow-up rules.

Conforms to UltrionTech-Backend-Template modules/appointments/db/ specification.
"""

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Date, Float, Integer, String, column, or_, select, table
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Session

from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.appointment_type import AppointmentType
from modules.appointments.entities.enums import AppointmentStatus

_consultation_pricing = table(
    "consultation_pricing",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("doctor_id", PG_UUID(as_uuid=True)),
    column("appointment_type_id", PG_UUID(as_uuid=True)),
    column("wing_id", PG_UUID(as_uuid=True)),
    column("department_id", PG_UUID(as_uuid=True)),
    column("consultation_fee", Float),
    column("followup_free_days", Integer),
    column("is_active", Boolean),
)

_hospital_users = table(
    "hospital_users",
    column("id", PG_UUID(as_uuid=True)),
    column("hospital_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("custom_values", JSONB),
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
    column("wing_id", PG_UUID(as_uuid=True)),
    column("name", String),
    column("is_active", Boolean),
)


class PricingReader:
    """Encapsulates fee resolution and doctor wing/department assignment."""

    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def get_doctor_fallback_fee(self, doctor_id: UUID) -> float:
        """Extract consultation fee from doctor custom_values if defined."""
        stmt = select(_hospital_users.c.custom_values).where(
            _hospital_users.c.hospital_id == self.hospital_id,
            _hospital_users.c.id == doctor_id,
        )
        row = self.db.execute(stmt).first()
        if not row or not row[0]:
            return 0.0
        cv = row[0]
        for key in ("consultation_fee", "consultationFee", "fee", "consult_fee", "Consultation Fee"):
            val = cv.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return 0.0

    def find_pricing(
        self,
        doctor_id: UUID,
        appt_type_id: UUID | None,
        wing_id: UUID | None,
        department_id: UUID | None,
    ) -> dict[str, Any] | None:
        """Match consultation_pricing with most specific match taking precedence."""
        stmt = select(
            _consultation_pricing.c.id,
            _consultation_pricing.c.consultation_fee,
            _consultation_pricing.c.followup_free_days,
            _consultation_pricing.c.wing_id,
            _consultation_pricing.c.department_id,
            _consultation_pricing.c.appointment_type_id,
        ).where(
            _consultation_pricing.c.hospital_id == self.hospital_id,
            _consultation_pricing.c.doctor_id == doctor_id,
            _consultation_pricing.c.is_active.is_(True),
        )
        rows = self.db.execute(stmt).mappings().all()
        if not rows:
            return None

        # When specific parameters are requested, candidate rows must not contradict them
        filtered = []
        for r in rows:
            if appt_type_id and r["appointment_type_id"] not in (appt_type_id, None):
                continue
            if wing_id and r["wing_id"] not in (wing_id, None):
                continue
            if department_id and r["department_id"] not in (department_id, None):
                continue
            filtered.append(r)

        if not filtered:
            return None

        def _score(r):
            score = 0
            if appt_type_id and r["appointment_type_id"] == appt_type_id:
                score += 8
            elif r["appointment_type_id"] is None:
                score += 1
            if department_id and r["department_id"] == department_id:
                score += 4
            if wing_id and r["wing_id"] == wing_id:
                score += 2
            return score

        sorted_rows = sorted(filtered, key=_score, reverse=True)
        return dict(sorted_rows[0]) if sorted_rows else None

    def resolve_wing_department_for_doctor(
        self,
        doctor_id: UUID,
        wing_id: UUID | None = None,
        department_id: UUID | None = None,
        appointment_type_id: UUID | None = None,
    ) -> tuple[UUID | None, UUID | None]:
        """Resolve wing and department IDs from explicit inputs, pricing, or active fallback."""
        if wing_id and department_id:
            return wing_id, department_id

        # 1. Try pricing match constraining to supplied dimensions
        stmt = select(
            _consultation_pricing.c.wing_id,
            _consultation_pricing.c.department_id,
        ).where(
            _consultation_pricing.c.hospital_id == self.hospital_id,
            _consultation_pricing.c.doctor_id == doctor_id,
            _consultation_pricing.c.is_active.is_(True),
        )
        if appointment_type_id:
            stmt = stmt.where(
                or_(
                    _consultation_pricing.c.appointment_type_id == appointment_type_id,
                    _consultation_pricing.c.appointment_type_id.is_(None),
                )
            )
        if wing_id:
            stmt = stmt.where(
                or_(_consultation_pricing.c.wing_id == wing_id, _consultation_pricing.c.wing_id.is_(None))
            )
        if department_id:
            stmt = stmt.where(
                or_(_consultation_pricing.c.department_id == department_id, _consultation_pricing.c.department_id.is_(None))
            )

        pricing_rows = self.db.execute(stmt).all()
        # Verify Wing<->Department compatibility for pricing matches
        for r_wing_id, r_dept_id in pricing_rows:
            candidate_wing = wing_id or r_wing_id
            candidate_dept = department_id or r_dept_id
            if candidate_wing and candidate_dept:
                dept_check = self.db.execute(
                    select(_departments.c.wing_id).where(
                        _departments.c.id == candidate_dept,
                        _departments.c.hospital_id == self.hospital_id,
                        _departments.c.is_active.is_(True),
                    )
                ).first()
                if dept_check and (dept_check[0] is None or dept_check[0] == candidate_wing):
                    return candidate_wing, candidate_dept
            elif candidate_wing and not candidate_dept:
                wing_id = candidate_wing
                break
            elif candidate_dept and not candidate_wing:
                department_id = candidate_dept
                break

        # 2. Fallback to active wing and department ensuring compatibility
        w = wing_id
        d = department_id
        if d and not w:
            dept_row = self.db.execute(
                select(_departments.c.wing_id).where(
                    _departments.c.id == d,
                    _departments.c.hospital_id == self.hospital_id,
                    _departments.c.is_active.is_(True),
                )
            ).first()
            if dept_row and dept_row[0]:
                w = dept_row[0]

        if not w:
            wing_stmt = (
                select(_wings.c.id)
                .where(
                    _wings.c.hospital_id == self.hospital_id,
                    _wings.c.is_active.is_(True),
                )
                .order_by(_wings.c.name.asc())
                .limit(1)
            )
            w_row = self.db.execute(wing_stmt).first()
            if w_row:
                w = w_row[0]

        if not d:
            dept_stmt = select(_departments.c.id, _departments.c.wing_id).where(
                _departments.c.hospital_id == self.hospital_id,
                _departments.c.is_active.is_(True),
            )
            if w:
                dept_stmt = dept_stmt.where(
                    or_(_departments.c.wing_id == w, _departments.c.wing_id.is_(None))
                )
            dept_stmt = dept_stmt.order_by(_departments.c.name.asc()).limit(1)
            d_row = self.db.execute(dept_stmt).first()
            if d_row:
                d = d_row[0]
                if not w and d_row[1]:
                    w = d_row[1]

        return w, d

    def get_last_completed_visit_date(
        self,
        patient_id: UUID,
        doctor_id: UUID,
    ) -> date | None:
        """Fetch the date of the most recent completed visit between patient and doctor."""
        row = (
            self.db.query(Appointment.appointment_date)
            .filter(
                Appointment.hospital_id == self.hospital_id,
                Appointment.patient_id == patient_id,
                Appointment.doctor_id == doctor_id,
                Appointment.status == AppointmentStatus.completed,
            )
            .order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
            .first()
        )
        return row[0] if row else None
