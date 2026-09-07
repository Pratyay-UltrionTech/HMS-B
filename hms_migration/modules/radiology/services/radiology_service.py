"""
Business logic and utility services for Radiology domain.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from hms_migration.modules.clinical_records.entities.clinical_record import MedicalRecord
from hms_migration.modules.radiology.contracts.radiology_contracts import RadOrderResponse
from hms_migration.modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
)

STANDARD_RADIOLOGY_SCANS: list[dict[str, Any]] = [
    {"scan_code": "XRCHEST", "scan_name": "X-Ray Chest", "category": "X-Ray", "department": "Radiology", "price": 400.0, "duration_minutes": 15, "description": "Chest radiograph"},
    {"scan_code": "XRKNEE", "scan_name": "X-Ray Knee", "category": "X-Ray", "department": "Radiology", "price": 450.0, "duration_minutes": 15, "description": "Knee radiograph"},
    {"scan_code": "XRSPINE", "scan_name": "X-Ray Spine", "category": "X-Ray", "department": "Radiology", "price": 500.0, "duration_minutes": 20, "description": "Spine radiograph"},
    {"scan_code": "XRPELVIS", "scan_name": "X-Ray Pelvis", "category": "X-Ray", "department": "Radiology", "price": 450.0, "duration_minutes": 15, "description": "Pelvis radiograph"},
    {"scan_code": "USGABD", "scan_name": "Ultrasound Abdomen", "category": "Ultrasound", "department": "Radiology", "price": 900.0, "duration_minutes": 30, "description": "Abdominal ultrasound"},
    {"scan_code": "USGPEL", "scan_name": "Ultrasound Pelvis", "category": "Ultrasound", "department": "Radiology", "price": 900.0, "duration_minutes": 30, "description": "Pelvic ultrasound"},
    {"scan_code": "USGPREG", "scan_name": "Ultrasound Pregnancy", "category": "Ultrasound", "department": "Radiology", "price": 1200.0, "duration_minutes": 30, "description": "Obstetric ultrasound"},
    {"scan_code": "CTBRAIN", "scan_name": "CT Brain", "category": "CT", "department": "Radiology", "price": 3500.0, "duration_minutes": 30, "description": "CT brain"},
    {"scan_code": "CTCHEST", "scan_name": "CT Chest", "category": "CT", "department": "Radiology", "price": 4500.0, "duration_minutes": 30, "description": "CT chest"},
    {"scan_code": "CTABD", "scan_name": "CT Abdomen", "category": "CT", "department": "Radiology", "price": 5000.0, "duration_minutes": 40, "description": "CT abdomen"},
    {"scan_code": "CTSPINE", "scan_name": "CT Spine", "category": "CT", "department": "Radiology", "price": 4500.0, "duration_minutes": 30, "description": "CT spine"},
    {"scan_code": "MRIBRAIN", "scan_name": "MRI Brain", "category": "MRI", "department": "Radiology", "price": 7000.0, "duration_minutes": 45, "description": "MRI brain"},
    {"scan_code": "MRISPINE", "scan_name": "MRI Spine", "category": "MRI", "department": "Radiology", "price": 7500.0, "duration_minutes": 45, "description": "MRI spine"},
    {"scan_code": "MRIKNEE", "scan_name": "MRI Knee", "category": "MRI", "department": "Radiology", "price": 6500.0, "duration_minutes": 40, "description": "MRI knee"},
    {"scan_code": "MRISHOULDER", "scan_name": "MRI Shoulder", "category": "MRI", "department": "Radiology", "price": 6500.0, "duration_minutes": 40, "description": "MRI shoulder"},
    {"scan_code": "ECHO2D", "scan_name": "2D Echo", "category": "Cardiology Imaging", "department": "Cardiology", "price": 2500.0, "duration_minutes": 30, "description": "2D echocardiography"},
    {"scan_code": "ECG", "scan_name": "ECG", "category": "Cardiology Imaging", "department": "Cardiology", "price": 300.0, "duration_minutes": 10, "description": "Electrocardiogram"},
    {"scan_code": "MAMMO", "scan_name": "Mammography", "category": "Mammography", "department": "Radiology", "price": 1800.0, "duration_minutes": 30, "description": "Mammogram"},
    {"scan_code": "DEXA", "scan_name": "Dexa Scan", "category": "Bone Density", "department": "Radiology", "price": 2200.0, "duration_minutes": 30, "description": "DEXA bone densitometry"},
]


def order_to_response(order: RadiologyOrder) -> RadOrderResponse:
    """Map RadiologyOrder SQLAlchemy entity to RadOrderResponse contract."""
    return RadOrderResponse(
        id=order.id,
        hospital_id=order.hospital_id,
        order_no=order.order_no,
        patient_id=order.patient_id,
        doctor_id=order.doctor_id,
        appointment_id=order.appointment_id,
        scan_id=order.scan_id,
        scan_code=order.scan_code,
        scan_name=order.scan_name,
        category=order.category,
        price=order.price,
        ordered_by_name=order.ordered_by_name,
        ordered_by_role=order.ordered_by_role,
        status=order.status,
        clinical_notes=order.clinical_notes,
        scheduled_at=order.scheduled_at,
        machine=order.machine,
        technician_name=order.technician_name,
        started_at=order.started_at,
        completed_at=order.completed_at,
        findings=order.findings,
        impression=order.impression,
        remarks=order.remarks,
        report_file_name=order.report_file_name,
        has_report_file=bool(order.report_file_data),
        image_file_name=order.image_file_name,
        has_image_file=bool(order.image_file_data),
        report_uploaded_by=order.report_uploaded_by,
        report_date=order.report_date,
        ordered_at=order.ordered_at,
        patient_name=order.patient.name if order.patient else None,
        patient_uhid=order.patient.uhid if order.patient else None,
        patient_mobile=order.patient.mobile if order.patient else None,
        doctor_name=order.doctor.name if order.doctor else None,
    )


def sync_radiology_order_medical_record(db: Session, order: RadiologyOrder) -> None:
    """Sync completed radiology report into MedicalRecord clinical history."""
    if order.status != RadiologyOrderStatus.completed or not order.doctor_id:
        return
    if not (order.findings or order.impression or order.report_file_data):
        return

    existing = (
        db.query(MedicalRecord)
        .filter(
            MedicalRecord.hospital_id == order.hospital_id,
            MedicalRecord.radiology_order_id == order.id,
        )
        .first()
    )
    parts: list[str] = []
    if order.findings:
        parts.append(f"Findings:\n{order.findings}")
    if order.impression:
        parts.append(f"Impression:\n{order.impression}")
    if order.remarks:
        parts.append(f"Remarks:\n{order.remarks}")
    notes = "\n\n".join(parts)

    report_type = "X-Ray"
    cat = (order.category or "").lower()
    if "mri" in cat:
        report_type = "MRI"
    elif "ct" in cat:
        report_type = "Other"

    if existing:
        existing.title = f"Radiology — {order.scan_name} ({order.order_no})"
        existing.notes = notes
        existing.appointment_id = order.appointment_id
        existing.file_name = order.report_file_name
        existing.file_data = order.report_file_data
        return

    db.add(
        MedicalRecord(
            hospital_id=order.hospital_id,
            doctor_id=order.doctor_id,
            patient_id=order.patient_id,
            appointment_id=order.appointment_id,
            radiology_order_id=order.id,
            report_type=report_type,
            title=f"Radiology — {order.scan_name} ({order.order_no})",
            notes=notes,
            file_name=order.report_file_name,
            file_data=order.report_file_data,
        )
    )


def stream_radiology_file(order: RadiologyOrder, kind: str) -> StreamingResponse:
    """Stream report or scan image binary data."""
    if kind == "report":
        data, name = order.report_file_data, order.report_file_name or "report.pdf"
    elif kind == "image":
        data, name = order.image_file_data, order.image_file_name or "image.jpg"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="kind must be report or image")

    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if data.startswith("data:"):
        header, b64 = data.split(",", 1)
        mime = header.split(";")[0].replace("data:", "") or "application/octet-stream"
        raw = base64.b64decode(b64)
        return StreamingResponse(
            BytesIO(raw),
            media_type=mime,
            headers={"Content-Disposition": f'inline; filename="{name}"'},
        )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file data")


def generate_radiology_report_html(order: RadiologyOrder, hospital_name: str | None = None) -> str:
    """Generate printable HTML report for a radiology examination."""
    hosp = hospital_name or "Hospital"
    report_date = order.report_date.isoformat() if order.report_date else "—"
    ordered = order.ordered_at.strftime("%d %b %Y %H:%M") if order.ordered_at else "—"

    def esc(s: str | None) -> str:
        return (s or "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    image_block = ""
    if order.image_file_data and str(order.image_file_data).startswith("data:image"):
        image_block = f"""
  <div class="box">
    <div class="label">Scan Image</div>
    <div class="value" style="text-align:center">
      <img src="{order.image_file_data}" alt="{esc(order.image_file_name or order.scan_name)}"
           style="max-width:100%;max-height:640px;border-radius:8px;border:1px solid #e2e8f0"/>
    </div>
  </div>"""
    elif order.image_file_data and str(order.image_file_data).startswith("data:"):
        image_block = f"""
  <div class="box">
    <div class="label">Scan Image</div>
    <div class="value">
      <p>Image on file: {esc(order.image_file_name or 'scan file')} — open via Download Image in HMS.</p>
    </div>
  </div>"""
    else:
        image_block = """
  <div class="box">
    <div class="label">Scan Image</div>
    <div class="value" style="color:#94a3b8">No scan image uploaded for this order.</div>
  </div>"""

    patient_name = order.patient.name if order.patient else None
    patient_uhid = order.patient.uhid if order.patient else None
    patient_age = order.patient.age if order.patient and order.patient.age is not None else "—"
    doctor_name = order.doctor.name if order.doctor else order.ordered_by_name

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{order.order_no} Radiology Report</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 800px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #0d9488; margin-bottom: 4px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}
  .box {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; margin: 12px 0; background: #f8fafc; }}
  .label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #64748b; }}
  .value {{ margin-top: 6px; white-space: pre-wrap; font-size: 14px; }}
  @media print {{ body {{ margin: 16px; }} img {{ max-height: 480px !important; }} }}
</style></head><body>
  <h1>{esc(hosp)}</h1>
  <p class="meta">Radiology Report · {esc(order.order_no)} · {esc(order.scan_name)} ({esc(order.scan_code)})</p>
  <p><strong>Patient:</strong> {esc(patient_name)} ({esc(patient_uhid)}) · Age: {patient_age}</p>
  <p><strong>Referred by:</strong> {esc(doctor_name)} · Ordered: {ordered}</p>
  <p><strong>Report Date:</strong> {report_date} · <strong>Uploaded by:</strong> {esc(order.report_uploaded_by)}</p>
  {image_block}
  <div class="box"><div class="label">Findings</div><div class="value">{esc(order.findings)}</div></div>
  <div class="box"><div class="label">Impression</div><div class="value">{esc(order.impression)}</div></div>
  <div class="box"><div class="label">Remarks</div><div class="value">{esc(order.remarks)}</div></div>
</body></html>"""
