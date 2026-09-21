"""
Laboratory report generation and medical record sync services.

Provides HTML report formatting and synchronization of finalized lab results
with patient clinical records.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session
from weasyprint import HTML

from modules.clinical_records.entities.clinical_record import MedicalRecord
from modules.laboratory.entities.lab_entities import LabOrder, LabOrderStatus


def generate_lab_report_html(
    order: LabOrder,
    hospital_name: str = "Hospital",
    hospital_address: str = "",
    hospital_phone: str = "",
    hospital_email: str = "",
) -> str:
    """Generate printable HTML laboratory test report."""
    status = order.status.value.replace("_", " ").title()
    ordered = order.ordered_at.strftime("%d %b %Y %H:%M") if order.ordered_at else "—"
    collected = order.collected_at.strftime("%d %b %Y %H:%M") if order.collected_at else "—"
    collected_by = order.collected_by or ""
    sample_name = (order.sample_type.value if order.sample_type else "—").title()
    patient_name = order.patient.name if order.patient and order.patient.name else "—"
    patient_uhid = order.patient.uhid if order.patient and order.patient.uhid else ""
    patient_age = str(order.patient.age) if order.patient and order.patient.age is not None else "—"
    ref_by = order.doctor.name if order.doctor else (order.ordered_by_name or "Self")
    tests = ", ".join(f"{i.test_name} ({i.test_code})" for i in (order.items or []))
    panels = sorted({i.panel_name for i in (order.items or []) if i.panel_name})

    rows = "".join(
        f"<tr><td>{r.parameter_name}</td><td>{r.result_value}</td>"
        f"<td>{r.unit or ''}</td><td>{r.reference_range or '—'}</td>"
        f"<td>{r.remarks or '—'}</td></tr>"
        for r in (order.results or [])
    )
    if not rows:
        rows = "<tr><td colspan='5'>No results entered yet.</td></tr>"

    hospital_lines = [f"<div class='hname'>{hospital_name}</div>"]
    if hospital_address:
        hospital_lines.append(f"<div class='hdetail'>{hospital_address}</div>")
    if hospital_phone:
        hospital_lines.append(f"<div class='hdetail'>Phone: {hospital_phone}</div>")
    if hospital_email:
        hospital_lines.append(f"<div class='hdetail'>Email: {hospital_email}</div>")
    hospital_block = "".join(hospital_lines)

    patient_fields = [f"<tr><th>Patient Name</th><td>{patient_name}</td></tr>"]
    if patient_uhid:
        patient_fields.append(f"<tr><th>UHID</th><td>{patient_uhid}</td></tr>")
    if patient_age != "—":
        patient_fields.append(f"<tr><th>Age</th><td>{patient_age}</td></tr>")

    completed = order.completed_at.strftime("%d %b %Y %H:%M") if order.completed_at else "—"
    order_fields = [
        f"<tr><th>Lab Order No</th><td>{order.order_no}</td></tr>",
        f"<tr><th>Ordered Date &amp; Time</th><td>{ordered}</td></tr>",
        f"<tr><th>Sample Collected</th><td>{collected}</td></tr>",
        f"<tr><th>Report Completed</th><td>{completed}</td></tr>",
        f"<tr><th>Sample Type</th><td>{sample_name}</td></tr>",
        f"<tr><th>Report Status</th><td>{status}</td></tr>",
    ]
    if collected_by:
        order_fields.append(f"<tr><th>Collected By</th><td>{collected_by}</td></tr>")

    panels_section = (
        f"<div class='field'><span class='field-label'>Panels</span>"
        f"<span class='field-value'>{', '.join(panels)}</span></div>"
        if panels
        else ""
    )

    tests_section = (
        f"<div class='field'><span class='field-label'>Tests</span>"
        f"<span class='field-value'>{tests}</span></div>"
        if tests
        else ""
    )

    notes_section = (
        f"<div class='notes'><h3>Clinical Notes</h3><p>{order.clinical_notes}</p></div>"
        if order.clinical_notes
        else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{order.order_no} Lab Report</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 0; color: #1f2937; }}
  .page {{ max-width: 800px; margin: 32px auto; padding: 0 24px; }}
  .hospital {{ border-bottom: 2px solid #2563eb; padding-bottom: 12px; margin-bottom: 20px; }}
  .hname {{ font-size: 26px; font-weight: 700; color: #1f2937; }}
  .hdetail {{ font-size: 12px; color: #6b7280; margin-top: 2px; }}
  .title-block {{ margin-bottom: 20px; }}
  .title-block h2 {{ font-size: 22px; margin: 0 0 4px; color: #1d4ed8; letter-spacing: 0.5px; }}
  .title-block .meta {{ color: #6b7280; font-size: 13px; margin: 0; }}
  .info-grid {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
  .info-grid td {{ vertical-align: top; width: 50%; padding: 0; }}
  .info-grid table {{ width: 100%; border-collapse: collapse; }}
  .info-grid th, .info-grid td {{ border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; font-size: 13px; }}
  .info-grid th {{ width: 42%; color: #4b5563; font-weight: 600; background: #f9fafb; }}
  .field {{ margin: 6px 0; font-size: 14px; }}
  .field-label {{ font-weight: 600; color: #4b5563; }}
  .field-value {{ color: #1f2937; }}
  h3 {{ font-size: 14px; margin: 16px 0 8px; color: #1f2937; border-bottom: 1px solid #e5e7eb; padding-bottom: 4px; }}
  table.results {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  table.results th, table.results td {{ border: 1px solid #e5e7eb; padding: 7px 10px; text-align: left; font-size: 13px; }}
  table.results th {{ background: #eff6ff; color: #1e3a8a; font-weight: 600; }}
  table.results tbody tr:nth-child(even) {{ background: #f9fafb; }}
  .notes p {{ font-size: 13px; color: #374151; margin: 8px 0 0; }}
  .footer {{ text-align: center; color: #9ca3af; font-size: 11px; margin-top: 32px; padding-top: 12px; border-top: 1px solid #e5e7eb; }}
  @media print {{
    body {{ margin: 0; }}
    .page {{ max-width: 100%; margin: 0; padding: 0; }}
    .info-grid td {{ page-break-inside: avoid; }}
    table.results thead {{ display: table-header-group; }}
    table.results tr {{ page-break-inside: avoid; }}
    @page {{ margin: 14mm; }}
  }}
</style></head><body>
<div class="page">
  <div class="hospital">{hospital_block}</div>
  <div class="title-block">
    <h2>LABORATORY REPORT</h2>
    <p class="meta">Order No: {order.order_no} &nbsp;·&nbsp; Status: {status}</p>
  </div>
  <table class="info-grid">
    <tr>
      <td>
        <table>
          <tbody>{''.join(patient_fields)}</tbody>
        </table>
      </td>
      <td>
        <table>
          <tbody>{''.join(order_fields)}</tbody>
        </table>
      </td>
    </tr>
  </table>
  <div class="field"><span class="field-label">Referred / Ordered By</span> <span class="field-value">{ref_by}</span></div>
  {panels_section}
  {tests_section}
  <h3>Test Results</h3>
  <table class="results">
    <thead><tr><th>Test / Parameter</th><th>Result</th><th>Unit</th><th>Reference Range</th><th>Remarks</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  {notes_section}
  <div class="footer">This is a computer-generated laboratory report.</div>
</div>
<script>window.onload=function(){{window.print();}}</script>
</body></html>"""


def generate_lab_report_pdf(
    order: LabOrder,
    hospital_name: str = "Hospital",
    hospital_address: str = "",
    hospital_phone: str = "",
    hospital_email: str = "",
) -> bytes:
    """Generate a PDF laboratory test report via WeasyPrint."""
    html = generate_lab_report_html(order, hospital_name, hospital_address, hospital_phone, hospital_email)
    return HTML(string=html).write_pdf()


def sync_lab_order_medical_record(db: Session, order: LabOrder) -> None:
    """Create or update patient medical record when lab results are completed."""
    if order.status != LabOrderStatus.completed or not order.doctor_id:
        return
    existing = (
        db.query(MedicalRecord)
        .filter(
            MedicalRecord.hospital_id == order.hospital_id,
            MedicalRecord.lab_order_id == order.id,
        )
        .first()
    )
    test_names = ", ".join(i.test_name for i in (order.items or [])) or "Laboratory tests"
    panel_names = sorted({i.panel_name for i in (order.items or []) if i.panel_name})
    title_bits = f"Lab Report — {order.order_no}"
    if panel_names:
        title_bits = f"Lab Report — {order.order_no} ({', '.join(panel_names)})"
    lines = []
    for r in order.results or []:
        lines.append(f"{r.parameter_name}: {r.result_value} {r.unit or ''}".strip())
    notes = "\n".join(lines) if lines else order.clinical_notes
    if panel_names:
        panel_line = f"Panels: {', '.join(panel_names)}\nTests: {test_names}"
        notes = f"{panel_line}\n\n{notes}" if notes else panel_line

    if existing:
        existing.title = title_bits
        existing.notes = notes
        existing.appointment_id = order.appointment_id
        return

    db.add(
        MedicalRecord(
            hospital_id=order.hospital_id,
            doctor_id=order.doctor_id,
            patient_id=order.patient_id,
            appointment_id=order.appointment_id,
            lab_order_id=order.id,
            report_type="Blood Report",
            title=title_bits,
            notes=notes,
            file_name=None,
            file_data=None,
        )
    )
