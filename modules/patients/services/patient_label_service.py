"""Patient Identification Label and Appointment Slip generation service.

Generates standard identification sticker (50x25mm), wristband (250x25mm),
and appointment token slip (80mm) HTML representations with barcode / visual identifier,
adhering to strict PHI allowlist and thermal-printer contrast guidelines:
- Pure monochrome black-on-white (#000000 on #ffffff) for maximum thermal clarity (203/300 DPI)
- Patient Name
- UHID / MRN
- Age / Gender
- Blood Group (if known)
- Machine-readable barcode (Code128 SVG / minimal opaque payload)

EXCLUDES:
- Room / Bed as patient identifier
- Diagnosis / clinical condition / HIV / STI / psychiatric info
- Medication / lab values
- Full allergen lists (only emergency flags if any)
- National ID in clear text
- Financial or insurance details on pure identification stickers
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from shared.barcodes.code128 import generate_code128_svg


@dataclass(frozen=True)
class LabelAppointmentContext:
    """Resolved appointment fields for appointment-aware labels and slips."""

    op_id: str
    appointment_date: date
    appointment_time: time
    doctor_name: str | None = None
    department_name: str | None = None
    consultation_fee: float | None = None
    visit_type: str | None = None


def _esc(s: str | None) -> str:
    return (s or "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def normalize_gender_short(gender: str | None) -> str:
    """Map stored patient gender values to a compact M/F/O code for labels."""
    if not gender or not str(gender).strip():
        return "—"
    raw = str(gender).strip().lower()
    if raw in {"m", "male", "man", "boy"}:
        return "M"
    if raw in {"f", "female", "woman", "girl"}:
        return "F"
    if raw in {"o", "other", "non-binary", "nonbinary", "nb", "transgender", "prefer not to say"}:
        return "O"
    if len(raw) == 1:
        return raw.upper()
    return raw[:1].upper()


def resolve_facility_branding(hospital: Hospital | None) -> tuple[str, str]:
    """Return (compact header code, display name) from facility settings or hospital record."""
    if not hospital:
        return "HOSPITAL", "Hospital"
    stored = hospital.facility_settings or {}
    display = stored.get(
        "display_name",
        hospital.name.split(" & ")[0] if " & " in hospital.name else hospital.name,
    )
    code = stored.get("institutional_code") or hospital.hospital_id
    header = str(code or display).strip().upper() or "HOSPITAL"
    return header, str(display).strip() or header


def _format_appointment_datetime(appt_date: date, appt_time: time) -> str:
    dt = datetime.combine(appt_date, appt_time)
    return dt.strftime("%d-%b-%Y %H:%M")


def render_appointment_slip_html(
    patient: Patient,
    hospital: Hospital | None,
    appointment: LabelAppointmentContext | None,
    auto_print: bool = True,
) -> str:
    """Render a dedicated 80mm thermal receipt / OPD Token Slip."""
    header_code, display_name = resolve_facility_branding(hospital)
    p_name = _esc(patient.name or f"{patient.first_name} {patient.last_name}".strip())
    p_uhid = _esc(patient.uhid)
    sex_code = normalize_gender_short(patient.gender)
    p_age = f"{patient.age}Y" if patient.age is not None else "—"

    token_no = _esc(appointment.op_id if appointment else "OPD")
    appt_dt = (
        _format_appointment_datetime(appointment.appointment_date, appointment.appointment_time)
        if appointment
        else datetime.now().strftime("%d-%b-%Y %H:%M")
    )
    doctor = _esc(appointment.doctor_name if appointment and appointment.doctor_name else "Consultant")
    dept = _esc(appointment.department_name if appointment and appointment.department_name else "General OPD")
    fee = f"₹{appointment.consultation_fee:.2f}" if appointment and appointment.consultation_fee is not None else "—"
    visit_type = _esc(appointment.visit_type or "OPD")

    barcode_svg = generate_code128_svg(token_no, height=36, quiet_modules=8)
    print_script = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""

    hosp_address = _esc(getattr(hospital, "address", "") or "")
    hosp_phone = _esc(getattr(hospital, "phone", "") or "")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=80mm, initial-scale=1"/>
<title>OPD Token - {token_no}</title>
<style>
  @page {{
    size: 80mm auto;
    margin: 3mm 4mm;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    background: #ffffff;
    color: #000000;
    width: 72mm;
    margin: 0 auto;
    padding: 2mm 0;
    font-size: 9pt;
    line-height: 1.25;
  }}
  .text-center {{ text-align: center; }}
  .bold {{ font-weight: 700; }}
  .hosp-header {{ text-align: center; border-bottom: 1px dashed #000000; padding-bottom: 2mm; margin-bottom: 2mm; }}
  .hosp-name {{ font-size: 11pt; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; }}
  .hosp-meta {{ font-size: 7.5pt; color: #000000; }}
  .token-box {{
    border: 1.5px solid #000000;
    border-radius: 2px;
    padding: 2mm;
    margin: 2mm 0;
    text-align: center;
  }}
  .token-label {{ font-size: 7.5pt; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; }}
  .token-value {{ font-size: 16pt; font-weight: 900; letter-spacing: 1px; margin: 1mm 0; font-family: monospace; }}
  .field-table {{ width: 100%; border-collapse: collapse; margin: 2mm 0; }}
  .field-table td {{ padding: 1mm 0; font-size: 8.5pt; vertical-align: top; }}
  .field-label {{ width: 26mm; color: #000000; font-weight: 600; }}
  .field-val {{ font-weight: 700; }}
  .divider {{ border-top: 1px dashed #000000; margin: 2mm 0; }}
  .barcode-wrap {{ text-align: center; margin: 2mm 0; }}
  .barcode-svg {{ display: block; margin: 0 auto; max-width: 60mm; }}
  .barcode-txt {{ font-family: monospace; font-size: 8pt; font-weight: 700; margin-top: 1mm; }}
  .footer-note {{ text-align: center; font-size: 7pt; font-style: italic; margin-top: 2mm; }}
  @media print {{
    body {{ width: 100%; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
</style></head><body>
  <div class="hosp-header">
    <div class="hosp-name">{_esc(display_name)}</div>
    {f'<div class="hosp-meta">{hosp_address}</div>' if hosp_address else ''}
    {f'<div class="hosp-meta">Tel: {hosp_phone}</div>' if hosp_phone else ''}
  </div>

  <div class="token-box">
    <div class="token-label">OPD APPOINTMENT TOKEN</div>
    <div class="token-value">{token_no}</div>
    <div style="font-size: 8pt; font-weight: 600;">{visit_type} · Slot: {appt_dt}</div>
  </div>

  <table class="field-table">
    <tr><td class="field-label">Patient:</td><td class="field-val">{p_name}</td></tr>
    <tr><td class="field-label">UHID:</td><td class="field-val" style="font-family: monospace;">{p_uhid}</td></tr>
    <tr><td class="field-label">Age / Sex:</td><td class="field-val">{p_age} / {sex_code}</td></tr>
    <tr><td class="field-label">Doctor:</td><td class="field-val">Dr. {doctor}</td></tr>
    <tr><td class="field-label">Department:</td><td class="field-val">{dept}</td></tr>
    <tr><td class="field-label">Consultation:</td><td class="field-val">{fee}</td></tr>
  </table>

  <div class="divider"></div>

  <div class="barcode-wrap">
    <div class="barcode-svg">{barcode_svg}</div>
    <div class="barcode-txt">{token_no}</div>
  </div>

  <div class="footer-note">Please proceed to OPD Waiting Area. Present token when announced.</div>
  {print_script}
</body></html>"""


def render_patient_label_html(
    patient: Patient,
    hospital: Hospital | None,
    encounter_id: str | None = None,
    format_type: str = "standard",  # standard (50x25mm), wristband (250x25mm), slip / token (80mm)
    auto_print: bool = True,
    appointment: LabelAppointmentContext | None = None,
) -> str:
    """Render patient identification label HTML adhering to high-contrast thermal printer standards."""
    header_code, _display_name = resolve_facility_branding(hospital)

    # Dedicated appointment slip / token path
    if format_type in {"slip", "token", "appointment_slip"}:
        return render_appointment_slip_html(
            patient=patient,
            hospital=hospital,
            appointment=appointment,
            auto_print=auto_print,
        )

    p_name = _esc(patient.name or f"{patient.first_name} {patient.last_name}".strip())
    p_uhid = _esc(patient.uhid)
    sex_code = normalize_gender_short(patient.gender)
    p_age = f"{patient.age}Y" if patient.age is not None else "—"
    p_dob = patient.date_of_birth.strftime("%d-%b-%Y") if patient.date_of_birth else "—"
    p_blood = _esc(patient.blood_group or "—")

    created_dt = (
        patient.created_at.strftime("%d-%b-%Y")
        if hasattr(patient, "created_at") and patient.created_at
        else datetime.now().strftime("%d-%b-%Y")
    )
    # 8.5mm barcode height for high readability on 203 DPI thermal heads
    barcode_svg = generate_code128_svg(patient.uhid or "UNKNOWN", height=30, quiet_modules=8)

    print_script = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""
    enc_ref = encounter_id or (appointment.op_id if appointment else None)

    # Wristband format (250mm x 25mm)
    if format_type == "wristband":
        hosp_title = _esc(header_code)
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Wristband - {p_uhid}</title>
<style>
  @page {{ size: 250mm 25mm; margin: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    background: #ffffff;
    color: #000000;
    width: 250mm;
    height: 25mm;
    display: flex;
    align-items: center;
    padding: 2mm 5mm;
  }}
  .band {{ display: flex; align-items: center; justify-content: space-between; width: 100%; border-right: 1px dashed #000000; padding-right: 10mm; }}
  .hosp {{ font-size: 8pt; font-weight: 800; text-transform: uppercase; color: #000000; letter-spacing: 0.5px; }}
  .name {{ font-size: 11pt; font-weight: 900; color: #000000; line-height: 1.1; margin: 1mm 0; }}
  .meta {{ font-size: 8pt; font-weight: 700; color: #000000; }}
  .barcode-wrap {{ width: 45mm; text-align: center; }}
  .uhid-txt {{ font-size: 8.5pt; font-family: monospace; font-weight: 800; letter-spacing: 1px; margin-top: 1mm; color: #000000; }}
  @media print {{ body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} }}
</style></head><body>
  <div class="band">
    <div>
      <div class="hosp">{hosp_title}</div>
      <div class="name">{p_name}</div>
      <div class="meta">DOB: {p_dob} ({p_age}) · {sex_code} · BG: {p_blood}</div>
    </div>
    <div class="barcode-wrap">
      {barcode_svg}
      <div class="uhid-txt">{p_uhid}</div>
    </div>
    <div style="font-size: 7.5pt; color: #000000; font-weight: 600; text-align: right;">
      {f'<div>ENC: {_esc(enc_ref)}</div>' if enc_ref else ''}
      <div>REG: {created_dt}</div>
    </div>
  </div>
  {print_script}
</body></html>"""

    # 50mm x 25mm Standard Patient Identification Label (Thermal Roll Sticker)
    # Strictly prioritises PATIENT IDENTITY: Pure #000000 on #ffffff, no dithered blues/grays
    blood_group_badge = f" · <strong>BG:</strong> {p_blood}" if p_blood and p_blood != "—" else ""
    
    appt_line = ""
    if appointment:
        appt_dt = _format_appointment_datetime(appointment.appointment_date, appointment.appointment_time)
        doc_str = _esc(appointment.doctor_name or "Consultant")
        enc_badge = f'<span class="enc-tag">APPOINTMENT: {_esc(appointment.op_id)}</span>'
        appt_line = f'<div class="appt-meta">{_esc(appt_dt)} · Dr. {doc_str}</div>'
    elif enc_ref:
        enc_badge = f'<span class="enc-tag">{_esc(enc_ref)}</span>'
    else:
        enc_badge = f'<span class="enc-tag">REG {created_dt}</span>'

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=50mm, initial-scale=1"/>
<title>Patient ID - {p_uhid}</title>
<style>
  @page {{
    size: 50mm 25mm;
    margin: 0;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    width: 50mm;
    height: 25mm;
    background: #ffffff;
    color: #000000;
    overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  }}
  body {{
    padding: 1.1mm 1.6mm;
  }}
  .label-container {{
    display: flex;
    flex-direction: column;
    height: 100%;
    justify-content: space-between;
    overflow: hidden;
  }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 0.8px solid #000000;
    padding-bottom: 0.2mm;
    flex-shrink: 0;
  }}
  .hosp-title {{
    font-size: 5.8pt;
    font-weight: 800;
    text-transform: uppercase;
    color: #000000;
    letter-spacing: 0.2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 25mm;
  }}
  .enc-tag {{
    font-size: 5.2pt;
    font-weight: 700;
    font-family: monospace;
    color: #000000;
    white-space: nowrap;
    text-align: right;
    max-width: 23mm;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .identity-body {{
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 0.2mm 0;
    min-height: 0;
  }}
  .patient-name {{
    font-size: 8.5pt;
    font-weight: 900;
    color: #000000;
    line-height: 1.05;
    text-transform: uppercase;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    word-break: break-word;
  }}
  .demographics {{
    font-size: 6pt;
    font-weight: 700;
    color: #000000;
    margin-top: 0.2mm;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .appt-meta {{
    font-size: 5.2pt;
    font-weight: 600;
    color: #000000;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .barcode-section {{
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1mm;
    flex-shrink: 0;
    height: 8mm;
  }}
  .barcode-box {{
    flex: 1;
    height: 8mm;
    overflow: hidden;
  }}
  .barcode-box svg {{
    display: block;
    width: 100%;
    height: 8mm;
  }}
  .uhid-display {{
    font-size: 7.2pt;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-weight: 900;
    letter-spacing: 0.35px;
    color: #000000;
    white-space: nowrap;
    line-height: 1;
    margin-bottom: 0.4mm;
  }}
  @media print {{
    @page {{
      size: 50mm 25mm;
      margin: 0;
    }}
    html, body {{
      width: 50mm !important;
      height: 25mm !important;
      max-width: 50mm !important;
      max-height: 25mm !important;
      margin: 0 !important;
      padding: 1.1mm 1.6mm !important;
      overflow: hidden !important;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
  }}
</style></head><body>
  <div class="label-container">
    <div class="header">
      <span class="hosp-title">{_esc(header_code)}</span>
      {enc_badge}
    </div>
    <div class="identity-body">
      <div class="patient-name">{p_name}</div>
      <div class="demographics">
        <span>Age/Sex: {p_age}/{sex_code}</span>{blood_group_badge}
      </div>
      {appt_line}
    </div>
    <div class="barcode-section">
      <div class="barcode-box">{barcode_svg}</div>
      <div class="uhid-display">{p_uhid}</div>
    </div>
  </div>
  {print_script}
</body></html>"""
