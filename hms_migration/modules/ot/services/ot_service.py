"""
Business logic and service operations for Operation Theatre (OT) domain.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from hms_migration.modules.clinical_records.entities.clinical_record import MedicalRecord
from hms_migration.modules.ot.contracts.ot_contracts import OtSurgeryResponse
from hms_migration.modules.ot.entities.ot_entities import (
    OtRoom,
    OtSurgery,
    OtSurgeryStatus,
)


def room_label(room: OtRoom) -> str:
    """Format human-readable label for an OT room."""
    return f"{room.code} — {room.name}"


def surgery_end(item: OtSurgery) -> datetime:
    """Calculate projected or actual end time of surgery."""
    start = item.scheduled_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start + timedelta(minutes=int(item.duration_minutes or 60))


def sync_time_based_status(item: OtSurgery, now: datetime | None = None) -> None:
    """Move scheduled surgeries into in_progress while inside the booked window."""
    if item.status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
        return
    now = now or datetime.now(timezone.utc)
    start = item.scheduled_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = surgery_end(item)
    if item.status in (OtSurgeryStatus.scheduled, OtSurgeryStatus.confirmed) and start <= now < end:
        item.status = OtSurgeryStatus.in_progress
        if not item.started_at:
            item.started_at = start


def surgery_to_response(item: OtSurgery) -> OtSurgeryResponse:
    """Map OtSurgery entity to OtSurgeryResponse contract."""
    dept = item.department
    room = item.ot_room_ref
    return OtSurgeryResponse(
        id=item.id,
        hospital_id=item.hospital_id,
        surgery_no=item.surgery_no,
        patient_id=item.patient_id,
        surgeon_id=item.surgeon_id,
        assistant_surgeon=item.assistant_surgeon,
        surgery_type=item.surgery_type,
        surgery_category=item.surgery_category,
        priority=item.priority,
        department_id=item.department_id,
        ot_room_id=item.ot_room_id,
        ot_room=item.ot_room,
        ot_charge_amount=float(getattr(item, "ot_charge_amount", 0) or 0),
        ot_room_rate=float(getattr(room, "base_ot_charge", 0) or 0) if room else None,
        scheduled_at=item.scheduled_at,
        duration_minutes=item.duration_minutes,
        anaesthetist=item.anaesthetist,
        remarks=item.remarks,
        booked_by_name=item.booked_by_name,
        booked_by_role=item.booked_by_role,
        status=item.status,
        started_at=item.started_at,
        completed_at=item.completed_at,
        actual_duration_minutes=item.actual_duration_minutes,
        shifted_to=item.shifted_to,
        pre_op_diagnosis=item.pre_op_diagnosis,
        procedure_performed=item.procedure_performed,
        findings=item.findings,
        implants_used=item.implants_used,
        complications=item.complications,
        post_op_instructions=item.post_op_instructions,
        follow_up_notes=item.follow_up_notes,
        notes_recorded_by=item.notes_recorded_by,
        notes_recorded_at=item.notes_recorded_at,
        ot_report_file_name=item.ot_report_file_name,
        has_ot_report=bool(item.ot_report_file_data),
        consent_file_name=item.consent_file_name,
        has_consent=bool(item.consent_file_data),
        image_file_name=item.image_file_name,
        has_image=bool(item.image_file_data),
        has_notes=bool(item.pre_op_diagnosis and item.procedure_performed),
        created_at=item.created_at,
        patient_name=item.patient.name if item.patient else None,
        patient_uhid=item.patient.uhid if item.patient else None,
        patient_mobile=item.patient.mobile if item.patient else None,
        surgeon_name=item.surgeon.name if item.surgeon else None,
        department_name=dept.name if dept else None,
        ot_room_name=room.name if room else None,
    )


def sync_ot_surgery_medical_record(db: Session, surgery: OtSurgery) -> None:
    """Mirror OT operation notes into surgeon's medical records for the patient."""
    if not surgery.surgeon_id:
        return
    if not (surgery.pre_op_diagnosis and surgery.procedure_performed):
        return

    existing = (
        db.query(MedicalRecord)
        .filter(
            MedicalRecord.hospital_id == surgery.hospital_id,
            MedicalRecord.patient_id == surgery.patient_id,
            MedicalRecord.doctor_id == surgery.surgeon_id,
            MedicalRecord.report_type == "OT",
            MedicalRecord.title.like(f"%{surgery.surgery_no}%"),
        )
        .first()
    )

    parts: list[str] = [
        f"Surgery: {surgery.surgery_type} ({surgery.surgery_no})",
        f"OT Room: {surgery.ot_room}",
        f"Pre-op Diagnosis:\n{surgery.pre_op_diagnosis}",
        f"Procedure Performed:\n{surgery.procedure_performed}",
    ]
    if surgery.findings:
        parts.append(f"Findings:\n{surgery.findings}")
    if surgery.implants_used:
        parts.append(f"Implants Used:\n{surgery.implants_used}")
    if surgery.complications:
        parts.append(f"Complications:\n{surgery.complications}")
    if surgery.post_op_instructions:
        parts.append(f"Post-op Instructions:\n{surgery.post_op_instructions}")
    if surgery.follow_up_notes:
        parts.append(f"Follow-up Notes:\n{surgery.follow_up_notes}")
    notes = "\n\n".join(parts)

    if existing:
        existing.notes = notes
        existing.file_name = surgery.ot_report_file_name
        existing.file_data = surgery.ot_report_file_data
        return

    db.add(
        MedicalRecord(
            hospital_id=surgery.hospital_id,
            doctor_id=surgery.surgeon_id,
            patient_id=surgery.patient_id,
            report_type="OT",
            title=f"OT Summary — {surgery.surgery_no} ({surgery.surgery_type})",
            notes=notes,
            file_name=surgery.ot_report_file_name,
            file_data=surgery.ot_report_file_data,
        )
    )


def stream_ot_file(item: OtSurgery, kind: str) -> StreamingResponse:
    """Stream OT report, consent form, or surgical photo binary payload."""
    if kind == "report":
        data, name = item.ot_report_file_data, item.ot_report_file_name or "ot-report.pdf"
    elif kind == "consent":
        data, name = item.consent_file_data, item.consent_file_name or "consent.pdf"
    elif kind == "image":
        data, name = item.image_file_data, item.image_file_name or "image.jpg"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="kind must be report, consent, or image")

    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if data.startswith("data:"):
        header, b64 = data.split(",", 1)
        mime = header.split(";")[0].replace("data:", "") or "application/octet-stream"
        raw = base64.b64decode(b64)
        return StreamingResponse(
            BytesIO(raw),
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file data")


def generate_ot_summary_html(item: OtSurgery, hospital_name: str | None = None) -> str:
    """Generate printable HTML summary for an operation theatre surgery."""
    hosp = hospital_name or "Hospital"
    scheduled = item.scheduled_at.strftime("%d %b %Y %H:%M") if item.scheduled_at else "—"
    started = item.started_at.strftime("%d %b %Y %H:%M") if item.started_at else "—"
    completed = item.completed_at.strftime("%d %b %Y %H:%M") if item.completed_at else "—"
    duration = item.actual_duration_minutes or item.duration_minutes

    def esc(s: str | None) -> str:
        return (s or "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    patient_name = item.patient.name if item.patient else "—"
    patient_uhid = item.patient.uhid if item.patient else ""
    surgeon_name = item.surgeon.name if item.surgeon else "—"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{item.surgery_no} OT Summary</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 840px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #c2410c; margin-bottom: 4px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .box {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; margin: 12px 0; background: #fff7ed; }}
  .label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #9a3412; }}
  .value {{ margin-top: 6px; white-space: pre-wrap; font-size: 14px; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <h1>{esc(hosp)}</h1>
  <p class="meta">Operation Theatre Summary · {esc(item.surgery_no)} · {esc(item.surgery_type)}</p>
  <div class="grid">
    <p><strong>Patient:</strong> {esc(patient_name)} ({esc(patient_uhid)})</p>
    <p><strong>Surgeon:</strong> {esc(surgeon_name)}</p>
    <p><strong>OT Room:</strong> {esc(item.ot_room)}</p>
    <p><strong>Priority:</strong> {esc(item.priority.value if item.priority else '—')}</p>
    <p><strong>Scheduled:</strong> {scheduled}</p>
    <p><strong>Duration:</strong> {duration} min</p>
    <p><strong>Started:</strong> {started}</p>
    <p><strong>Completed:</strong> {completed}</p>
    <p><strong>Anaesthetist:</strong> {esc(item.anaesthetist)}</p>
    <p><strong>Shifted to:</strong> {esc(item.shifted_to)}</p>
  </div>
  <div class="box"><div class="label">Pre-operative Diagnosis</div><div class="value">{esc(item.pre_op_diagnosis)}</div></div>
  <div class="box"><div class="label">Procedure Performed</div><div class="value">{esc(item.procedure_performed)}</div></div>
  <div class="box"><div class="label">Findings</div><div class="value">{esc(item.findings)}</div></div>
  <div class="box"><div class="label">Implants Used</div><div class="value">{esc(item.implants_used)}</div></div>
  <div class="box"><div class="label">Complications</div><div class="value">{esc(item.complications)}</div></div>
  <div class="box"><div class="label">Post-operative Instructions</div><div class="value">{esc(item.post_op_instructions)}</div></div>
  <div class="box"><div class="label">Follow-up Notes</div><div class="value">{esc(item.follow_up_notes)}</div></div>
</body></html>"""
