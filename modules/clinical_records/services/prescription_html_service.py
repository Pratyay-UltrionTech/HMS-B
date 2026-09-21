"""
HTML prescription document renderer.

Conforms to UltrionTech-Backend-Template modules/clinical_records/services/ specification.
Generates print-optimized HTML layout for prescriptions.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from html import escape as html_escape

from modules.clinical_records.entities.clinical_record import Prescription


class PrescriptionHtmlService:
    """Renders printable HTML prescriptions matching the exact HMS-B design."""

    @staticmethod
    def render(
        rx: Prescription,
        hospital_info: dict[str, str] | None = None,
        lab_names: list[str] | None = None,
        rad_names: list[str] | None = None,
        vitals: list[tuple[str, str]] | None = None,
    ) -> str:
        hospital_info = hospital_info or {}
        follow = rx.follow_up_date.strftime("%d %b %Y") if rx.follow_up_date else "—"
        created = rx.created_at.strftime("%d %b %Y") if rx.created_at else ""
        doctor = rx.doctor
        patient = rx.patient
        cv = (doctor.custom_values or {}) if doctor else {}

        def cv_get(*keys: str) -> str:
            for key in keys:
                for ck, val in cv.items():
                    if str(ck).strip().lower().replace(" ", "_") == key.lower().replace(" ", "_") and val not in (None, ""):
                        return str(val)
            return ""

        qualification = (
            (doctor.qualification if doctor and doctor.qualification else None)
            or (doctor.specialization if doctor and doctor.specialization else None)
            or cv_get("qualification", "qualifications", "degree", "specialization")
            or "Physician"
        )
        registration_no = (
            (doctor.medical_registration_number if doctor and doctor.medical_registration_number else None)
            or cv_get("registration_number", "medical_registration_number", "reg_no", "registration")
            or ""
        )
        doctor_name = doctor.name if doctor else "—"
        if doctor_name and not str(doctor_name).lower().startswith("dr"):
            doctor_name = f"Dr. {doctor_name}"

        hosp_name = hospital_info.get("name") or "Hospital"
        hosp_phone = hospital_info.get("phone") or "-"
        hosp_email = hospital_info.get("email") or "-"
        hosp_address = hospital_info.get("address") or "-"

        body = (rx.medicines or "").strip()
        if not body or body == "—":
            parts = [p for p in [rx.symptoms, rx.dosage, rx.advice] if p and str(p).strip() and str(p).strip() != "—"]
            body = "\n\n".join(parts) if parts else "—"

        def _has_content(value: object) -> bool:
            text = str(value or "").strip()
            return bool(text) and text != "—"

        patient_name = patient.name if patient else "—"
        patient_age = f"{patient.age} Years" if (patient and patient.age is not None) else "—"
        patient_gender = patient.gender if (patient and patient.gender) else ""
        age_gender = f"{patient_age} / {patient_gender}" if patient_gender else patient_age
        uhid = patient.uhid if (patient and patient.uhid) else "—"
        patient_id = uhid

        vitals_block = ""
        if vitals and len(vitals) > 0:
            col_count = len(vitals)
            col_pct = round(100.0 / col_count, 2)
            cols = "".join(f'<col style="width: {col_pct}%;" />' for _ in vitals)
            ths = "".join(f"<th>{html_escape(name)}</th>" for name, _ in vitals)
            tds = "".join(f"<td>{html_escape(res)}</td>" for _, res in vitals)
            vitals_block = f"""
      <div class="vitals-section">
        <div class="field-title">Vitals <span class="subtext">(at time of visit)</span></div>
        <table class="vitals-table">
          <colgroup>{cols}</colgroup>
          <thead><tr>{ths}</tr></thead>
          <tbody><tr>{tds}</tr></tbody>
        </table>
      </div>"""

        diagnosis_block = ""
        if _has_content(rx.diagnosis):
            diagnosis_block = f"""
      <div class="field-section">
        <div class="field-title">Clinical Diagnosis</div>
        <div class="field-box">{html_escape(str(rx.diagnosis).strip())}</div>
      </div>"""

        symptoms_block = ""
        if _has_content(rx.symptoms):
            symptoms_block = f"""
      <div class="field-section">
        <div class="field-title">Clinical Notes / Chief Complaints</div>
        <div class="field-box">{html_escape(str(rx.symptoms).strip())}</div>
      </div>"""

        investigations_block = ""
        has_lab = bool(lab_names and len(lab_names) > 0)
        has_rad = bool(rad_names and len(rad_names) > 0)
        if has_lab or has_rad:
            lab_items = "".join(f"<li>{html_escape(item)}</li>" for item in (lab_names or []))
            rad_items = "".join(f"<li>{html_escape(item)}</li>" for item in (rad_names or []))
            investigations_block = f"""
      <div class="tests-grid">
        <div class="test-box">
          <div class="test-title">Laboratory Tests Prescribed</div>
          <div class="test-content">
            {f'<ul class="test-list">{lab_items}</ul>' if has_lab else '<span class="empty-text">None</span>'}
          </div>
        </div>
        <div class="test-box">
          <div class="test-title">Radiology / Imaging Prescribed</div>
          <div class="test-content">
            {f'<ul class="test-list">{rad_items}</ul>' if has_rad else '<span class="empty-text">None</span>'}
          </div>
        </div>
      </div>"""

        advice_block = ""
        if _has_content(rx.advice):
            advice_lines = [line.strip() for line in str(rx.advice).strip().split("\n") if line.strip()]
            advice_content = "".join(f"<li>{html_escape(line.lstrip('•-* '))}</li>" for line in advice_lines)
            advice_block = f"""
      <div class="field-section">
        <div class="field-title">General Medical Advice &amp; Lifestyle Instructions</div>
        <div class="field-box">
          <ul class="advice-list">{advice_content}</ul>
        </div>
      </div>"""

        hosp_address_html = "<br/>".join(html_escape(line) for line in hosp_address.split("\n"))

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Prescription — {html_escape(patient_name)}</title>
<style>
  * {{ box-sizing: border-box; }}
  @page {{ margin: 12mm; size: A4 portrait; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    margin: 0;
    padding: 0;
    background: #f8fafc;
    color: #0f172a;
    font-size: 13px;
    line-height: 1.45;
  }}
  .pad {{
    max-width: 800px;
    margin: 20px auto;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
    padding: 28px 36px 24px;
    min-height: 980px;
    display: flex;
    flex-direction: column;
  }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    padding-bottom: 12px;
    border-bottom: 1.5px solid #0f172a;
  }}
  .hosp-brand {{
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .cross-logo {{
    width: 38px;
    height: 38px;
    position: relative;
    flex-shrink: 0;
  }}
  .cross-v {{
    position: absolute;
    left: 13px;
    top: 0;
    width: 12px;
    height: 38px;
    background: #2563eb;
    border-radius: 2px;
  }}
  .cross-h {{
    position: absolute;
    left: 0;
    top: 13px;
    width: 38px;
    height: 12px;
    background: #2563eb;
    border-radius: 2px;
  }}
  .hosp-name {{
    font-size: 22px;
    font-weight: 800;
    color: #0f172a;
    margin: 0;
    letter-spacing: -0.01em;
  }}
  .hosp-tagline {{
    font-size: 11px;
    font-weight: 600;
    color: #475569;
    margin: 2px 0 0;
    letter-spacing: 0.04em;
  }}
  .hosp-contact {{
    text-align: right;
    font-size: 11px;
    color: #334155;
    line-height: 1.45;
  }}
  .title-strip {{
    text-align: center;
    padding: 10px 0 4px;
  }}
  .title-text {{
    font-size: 17px;
    font-weight: 800;
    letter-spacing: 0.12em;
    color: #0f172a;
    margin: 0;
  }}
  .patient-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    column-gap: 32px;
    row-gap: 4px;
    padding: 10px 0 12px;
    border-bottom: 1px solid #cbd5e1;
    font-size: 12.5px;
  }}
  .meta-row {{
    display: flex;
    align-items: baseline;
    padding: 1.5px 0;
  }}
  .meta-label {{
    width: 95px;
    font-weight: 700;
    color: #0f172a;
    flex-shrink: 0;
  }}
  .meta-sep {{
    width: 14px;
    color: #0f172a;
    font-weight: 700;
  }}
  .meta-val {{
    flex: 1;
    color: #1e293b;
  }}
  .field-section {{
    margin-top: 14px;
  }}
  .field-title {{
    font-size: 12.5px;
    font-weight: 700;
    color: #0f172a;
    margin-bottom: 5px;
  }}
  .field-title .subtext {{
    font-weight: normal;
    color: #64748b;
    font-size: 11.5px;
  }}
  .field-box {{
    border: 1px solid #cbd5e1;
    border-radius: 2px;
    padding: 8px 12px;
    background: #ffffff;
    font-size: 12.5px;
    color: #0f172a;
    min-height: 32px;
    white-space: pre-wrap;
    line-height: 1.5;
  }}
  .vitals-section {{
    margin-top: 14px;
    width: 100%;
    overflow: hidden;
  }}
  .vitals-table {{
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    border: 1px solid #cbd5e1;
    text-align: center;
    font-size: 11.5px;
  }}
  .vitals-table th {{
    background: #f1f5f9;
    color: #0f172a;
    font-weight: 700;
    padding: 6px 6px;
    border: 1px solid #cbd5e1;
    overflow-wrap: break-word;
    word-break: break-word;
    line-height: 1.3;
  }}
  .vitals-table td {{
    padding: 6px 6px;
    border: 1px solid #cbd5e1;
    color: #0f172a;
    font-weight: 600;
    overflow-wrap: break-word;
    word-break: break-word;
    line-height: 1.3;
  }}
  .rx-section {{
    margin-top: 16px;
  }}
  .rx-header {{
    display: flex;
    align-items: baseline;
    gap: 6px;
    margin-bottom: 6px;
  }}
  .rx-sym {{
    font-family: Georgia, serif;
    font-size: 24px;
    font-weight: 700;
    color: #0f172a;
    line-height: 1;
  }}
  .rx-heading {{
    font-size: 13px;
    font-weight: 700;
    color: #0f172a;
  }}
  .rx-box {{
    border: 1px solid #cbd5e1;
    border-radius: 2px;
    padding: 10px 14px;
    background: #ffffff;
    min-height: 130px;
    white-space: pre-wrap;
    font-size: 13px;
    line-height: 1.6;
    color: #0f172a;
    font-family: inherit;
  }}
  .tests-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    margin-top: 14px;
  }}
  .test-box {{
    border: 1px solid #cbd5e1;
    border-radius: 2px;
    padding: 8px 12px;
    min-height: 70px;
  }}
  .test-title {{
    font-size: 12px;
    font-weight: 700;
    color: #0f172a;
    margin-bottom: 4px;
  }}
  .test-list, .advice-list {{
    margin: 4px 0 0 18px;
    padding: 0;
    font-size: 12px;
    color: #0f172a;
    line-height: 1.5;
  }}
  .empty-text {{
    font-size: 11.5px;
    color: #94a3b8;
    font-style: italic;
  }}
  .closing-row {{
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    margin-top: 24px;
    padding-bottom: 12px;
  }}
  .follow-up-info {{
    font-size: 13px;
    font-weight: 700;
    color: #0f172a;
  }}
  .follow-up-info span {{
    font-weight: 600;
    margin-left: 8px;
  }}
  .signature-block {{
    text-align: center;
    width: 210px;
  }}
  .signature-img {{
    max-height: 52px;
    max-width: 170px;
    display: block;
    margin: 0 auto 4px;
  }}
  .signature-line {{
    border-top: 1.5px solid #0f172a;
    padding-top: 4px;
  }}
  .doc-name {{
    font-size: 13px;
    font-weight: 800;
    color: #0f172a;
    margin: 0;
  }}
  .doc-qual {{
    font-size: 11px;
    color: #334155;
    margin: 2px 0 0;
  }}
  .doc-reg {{
    font-size: 11px;
    color: #475569;
    margin: 2px 0 0;
  }}
  .footer {{
    margin-top: auto;
    padding-top: 12px;
    border-top: 1px solid #cbd5e1;
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: #475569;
  }}
  @media print {{
    body {{ background: #fff; }}
    .pad {{
      margin: 0;
      max-width: none;
      border: none;
      box-shadow: none;
      padding: 0;
      min-height: auto;
    }}
  }}
</style></head><body>
  <div class="pad">
    <!-- 1. Hospital Header -->
    <div class="header">
      <div class="hosp-brand">
        <div class="cross-logo">
          <div class="cross-v"></div>
          <div class="cross-h"></div>
        </div>
        <div class="hosp-info">
          <h1 class="hosp-name">{html_escape(hosp_name)}</h1>
          <p class="hosp-tagline">Compassion &nbsp;|&nbsp; Care &nbsp;|&nbsp; Better Health</p>
        </div>
      </div>
      <div class="hosp-contact">
        {hosp_address_html}<br/>
        Phone: {html_escape(hosp_phone)}<br/>
        Email: {html_escape(hosp_email)}
      </div>
    </div>

    <!-- 2. Prescription Title -->
    <div class="title-strip">
      <h2 class="title-text">PRESCRIPTION</h2>
    </div>

    <!-- 3. Patient / Visit Information -->
    <div class="patient-grid">
      <div>
        <div class="meta-row">
          <span class="meta-label">Patient Name</span>
          <span class="meta-sep">:</span>
          <span class="meta-val"><strong>{html_escape(patient_name)}</strong></span>
        </div>
        <div class="meta-row">
          <span class="meta-label">Age / Gender</span>
          <span class="meta-sep">:</span>
          <span class="meta-val">{html_escape(age_gender)}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">Patient ID</span>
          <span class="meta-sep">:</span>
          <span class="meta-val">{html_escape(patient_id)}</span>
        </div>
      </div>
      <div>
        <div class="meta-row">
          <span class="meta-label">Date</span>
          <span class="meta-sep">:</span>
          <span class="meta-val">{html_escape(created)}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">UHID</span>
          <span class="meta-sep">:</span>
          <span class="meta-val">{html_escape(uhid)}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">Visit Type</span>
          <span class="meta-sep">:</span>
          <span class="meta-val">OPD</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">Department</span>
          <span class="meta-sep">:</span>
          <span class="meta-val">{html_escape(qualification)}</span>
        </div>
      </div>
    </div>

    <!-- 4. Clinical Diagnosis -->
    {diagnosis_block}

    <!-- 5. Clinical Notes / Chief Complaints -->
    {symptoms_block}

    <!-- 6. Vitals (at time of visit) -->
    {vitals_block}

    <!-- 7. ℞ Prescribed Medications & Clinical Regimen -->
    <div class="rx-section">
      <div class="rx-header">
        <span class="rx-sym">℞</span>
        <span class="rx-heading">Prescribed Medications &amp; Clinical Regimen</span>
      </div>
      <div class="rx-box">{html_escape(body)}</div>
    </div>

    <!-- 8 & 9. Prescribed Investigations -->
    {investigations_block}

    <!-- 10. General Medical Advice & Lifestyle Instructions -->
    {advice_block}

    <!-- 11 & 12. Follow-up and Doctor Signature -->
    <div class="closing-row">
      <div class="follow-up-info">
        Follow-up Date : <span>{html_escape(follow)}</span>
      </div>
      <div class="signature-block">
        {f'<img src="{rx.signature_data}" alt="Doctor Signature" class="signature-img"/>' if getattr(rx, "signature_data", None) else '<div style="height:36px"></div>'}
        <div class="signature-line">
          <div class="doc-name">{html_escape(doctor_name)}</div>
          <div class="doc-qual">{html_escape(qualification)}</div>
          {f'<div class="doc-reg">Reg. No: {html_escape(registration_no)}</div>' if registration_no else ''}
        </div>
      </div>
    </div>

    <!-- 13. Footer -->
    <div class="footer">
      <div>Thank you for trusting us with your health.</div>
      <div>This is a computer generated prescription.</div>
    </div>
  </div>
  <script>window.onload = function() {{ window.print(); }}</script>
</body></html>"""

