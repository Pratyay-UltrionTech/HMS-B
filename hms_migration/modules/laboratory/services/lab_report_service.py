"""
Laboratory report generation and medical record sync services.

Provides HTML report formatting and synchronization of finalized lab results
with patient clinical records.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from hms_migration.modules.clinical_records.entities.clinical_record import MedicalRecord
from hms_migration.modules.laboratory.entities.lab_entities import LabOrder, LabOrderStatus


def generate_lab_report_html(order: LabOrder, hospital_name: str = "Hospital") -> str:
    """Generate printable HTML laboratory test report."""
    rows = "".join(
        f"<tr><td>{r.parameter_name}</td><td><strong>{r.result_value}</strong>"
        f"{(' ' + r.unit) if r.unit else ''}</td><td>{r.reference_range or '—'}</td>"
        f"<td>{r.remarks or '—'}</td></tr>"
        for r in (order.results or [])
    )
    if not rows:
        rows = "<tr><td colspan='4'>No results entered yet.</td></tr>"
    tests = ", ".join(f"{i.test_name} ({i.test_code})" for i in (order.items or []))
    panels = sorted({i.panel_name for i in (order.items or []) if i.panel_name})
    panel_line = f"<p><strong>Panels:</strong> {', '.join(panels)}</p>" if panels else ""
    collected = order.collected_at.strftime("%d %b %Y %H:%M") if order.collected_at else "—"
    ordered = order.ordered_at.strftime("%d %b %Y %H:%M") if order.ordered_at else "—"
    patient_name = order.patient.name if order.patient else "—"
    patient_uhid = f"({order.patient.uhid})" if order.patient and order.patient.uhid else ""
    patient_age = str(order.patient.age) if order.patient and order.patient.age is not None else "—"
    ref_by = order.doctor.name if order.doctor else (order.ordered_by_name or "Self")
    sample_name = (order.sample_type.value if order.sample_type else "—").title()

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{order.order_no} Lab Report</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 800px; margin: 32px auto; color: #0f172a; }}
  h1 {{ color: #4f46e5; margin-bottom: 4px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; font-size: 13px; }}
  th {{ background: #eef2ff; }}
  @media print {{ body {{ margin: 16px; }} }}
</style></head><body>
  <h1>{hospital_name}</h1>
  <p class="meta">Laboratory Report · {order.order_no} · Status: {order.status.value.replace('_', ' ').title()}</p>
  <p><strong>Patient:</strong> {patient_name} {patient_uhid} · Age: {patient_age}</p>
  <p><strong>Referred by:</strong> {ref_by} · <strong>Ordered:</strong> {ordered}</p>
  {panel_line}
  <p><strong>Tests:</strong> {tests}</p>
  <p><strong>Sample:</strong> {sample_name} · Collected: {collected} by {order.collected_by or '—'}</p>
  <table>
    <thead><tr><th>Test / Parameter</th><th>Result</th><th>Reference Range</th><th>Remarks</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <script>window.onload=function(){{window.print();}}</script>
</body></html>"""


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
