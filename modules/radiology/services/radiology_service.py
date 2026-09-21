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
from weasyprint import HTML

from modules.clinical_records.entities.clinical_record import MedicalRecord
from modules.radiology.contracts.radiology_contracts import RadOrderResponse
from modules.radiology.entities.radiology_entities import (
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


def order_to_response(order: RadiologyOrder, db: Session | None = None) -> RadOrderResponse:
    """Map RadiologyOrder SQLAlchemy entity to RadOrderResponse contract."""
    payment_status = "pending"
    is_financially_cleared = False
    net_amount = 0.0
    amount_paid = 0.0
    outstanding_amount = 0.0

    target_db = db or (order._sa_instance_state.session if hasattr(order, "_sa_instance_state") else None)
    if target_db:
        try:
            from modules.billing.entities.billing_entities import BillingSourceType
            from modules.billing.services.service_financial_clearance import check_service_financial_clearance

            fin = check_service_financial_clearance(
                target_db,
                order.hospital_id,
                BillingSourceType.radiology,
                order.id,
            )
            payment_status = fin.status
            is_financially_cleared = fin.is_cleared
            net_amount = fin.net_amount
            amount_paid = fin.amount_paid
            outstanding_amount = fin.outstanding_amount
        except Exception:
            pass

    return RadOrderResponse(
        id=order.id,
        hospital_id=order.hospital_id,
        order_no=order.order_no,
        patient_id=order.patient_id,
        doctor_id=order.doctor_id,
        appointment_id=order.appointment_id,
        prescription_id=order.prescription_id,
        prescription_request_id=order.prescription_request_id,
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
        has_report_file=bool(getattr(order, "has_report_file", None) if getattr(order, "has_report_file", None) is not None else order.report_file_name),
        image_file_name=order.image_file_name,
        has_image_file=bool(getattr(order, "has_image_file", None) if getattr(order, "has_image_file", None) is not None else order.image_file_name),
        report_uploaded_by=order.report_uploaded_by,
        report_date=order.report_date,
        is_amended=bool(getattr(order, "is_amended", False)),
        amendment_reason=getattr(order, "amendment_reason", None),
        ordered_at=order.ordered_at,
        patient_name=order.patient.name if order.patient else None,
        patient_uhid=order.patient.uhid if order.patient else None,
        patient_mobile=order.patient.mobile if order.patient else None,
        doctor_name=order.doctor.name if order.doctor else None,
        payment_status=payment_status,
        is_financially_cleared=is_financially_cleared,
        net_amount=net_amount,
        amount_paid=amount_paid,
        outstanding_amount=outstanding_amount,
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


def stream_radiology_file(
    order: RadiologyOrder,
    kind: str,
    hospital_name: str | None = None,
    hospital_address: str | None = None,
    hospital_phone: str | None = None,
    hospital_email: str | None = None,
) -> StreamingResponse:
    """Stream report or scan image binary data."""
    if kind == "report":
        if order.report_file_data:
            data, name = order.report_file_data, order.report_file_name or "report.pdf"
        elif order.findings or order.impression or order.status == RadiologyOrderStatus.completed:
            html = generate_radiology_report_html(
                order,
                hospital_name,
                hospital_address,
                hospital_phone,
                hospital_email,
            )
            return StreamingResponse(
                BytesIO(html.encode("utf-8")),
                media_type="text/html",
                headers={"Content-Disposition": f'attachment; filename="{order.order_no}-radiology.html"'},
            )
        else:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not available")
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
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file data")


def generate_radiology_report_html(
    order: RadiologyOrder,
    hospital_name: str | None = None,
    hospital_address: str | None = None,
    hospital_phone: str | None = None,
    hospital_email: str | None = None,
) -> str:
    """Generate printable HTML report for a radiology examination."""
    hosp = hospital_name or "Hospital"
    report_date = order.report_date.isoformat() if order.report_date else "—"
    ordered = order.ordered_at.strftime("%d %b %Y %H:%M") if order.ordered_at else "—"
    completed = order.completed_at.strftime("%d %b %Y %H:%M") if order.completed_at else "—"
    status_display = (order.status.value or "").replace("_", " ").title() if order.status else "—"

    def esc(s: str | None) -> str:
        return (s or "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def esc_opt(s: str | None) -> str:
        if s is None or s == "":
            return ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    hospital_lines = ""
    hosp_lines = [h for h in [hospital_address, hospital_phone, hospital_email] if h]
    if hosp_lines:
        hospital_lines = '<div class="hosp-contact">' + " · ".join(esc_opt(h) for h in hosp_lines) + "</div>"

    def row(label: str, value: str | None) -> str:
        if value is None or value == "":
            return ""
        return f'<tr><td class="k">{esc(label)}</td><td class="v">{esc(value)}</td></tr>'

    patient_name = order.patient.name if order.patient else None
    patient_uhid = order.patient.uhid if order.patient else None
    patient_age = order.patient.age if order.patient and order.patient.age is not None else None
    patient_mobile = order.patient.mobile if order.patient else None

    left_rows = ""
    left_rows += row("Patient Name", patient_name)
    if patient_uhid:
        left_rows += row("UHID", patient_uhid)
    if patient_age is not None:
        left_rows += row("Age", str(patient_age))
    if patient_mobile:
        left_rows += row("Mobile", patient_mobile)
    left_block = f"""
      <table class="info"><tbody>{left_rows}</tbody></table>"""

    right_rows = ""
    right_rows += row("Order No", order.order_no)
    right_rows += row("Scan Name", order.scan_name)
    right_rows += row("Scan Code", order.scan_code)
    if order.category:
        right_rows += row("Category", order.category)
    if order.ordered_at:
        right_rows += row("Ordered Date & Time", ordered)
    if order.completed_at:
        right_rows += row("Scan Completed Date", completed)
    if order.report_date:
        right_rows += row("Report Date", report_date)
    right_rows += row("Status", status_display)
    right_block = f"""
      <table class="info"><tbody>{right_rows}</tbody></table>"""

    referred = order.doctor.name if order.doctor else (order.ordered_by_name or "Self")
    referred_block = f'<div class="referred"><span class="k">Referred / Ordered By:</span> {esc(referred)}</div>'
    if order.ordered_by_role:
        referred_block = (
            f'<div class="referred"><span class="k">Referred / Ordered By:</span> '
            f'{esc(referred)} ({esc(order.ordered_by_role)})</div>'
        )

    clinical_block = ""
    if order.clinical_notes:
        clinical_block = f"""
  <section class="clinical">
    <h2>Clinical Notes / Indication</h2>
    <div class="text">{esc(order.clinical_notes)}</div>
  </section>"""

    image_block = ""
    if order.image_file_data and str(order.image_file_data).startswith("data:image"):
        image_block = f"""
        <img src="{order.image_file_data}" alt="{esc(order.image_file_name or order.scan_name)}"
             class="scan-img"/>"""
    elif order.image_file_data and str(order.image_file_data).startswith("data:"):
        image_block = f"""
        <p class="muted">Image on file: {esc(order.image_file_name or 'scan file')} — open via Download Image in HMS.</p>"""
    else:
        image_block = """
        <p class="muted">No scan image uploaded for this order.</p>"""

    additional_rows = ""
    if order.machine:
        additional_rows += f'<div class="ai-row"><span class="k">Machine</span><span class="v">{esc(order.machine)}</span></div>'
    if order.technician_name:
        additional_rows += f'<div class="ai-row"><span class="k">Technician</span><span class="v">{esc(order.technician_name)}</span></div>'
    if order.report_uploaded_by:
        additional_rows += f'<div class="ai-row"><span class="k">Report Uploaded By</span><span class="v">{esc(order.report_uploaded_by)}</span></div>'

    scan_column = f"""
  <div class="two-col">
    <div class="col">
      <h2>Scan Image</h2>
      <div class="img-box">{image_block}</div>
    </div>"""
    if additional_rows:
        scan_column += f"""
    <div class="col">
      <h2>Additional Information</h2>
      <div class="ai">{additional_rows}</div>
    </div>"""
    scan_column += """
  </div>"""

    impression_block = ""
    if order.impression:
        impression_block = f"""
  <section class="impression">
    <h2>Impression</h2>
    <div class="text">{esc(order.impression)}</div>
  </section>"""

    remarks_block = ""
    if order.remarks:
        remarks_block = f"""
  <section class="remarks">
    <h2>Remarks</h2>
    <div class="text">{esc(order.remarks)}</div>
  </section>"""

    meta_rows = ""
    if order.report_date:
        meta_rows += f'<span class="meta-item"><span class="k">Report Date</span>{esc(report_date)}</span>'
    if order.report_uploaded_by:
        meta_rows += f'<span class="meta-item"><span class="k">Report Uploaded By</span>{esc(order.report_uploaded_by)}</span>'
    meta_rows += f'<span class="meta-item"><span class="k">Status</span>{esc(status_display)}</span>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{esc(order.order_no)} Radiology Report</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 800px; margin: 32px auto; padding: 0 16px; color: #1f2937; background: #ffffff; line-height: 1.5; }}
  .hosp {{ text-align: center; padding-bottom: 12px; border-bottom: 2px solid #2563eb; }}
  .hosp-name {{ font-size: 22px; font-weight: 700; color: #111827; margin: 0; }}
  .hosp-contact {{ color: #6b7280; font-size: 12px; margin-top: 4px; }}
  .doc-title {{ text-align: center; margin-top: 18px; }}
  .doc-title h1 {{ font-size: 17px; letter-spacing: .06em; color: #1d4ed8; margin: 0; }}
  .doc-meta {{ color: #6b7280; font-size: 12px; margin-top: 4px; }}
  hr {{ border: 0; border-top: 1px solid #e5e7eb; margin: 18px 0; }}
  h2 {{ font-size: 13px; color: #374151; letter-spacing: .03em; margin: 0 0 8px; padding-bottom: 4px; border-bottom: 1px solid #e5e7eb; }}
  table.info {{ width: 100%; border-collapse: collapse; }}
  table.info td {{ padding: 5px 8px; font-size: 13px; vertical-align: top; }}
  table.info td.k {{ width: 42%; color: #6b7280; }}
  table.info td.v {{ color: #111827; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  .referred {{ margin: 14px 0; font-size: 13px; }}
  .referred .k {{ color: #6b7280; }}
  .clinical .text, .impression .text, .remarks .text {{ white-space: pre-wrap; font-size: 14px; color: #1f2937; }}
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  .img-box {{ border: 1px solid #e5e7eb; border-radius: 4px; padding: 10px; text-align: center; }}
  .scan-img {{ max-width: 100%; max-height: 480px; object-fit: contain; }}
  .muted {{ color: #9ca3af; font-size: 13px; margin: 0; }}
  .ai {{ font-size: 13px; }}
  .ai-row {{ padding: 5px 0; border-bottom: 1px solid #f3f4f6; }}
  .ai-row:last-child {{ border-bottom: 0; }}
  .ai-row .k {{ color: #6b7280; display: inline-block; width: 45%; }}
  .ai-row .v {{ color: #111827; }}
  section {{ margin: 20px 0; }}
  .impression {{ border-left: 3px solid #2563eb; padding-left: 14px; }}
  .meta {{ margin-top: 24px; padding-top: 10px; border-top: 1px solid #e5e7eb; font-size: 12px; color: #6b7280; display: flex; gap: 24px; flex-wrap: wrap; }}
  .meta-item .k {{ display: block; font-size: 11px; color: #9ca3af; }}
  .footer {{ text-align: center; color: #9ca3af; font-size: 11px; margin: 22px 0 0; }}
  @media print {{
    @page {{ margin: 14mm; }}
    body {{ margin: 0; max-width: none; padding: 0; }}
    tr, div {{ page-break-inside: avoid; }}
    img {{ max-width: 100% !important; max-height: 480px !important; }}
  }}
</style></head><body>
  <div class="hosp">
    <p class="hosp-name">{esc(hosp)}</p>
    {hospital_lines}
  </div>
  <div class="doc-title">
    <h1>RADIOLOGY REPORT</h1>
    <div class="doc-meta">Order No: {esc(order.order_no)} &nbsp;·&nbsp; Scan: {esc(order.scan_name)} ({esc(order.scan_code)}) &nbsp;·&nbsp; Status: {esc(status_display)}</div>
  </div>
  <hr/>
  <div class="grid">
    <div>
      <h2>Patient Information</h2>{left_block}
    </div>
    <div>
      <h2>Examination Details</h2>{right_block}
    </div>
  </div>
  {referred_block}
  {clinical_block}
  {scan_column}
  <section class="findings">
    <h2>Findings</h2>
    <div class="text">{esc(order.findings)}</div>
  </section>
  {impression_block}
  {remarks_block}
  <div class="meta">{meta_rows}</div>
  <p class="footer">This is a computer-generated radiology report.</p>
</body></html>"""


def generate_radiology_report_pdf(
    order: RadiologyOrder,
    hospital_name: str | None = None,
    hospital_address: str | None = None,
    hospital_phone: str | None = None,
    hospital_email: str | None = None,
) -> bytes:
    """Generate a PDF radiology report via WeasyPrint."""
    html = generate_radiology_report_html(
        order,
        hospital_name,
        hospital_address,
        hospital_phone,
        hospital_email,
    )
    return HTML(string=html).write_pdf()
