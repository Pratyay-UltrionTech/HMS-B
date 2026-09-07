"""
Business use-case actions for Operation Theatre (OT) domain.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hms_migration.modules.billing.entities.billing_entities import BillingSourceType
from hms_migration.modules.billing.services.billing_service import (
    cancel_charge_for_source,
    ensure_charge,
)
from hms_migration.modules.ot.contracts.ot_contracts import (
    OtCalendarEntry,
    OtCompleteRequest,
    OtDashboardResponse,
    OtNotesRequest,
    OtRescheduleRequest,
    OtSurgeryCreate,
    OtSurgeryResponse,
    OtSurgeryUpdate,
)
from hms_migration.modules.ot.db.ot_repository import OtRepository
from hms_migration.modules.ot.entities.ot_entities import (
    OtRoom,
    OtSurgery,
    OtSurgeryStatus,
)
from hms_migration.modules.ot.services.ot_service import (
    room_label,
    surgery_end,
    surgery_to_response,
    sync_ot_surgery_medical_record,
    sync_time_based_status,
)
from hms_migration.shared.audit import write_audit_log


def _actor_name(user: dict[str, Any]) -> str:
    return str(user.get("name") or user.get("sub") or "Staff")


def _actor_role(user: dict[str, Any]) -> str:
    return str(user.get("staff_role_name") or user.get("role") or "")


class ListCalendarAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = OtRepository(db, hospital_id)

    def execute(
        self, date_from: date, date_to: date, ot_room_id: UUID | None = None
    ) -> list[OtCalendarEntry]:
        if date_to < date_from:
            raise HTTPException(status_code=400, detail="date_to must be on or after date_from")

        start = datetime.combine(date_from, time.min).replace(tzinfo=timezone.utc)
        end = datetime.combine(date_to, time.max).replace(tzinfo=timezone.utc)

        rows = self.repo.list_calendar_surgeries(end=end, ot_room_id=ot_room_id)
        out: list[OtCalendarEntry] = []
        for item in rows:
            ends = surgery_end(item)
            if ends < start:
                continue
            room = item.ot_room_ref
            if not item.ot_room_id and not room:
                continue
            label = room_label(room) if room else (item.ot_room or "OT")
            out.append(
                OtCalendarEntry(
                    id=item.id,
                    ot_room_id=item.ot_room_id or room.id,
                    ot_room_label=label,
                    scheduled_at=item.scheduled_at,
                    ends_at=ends,
                    surgery_type=item.surgery_type,
                    status=item.status,
                    patient_name=item.patient.name if item.patient else None,
                    surgeon_name=item.surgeon.name if item.surgeon else None,
                )
            )
        return out


class GetDashboardAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = OtRepository(db, hospital_id)

    def execute(self) -> OtDashboardResponse:
        counts = self.repo.get_dashboard_metrics()
        return OtDashboardResponse(
            todays_surgeries=counts["todays_surgeries"],
            completed=counts["completed"],
            ongoing=counts["ongoing"],
            scheduled=counts["scheduled"],
            cancelled=counts["cancelled"],
        )


class ListSurgeriesAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.repo = OtRepository(db, hospital_id)

    def execute(
        self,
        status_filter: str | None = None,
        patient_id: UUID | None = None,
        search: str | None = None,
        schedule_only: bool | None = None,
        ongoing_only: bool | None = None,
        history_only: bool | None = None,
        notes_pending: bool | None = None,
    ) -> list[OtSurgeryResponse]:
        status_enum = None
        if status_filter:
            try:
                status_enum = OtSurgeryStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")

        rows = self.repo.list_surgeries(
            status_filter=status_enum,
            patient_id=patient_id,
            search=search,
            schedule_only=schedule_only,
            ongoing_only=ongoing_only,
            history_only=history_only,
            notes_pending=notes_pending,
        )

        now = datetime.now(timezone.utc)
        for r in rows:
            sync_time_based_status(r, now)
        self.db.commit()

        return [surgery_to_response(r) for r in rows]


class GetSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        return surgery_to_response(item)


class CreateSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, payload: OtSurgeryCreate) -> OtSurgeryResponse:
        patient = self.repo.get_patient(payload.patient_id)
        if not patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

        if not self.repo.check_active_admission(payload.patient_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transfer the patient to IPD and admit them before booking OT",
            )

        if not self.repo.check_prescription_exists(payload.patient_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Write a prescription for this patient before booking OT",
            )

        if payload.surgeon_id:
            surgeon = self.repo.get_surgeon(payload.surgeon_id)
            if not surgeon:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgeon not found")

        dept = self.repo.get_department(payload.department_id)
        if not dept:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

        ot_room = self.repo.resolve_ot_room(payload.ot_room_id, payload.department_id)
        if not ot_room:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selected OT room is invalid or does not belong to the selected department",
            )

        start = payload.scheduled_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = start + timedelta(minutes=int(payload.duration_minutes or 60))

        if self.repo.check_ot_room_conflict(ot_room.id, start, end):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="OT room is already booked for this time range",
            )

        surgery_no = self.repo.next_surgery_no()
        actor_name = _actor_name(self.user)
        actor_role = _actor_role(self.user)

        item = OtSurgery(
            hospital_id=self.hospital_id,
            surgery_no=surgery_no,
            patient_id=payload.patient_id,
            surgeon_id=payload.surgeon_id,
            assistant_surgeon=payload.assistant_surgeon.strip() if payload.assistant_surgeon else None,
            surgery_type=payload.surgery_type.strip(),
            surgery_category=payload.surgery_category.strip() or "General",
            priority=payload.priority,
            department_id=payload.department_id,
            ot_room_id=ot_room.id,
            ot_room=room_label(ot_room),
            ot_charge_amount=float(getattr(ot_room, "base_ot_charge", 0) or 0),
            scheduled_at=payload.scheduled_at,
            duration_minutes=payload.duration_minutes,
            anaesthetist=payload.anaesthetist.strip() if payload.anaesthetist else None,
            remarks=payload.remarks.strip() if payload.remarks else None,
            booked_by_name=actor_name,
            booked_by_role=actor_role,
            status=OtSurgeryStatus.scheduled,
        )
        self.db.add(item)
        self.db.flush()

        # Target-native billing integration
        ensure_charge(
            self.db,
            hospital_id=self.hospital_id,
            patient_id=patient.id,
            source_type=BillingSourceType.ot,
            source_id=item.id,
            description=f"OT Charge — {item.surgery_no} · {room_label(ot_room)}"[:512],
            charge_amount=float(item.ot_charge_amount or 0),
            created_by_name=actor_name,
        )

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="create",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Booked surgery {item.surgery_no} — {item.surgery_type} for {patient.name}",
        )
        self.db.commit()

        return surgery_to_response(self.repo.get_surgery(item.id))


class UpdateSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID, payload: OtSurgeryUpdate) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot edit completed/cancelled surgery")

        data = payload.model_dump(exclude_unset=True)
        if "surgeon_id" in data and data["surgeon_id"]:
            surgeon = self.repo.get_surgeon(data["surgeon_id"])
            if not surgeon:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgeon not found")

        for key, value in data.items():
            if isinstance(value, str):
                value = value.strip() or None
            setattr(item, key, value)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Updated surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class ConfirmSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status != OtSurgeryStatus.scheduled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only scheduled surgeries can be confirmed",
            )

        item.status = OtSurgeryStatus.confirmed
        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Confirmed surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class RescheduleSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID, payload: OtRescheduleRequest) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot reschedule this surgery")

        item.scheduled_at = payload.scheduled_at
        if payload.ot_room_id:
            dept_id = payload.department_id or item.department_id
            if payload.department_id:
                dept = self.repo.get_department(payload.department_id)
                if not dept:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
                item.department_id = payload.department_id
            ot_room = self.repo.resolve_ot_room(payload.ot_room_id, dept_id)
            if not ot_room:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OT room not found")
            item.ot_room_id = ot_room.id
            item.ot_room = room_label(ot_room)
            if not item.department_id:
                item.department_id = ot_room.department_id
        elif payload.ot_room:
            item.ot_room = payload.ot_room.strip()

        if payload.duration_minutes:
            item.duration_minutes = payload.duration_minutes
        if payload.remarks is not None:
            item.remarks = payload.remarks.strip() or None

        if item.status == OtSurgeryStatus.confirmed:
            item.status = OtSurgeryStatus.scheduled

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Rescheduled surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class StartSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status not in (OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Surgery cannot be started from current status",
            )

        item.status = OtSurgeryStatus.in_progress
        item.started_at = datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Started surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class CompleteSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID, payload: OtCompleteRequest | None = None) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")

        now = datetime.now(timezone.utc)
        sync_time_based_status(item, now)
        if item.status in (OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed):
            start = item.scheduled_at
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if now >= start:
                item.status = OtSurgeryStatus.in_progress
                item.started_at = item.started_at or start

        if item.status != OtSurgeryStatus.in_progress:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Surgery is not ongoing yet")

        item.status = OtSurgeryStatus.completed
        item.completed_at = now
        if payload:
            if payload.shifted_to:
                item.shifted_to = payload.shifted_to.strip()
            if payload.actual_duration_minutes:
                item.actual_duration_minutes = payload.actual_duration_minutes
            elif item.started_at:
                item.actual_duration_minutes = max(1, int((now - item.started_at).total_seconds() // 60))
        elif item.started_at:
            item.actual_duration_minutes = max(1, int((now - item.started_at).total_seconds() // 60))

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Completed surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class CancelSurgeryAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Surgery cannot be cancelled")

        item.status = OtSurgeryStatus.cancelled
        cancel_charge_for_source(self.db, self.hospital_id, BillingSourceType.ot, item.id)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_surgery",
            entity_id=item.id,
            summary=f"Cancelled surgery {item.surgery_no}",
        )
        self.db.commit()
        return surgery_to_response(self.repo.get_surgery(surgery_id))


class SaveNotesAction:
    def __init__(self, db: Session, hospital_id: UUID, user: dict[str, Any]) -> None:
        self.db = db
        self.hospital_id = hospital_id
        self.user = user
        self.repo = OtRepository(db, hospital_id)

    def execute(self, surgery_id: UUID, payload: OtNotesRequest) -> OtSurgeryResponse:
        item = self.repo.get_surgery(surgery_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surgery not found")
        if item.status == OtSurgeryStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot add notes to a cancelled surgery")

        sync_time_based_status(item)
        item.pre_op_diagnosis = payload.pre_op_diagnosis.strip()
        item.procedure_performed = payload.procedure_performed.strip()
        item.findings = payload.findings.strip() if payload.findings else None
        item.implants_used = payload.implants_used.strip() if payload.implants_used else None
        item.complications = payload.complications.strip() if payload.complications else None
        item.post_op_instructions = payload.post_op_instructions.strip() if payload.post_op_instructions else None
        item.follow_up_notes = payload.follow_up_notes.strip() if payload.follow_up_notes else None
        if payload.shifted_to:
            item.shifted_to = payload.shifted_to.strip()
        item.notes_recorded_by = _actor_name(self.user)
        item.notes_recorded_at = datetime.now(timezone.utc)
        if payload.ot_report_file_data:
            item.ot_report_file_name = payload.ot_report_file_name
            item.ot_report_file_data = payload.ot_report_file_data
        if payload.consent_file_data:
            item.consent_file_name = payload.consent_file_name
            item.consent_file_data = payload.consent_file_data
        if payload.image_file_data:
            item.image_file_name = payload.image_file_name
            item.image_file_data = payload.image_file_data

        if item.status == OtSurgeryStatus.in_progress:
            item.status = OtSurgeryStatus.completed
            item.completed_at = item.completed_at or datetime.now(timezone.utc)

        write_audit_log(
            self.db,
            hospital_id=self.hospital_id,
            actor=self.user,
            action="update",
            entity_type="ot_notes",
            entity_id=item.id,
            summary=f"Saved operation notes for {item.surgery_no}",
        )
        sync_ot_surgery_medical_record(self.db, item)
        self.db.commit()

        return surgery_to_response(self.repo.get_surgery(surgery_id))
