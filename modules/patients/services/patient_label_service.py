"""Patient Identification Label generation service.

Generates standard identification sticker and wristband HTML representations
with barcode / visual identifier, adhering to strict PHI allowlist:
- Hospital Identity (Name, contact)
- Patient Name
- UHID / MRN
- Age / Gender
- Blood Group (if known)
- Registration Date & Time (generic label) or appointment context when encounter_id supplied
- Machine-readable barcode (Code128 SVG / minimal opaque payload)

EXCLUDES:
- Room / Bed as patient identifier
- Diagnosis / clinical condition / HIV / STI / psychiatric info
- Medication / lab values
- Full allergen lists (only emergency flags if any)
- National ID in clear text
- Financial or insurance details
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital


@dataclass(frozen=True)
class LabelAppointmentContext:
    """Resolved appointment fields for appointment-aware patient labels."""

    op_id: str
    appointment_date: date
    appointment_time: time
    doctor_name: str | None = None
    department_name: str | None = None


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
        return "Hospital", "Hospital"
    stored = hospital.facility_settings or {}
    display = stored.get(
        "display_name",
        hospital.name.split(" & ")[0] if " & " in hospital.name else hospital.name,
    )
    code = stored.get("institutional_code") or hospital.hospital_id
    header = str(code or display).strip() or "Hospital"
    return header, str(display).strip() or header


def generate_code128_svg(code: str, height: int = 40, quiet_modules: int = 10) -> str:
    """Generate a clean, self-contained SVG representation of a Code128-B barcode."""
    patterns = {
        ' ': '11011001100', '!': '11001101100', '"': '11001100110', '#': '10010011000',
        '$': '10010001100', '%': '10001001100', '&': '10011001000', "'": '10011000100',
        '(': '10001100100', ')': '11001001000', '*': '11001000100', '+': '11000100100',
        ',': '10110011100', '-': '10011011100', '.': '10011001110', '/': '10111001100',
        '0': '10011101100', '1': '10011100110', '2': '11001110010', '3': '11001011100',
        '4': '11001001110', '5': '11011100100', '6': '11001110100', '7': '11101101110',
        '8': '11101001100', '9': '11100101100', ':': '11100100110', ';': '11101100100',
        '<': '11100110100', '=': '11100110010', '>': '11011011000', '?': '11011000110',
        '@': '11000110110', 'A': '10100011000', 'B': '10001011000', 'C': '10001000110',
        'D': '10110001000', 'E': '10001101000', 'F': '10001100010', 'G': '11010001000',
        'H': '11000101000', 'I': '11000100010', 'J': '10110111000', 'K': '10110001110',
        'L': '10001101110', 'M': '10111011000', 'N': '10111000110', 'O': '10001110110',
        'P': '11101110110', 'Q': '11010001110', 'R': '11000101110', 'S': '11011101000',
        'T': '11011100010', 'U': '11011101110', 'V': '11101011000', 'W': '11101000110',
        'X': '11100010110', 'Y': '11101101000', 'Z': '11101100010', '[': '11100011010',
        '\\': '11101111010', ']': '11001000010', '^': '11110001010', '_': '10100110000',
        'a': '10100001100', 'b': '10010110000', 'c': '10010000110', 'd': '10000101100',
        'e': '10000100110', 'f': '10110010000', 'g': '10110000100', 'h': '10011010000',
        'i': '10011000010', 'j': '10000110100', 'k': '10000110010', 'l': '11000010010',
        'm': '11001010000', 'n': '11110111010', 'o': '11000010100', 'p': '10001111010',
        'q': '10100111100', 'r': '10010111100', 's': '10010011110', 't': '10111100100',
        'u': '10011110100', 'v': '10011110010', 'w': '11110100100', 'x': '11110010100',
        'y': '11110010010', 'z': '11011011110',
    }
    val_map = {
        ' ': 0, '!': 1, '"': 2, '#': 3, '$': 4, '%': 5, '&': 6, "'": 7,
        '(': 8, ')': 9, '*': 10, '+': 11, ',': 12, '-': 13, '.': 14, '/': 15,
        '0': 16, '1': 17, '2': 18, '3': 19, '4': 20, '5': 21, '6': 22, '7': 23,
        '8': 24, '9': 25, ':': 26, ';': 27, '<': 28, '=': 29, '>': 30, '?': 31,
        '@': 32, 'A': 33, 'B': 34, 'C': 35, 'D': 36, 'E': 37, 'F': 38, 'G': 39,
        'H': 40, 'I': 41, 'J': 42, 'K': 43, 'L': 44, 'M': 45, 'N': 46, 'O': 47,
        'P': 48, 'Q': 49, 'R': 50, 'S': 51, 'T': 52, 'U': 53, 'V': 54, 'W': 55,
        'X': 56, 'Y': 57, 'Z': 58, '[': 59, '\\': 60, ']': 61, '^': 62, '_': 63,
        'a': 64, 'b': 65, 'c': 66, 'd': 67, 'e': 68, 'f': 69, 'g': 70, 'h': 71,
        'i': 72, 'j': 73, 'k': 74, 'l': 75, 'm': 76, 'n': 77, 'o': 78, 'p': 79,
        'q': 80, 'r': 81, 's': 82, 't': 83, 'u': 84, 'v': 85, 'w': 86, 'x': 87,
        'y': 88, 'z': 89
    }

    start_b = "11010010000"
    stop_pattern = "1100011101011"

    checksum = 104
    bit_string = start_b
    for idx, char in enumerate(code):
        p = patterns.get(char, patterns[' '])
        bit_string += p
        checksum += val_map.get(char, 0) * (idx + 1)

    check_val = checksum % 103
    rev_map = {v: k for k, v in val_map.items()}
    check_char = rev_map.get(check_val, ' ')
    bit_string += patterns.get(check_char, patterns[' '])
    bit_string += stop_pattern

    bar_width = 1.4
    quiet_width = quiet_modules * bar_width
    rects = []
    x = quiet_width
    for bit in bit_string:
        if bit == '1':
            rects.append(f'<rect x="{x:.1f}" y="0" width="{bar_width:.1f}" height="{height}" fill="#000000" />')
        x += bar_width

    total_width = x + quiet_width
    svg = (
        f'<svg viewBox="0 0 {total_width:.1f} {height}" width="100%" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet" style="display:block;">'
        + "".join(rects)
        + "</svg>"
    )
    return svg


def _format_appointment_datetime(appt_date: date, appt_time: time) -> str:
    dt = datetime.combine(appt_date, appt_time)
    return dt.strftime("%d-%b-%Y %H:%M")


def _short_doctor_label(name: str | None) -> str:
    if not name or not name.strip():
        return "Consultant"
    text = name.strip()
    if len(text) <= 22:
        return text
    return text[:20].rstrip() + "…"


def render_patient_label_html(
    patient: Patient,
    hospital: Hospital | None,
    encounter_id: str | None = None,
    format_type: str = "standard",  # standard (50x25mm), wristband (250x25mm)
    auto_print: bool = True,
    appointment: LabelAppointmentContext | None = None,
) -> str:
    """Render patient identification label HTML adhering to Clinical Calm and standard thermal printer sizes."""
    header_code, _display_name = resolve_facility_branding(hospital)

    p_name = _esc(patient.name or f"{patient.first_name} {patient.last_name}".strip())
    p_uhid = _esc(patient.uhid)
    sex_code = normalize_gender_short(patient.gender)
    p_age = f"{patient.age}Y" if patient.age is not None else "—"
    p_dob = patient.date_of_birth.strftime("%d-%b-%Y") if patient.date_of_birth else "—"
    p_blood = _esc(patient.blood_group or "—")

    created_dt = (
        patient.created_at.strftime("%d-%b-%Y %H:%M")
        if hasattr(patient, "created_at") and patient.created_at
        else datetime.now().strftime("%d-%b-%Y %H:%M")
    )
    barcode_svg = generate_code128_svg(patient.uhid or "UNKNOWN", height=26, quiet_modules=10)

    print_script = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""

    enc_for_wrist = encounter_id or (appointment.op_id if appointment else None)

    if format_type == "wristband":
        hosp_title = _esc(header_code)
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Wristband - {p_uhid}</title>
<style>
  @page {{ size: 250mm 25mm; margin: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #fff; color: #0f172a; width: 250mm; height: 25mm; display: flex; align-items: center; padding: 2mm 5mm; }}
  .band {{ display: flex; align-items: center; justify-content: space-between; width: 100%; border-right: 1px dashed #cbd5e1; padding-right: 10mm; }}
  .hosp {{ font-size: 8pt; font-weight: 700; text-transform: uppercase; color: #0284c7; letter-spacing: 0.5px; }}
  .name {{ font-size: 11pt; font-weight: 800; color: #0f172a; line-height: 1.1; margin: 1mm 0; }}
  .meta {{ font-size: 8pt; font-weight: 600; color: #334155; }}
  .barcode-wrap {{ width: 45mm; text-align: center; }}
  .uhid-txt {{ font-size: 8.5pt; font-family: monospace; font-weight: 700; letter-spacing: 1px; margin-top: 1mm; }}
  @media print {{ body {{ -webkit-print-color-adjust: exact; }} }}
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
    <div style="font-size: 7pt; color: #64748b; text-align: right;">
      {f'<div>ENC: {_esc(enc_for_wrist)}</div>' if enc_for_wrist else ''}
      <div>REG: {created_dt}</div>
    </div>
  </div>
  {print_script}
</body></html>"""

    appt_column = ""
    header_right = f'<span class="reg-date">REG {created_dt}</span>'
    if appointment:
        appt_dt = _format_appointment_datetime(appointment.appointment_date, appointment.appointment_time)
        doctor_line = _esc(_short_doctor_label(appointment.doctor_name))
        dept_bit = (
            f' · {_esc(appointment.department_name)}'
            if appointment.department_name and appointment.department_name.strip()
            else ""
        )
        header_right = f'<span class="reg-date">{_esc(appointment.op_id)}</span>'
        appt_column = f"""
    <div class="col-appt">
      <div class="appt-label">APPOINTMENT</div>
      <div class="appt-line">{_esc(appt_dt)}</div>
      <div class="appt-line appt-doctor">{doctor_line}{dept_bit}</div>
    </div>"""
    elif encounter_id:
        header_right = f'<span class="reg-date">ENC {_esc(encounter_id)}</span>'

    # Standard Patient Label / Sticker (50mm x 25mm landscape — width × height)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=50mm, initial-scale=1"/>
<title>Label - {p_uhid}</title>
<style>
  @page {{
    size: 50mm 25mm;
    margin: 0;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html {{
    width: 50mm;
    height: 25mm;
    background: #ffffff;
  }}
  body {{
    font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    background: #ffffff;
    color: #0f172a;
    width: 50mm;
    height: 25mm;
    padding: 1.1mm 1.6mm;
    overflow: hidden;
  }}
  .label-container {{
    display: flex;
    flex-direction: column;
    height: 100%;
    max-height: 100%;
    gap: 0.25mm;
    overflow: hidden;
  }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 0.5px solid #0f172a;
    padding-bottom: 0.25mm;
    flex-shrink: 0;
  }}
  .hosp-title {{
    font-size: 5.5pt;
    font-weight: 800;
    text-transform: uppercase;
    color: #0369a1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 26mm;
    letter-spacing: 0.2px;
  }}
  .reg-date {{
    font-size: 4.8pt;
    font-weight: 600;
    color: #475569;
    white-space: nowrap;
    text-align: right;
    max-width: 22mm;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .main-row {{
    display: flex;
    flex: 1;
    min-height: 0;
    gap: 0.8mm;
    align-items: stretch;
  }}
  .col-identity {{
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 0.15mm;
  }}
  .col-appt {{
    flex: 1;
    min-width: 0;
    border-left: 0.4px solid #0369a1;
    padding-left: 0.5mm;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 0.1mm;
    line-height: 1.1;
  }}
  .patient-name {{
    font-size: 7.6pt;
    font-weight: 800;
    color: #0f172a;
    line-height: 1.05;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    word-break: break-word;
  }}
  .demographics {{
    font-size: 5.6pt;
    font-weight: 600;
    color: #334155;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .appt-label {{
    font-size: 4pt;
    font-weight: 800;
    letter-spacing: 0.35px;
    color: #0369a1;
    text-transform: uppercase;
  }}
  .appt-line {{
    font-size: 5pt;
    font-weight: 700;
    color: #0f172a;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .appt-doctor {{
    font-weight: 600;
    color: #334155;
    font-size: 4.8pt;
  }}
  .barcode-section {{
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 0.8mm;
    flex-shrink: 0;
    min-height: 7.5mm;
  }}
  .barcode-box {{
    flex: 1;
    min-width: 0;
    height: 7mm;
    max-height: 7mm;
    padding: 0 0.5mm;
    overflow: hidden;
  }}
  .barcode-box svg {{
    display: block;
    width: 100%;
    height: 100%;
    max-height: 7mm;
  }}
  .uhid-display {{
    font-size: 6.5pt;
    font-family: ui-monospace, monospace;
    font-weight: 800;
    letter-spacing: 0.35px;
    white-space: nowrap;
    flex-shrink: 0;
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
      overflow: hidden !important;
      page-break-after: avoid !important;
      page-break-before: avoid !important;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    body {{
      padding: 1.1mm 1.6mm;
    }}
    .label-container {{
      page-break-inside: avoid !important;
      break-inside: avoid !important;
    }}
  }}
</style></head><body>
  <div class="label-container">
    <div class="header">
      <span class="hosp-title">{_esc(header_code)}</span>
      {header_right}
    </div>
    <div class="main-row">
      <div class="col-identity">
        <div class="patient-name">{p_name}</div>
        <div class="demographics">
          <span>Age/Sex: {p_age}/{sex_code}</span> · <span>BG: {p_blood}</span>
        </div>
      </div>{appt_column}
    </div>
    <div class="barcode-section">
      <div class="barcode-box">{barcode_svg}</div>
      <div class="uhid-display">{p_uhid}</div>
    </div>
  </div>
  {print_script}
</body></html>"""
