"""
Actions for Doctor Leaves and holiday schedule management.

Conforms to UltrionTech-Backend-Template modules/doctors/actions/ specification.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from hms_migration.modules.doctors.contracts.doctor_contracts import (
    DoctorLeaveCreate,
    DoctorLeaveRangeCreate,
    DoctorLeaveResponse,
)
from hms_migration.modules.doctors.db.doctor_leaves_repository import DoctorLeavesRepository
from hms_migration.modules.doctors.db.doctors_repository import DoctorsRepository
from hms_migration.modules.doctors.entities.doctor import DoctorLeave
from hms_migration.modules.doctors.services.doctor_schedule_service import (
    check_leave_conflicts,
    normalize_leave_type,
    resolve_leave_times,
    slot_duration_minutes,
)
from hms_migration.shared.audit.service import write_audit_log


def to_leave_response(leave: DoctorLeave) -> DoctorLeaveResponse:
    return DoctorLeaveResponse(
        id=leave.id,
        doctor_id=leave.doctor_id,
        leave_date=leave.leave_date,
        start_time=leave.start_time,
        end_time=leave.end_time,
        leave_type=leave.leave_type or "Other",
        reason=leave.reason,
        created_at=leave.created_at,
    )


class ListDoctorLeavesAction:
    def __init__(self, db: Session) -> None:
        self.repo = DoctorLeavesRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[DoctorLeaveResponse]:
        leaves = self.repo.list_leaves(hospital_id, doctor_id, start_date, end_date)
        return [to_leave_response(l) for l in leaves]


class CreateDoctorLeaveAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.doctors_repo = DoctorsRepository(db)
        self.leaves_repo = DoctorLeavesRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        payload: DoctorLeaveCreate,
        actor: dict[str, Any],
    ) -> DoctorLeaveResponse:
        doctor = self.doctors_repo.get_doctor(hospital_id, doctor_id)
        if not doctor:
            raise HTTPException(status_code=404, detail="Doctor not found")

        start_time, end_time = resolve_leave_times(
            doctor,
            full_day=payload.full_day,
            start_time=payload.start_time,
            end_time=payload.end_time,
        )
        slot_mins = slot_duration_minutes(self.db, hospital_id)
        check_leave_conflicts(
            self.db,
            hospital_id,
            doctor_id,
            [payload.leave_date],
            start_time,
            end_time,
            slot_mins,
        )

        leave = self.leaves_repo.create_leave(
            hospital_id=hospital_id,
            doctor_id=doctor_id,
            leave_date=payload.leave_date,
            start_time=start_time,
            end_time=end_time,
            leave_type=normalize_leave_type(payload.leave_type),
            reason=payload.reason,
        )
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="doctor_leave",
            entity_id=str(leave.id),
            summary=f"Created leave on {leave.leave_date} for doctor {doctor.name}",
        )
        self.db.commit()
        return to_leave_response(leave)


class CreateDoctorLeaveRangeAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.doctors_repo = DoctorsRepository(db)
        self.leaves_repo = DoctorLeavesRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        payload: DoctorLeaveRangeCreate,
        actor: dict[str, Any],
    ) -> list[DoctorLeaveResponse]:
        doctor = self.doctors_repo.get_doctor(hospital_id, doctor_id)
        if not doctor:
            raise HTTPException(status_code=404, detail="Doctor not found")
        if payload.start_date > payload.end_date:
            raise HTTPException(status_code=400, detail="start_date must be on or before end_date")
        if (payload.end_date - payload.start_date).days > 90:
            raise HTTPException(status_code=400, detail="Leave range cannot exceed 90 days")

        start_time, end_time = resolve_leave_times(
            doctor,
            full_day=payload.full_day,
            start_time=payload.start_time,
            end_time=payload.end_time,
        )

        days: list[date] = []
        cur = payload.start_date
        while cur <= payload.end_date:
            days.append(cur)
            cur += timedelta(days=1)

        slot_mins = slot_duration_minutes(self.db, hospital_id)
        check_leave_conflicts(
            self.db,
            hospital_id,
            doctor_id,
            days,
            start_time,
            end_time,
            slot_mins,
        )

        ltype = normalize_leave_type(payload.leave_type)
        created_leaves: list[DoctorLeave] = []
        for d in days:
            leave = self.leaves_repo.create_leave(
                hospital_id=hospital_id,
                doctor_id=doctor_id,
                leave_date=d,
                start_time=start_time,
                end_time=end_time,
                leave_type=ltype,
                reason=payload.reason,
            )
            created_leaves.append(leave)

        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="create",
            entity_type="doctor_leave",
            entity_id=f"range_{payload.start_date}_{payload.end_date}",
            summary=f"Created {len(created_leaves)} leave day(s) for doctor {doctor.name}",
        )
        self.db.commit()
        return [to_leave_response(l) for l in created_leaves]


class DeleteDoctorLeaveAction:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.leaves_repo = DoctorLeavesRepository(db)

    def execute(
        self,
        hospital_id: UUID,
        doctor_id: UUID,
        leave_id: UUID,
        actor: dict[str, Any],
    ) -> None:
        ok = self.leaves_repo.delete_leave(hospital_id, doctor_id, leave_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Leave not found")
        write_audit_log(
            self.db,
            hospital_id=hospital_id,
            actor=actor,
            action="delete",
            entity_type="doctor_leave",
            entity_id=str(leave_id),
            summary=f"Deleted leave {leave_id}",
        )
        self.db.commit()
