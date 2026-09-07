"""
Database repository for Doctor Leaves.

Conforms to UltrionTech-Backend-Template modules/doctors/db/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, time
from uuid import UUID

from sqlalchemy.orm import Session

from hms_migration.modules.doctors.entities.doctor import DoctorLeave


class DoctorLeavesRepository:
    """Repository handling doctor leave schedule records."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_leaves(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[DoctorLeave]:
        q = self.db.query(DoctorLeave).filter(
            DoctorLeave.hospital_id == hospital_id,
            DoctorLeave.doctor_id == doctor_id,
        )
        if start_date:
            q = q.filter(DoctorLeave.leave_date >= start_date)
        if end_date:
            q = q.filter(DoctorLeave.leave_date <= end_date)
        return q.order_by(DoctorLeave.leave_date.asc(), DoctorLeave.start_time.asc()).all()

    def create_leave(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        leave_date: date,
        start_time: time,
        end_time: time,
        leave_type: str | None = None,
        reason: str | None = None,
    ) -> DoctorLeave:
        leave = DoctorLeave(
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            leave_date=leave_date,
            start_time=start_time,
            end_time=end_time,
            leave_type=leave_type,
            reason=reason.strip() if reason else None,
        )
        self.db.add(leave)
        self.db.flush()
        return leave

    def get_leave_by_id(
        self, hospital_id: UUID, doctor_id: UUID, leave_id: UUID
    ) -> DoctorLeave | None:
        return (
            self.db.query(DoctorLeave)
            .filter(
                DoctorLeave.id == leave_id,
                DoctorLeave.doctor_id == doctor_id,
                DoctorLeave.hospital_id == hospital_id,
            )
            .first()
        )

    def delete_leave(self, hospital_id: UUID, doctor_id: UUID, leave_id: UUID) -> bool:
        leave = self.get_leave_by_id(hospital_id, doctor_id, leave_id)
        if not leave:
            return False
        self.db.delete(leave)
        return True
