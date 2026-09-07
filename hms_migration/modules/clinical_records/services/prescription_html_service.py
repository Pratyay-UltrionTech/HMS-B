"""
HTML prescription document renderer.

Conforms to UltrionTech-Backend-Template modules/clinical_records/services/ specification.
Generates print-optimized HTML layout for prescriptions.
Kept strictly under 500 lines.
"""

from __future__ import annotations

from html import escape as html_escape

from hms_migration.modules.clinical_records.entities.clinical_record import Prescription


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
        hosp_phone = hospital_info.get("phone") or "—"
        hosp_email = hospital_info.get("email") or "—"
        hosp_address = hospital_info.get("address") or "—"

        body = (rx.medicines or "").strip()
        if not body or body == "—":
            parts = [p for p in [rx.symptoms, rx.dosage, rx.advice] if p and str(p).strip() and str(p).strip() != "—"]
            body = "\n\n".join(parts) if parts else "—"

        body_lower = body.lower()
        extra_lines: list[str] = []
        if lab_names and "laboratory tests prescribed" not in body_lower and "lab tests prescribed" not in body_lower:
            extra_lines.append(f"Laboratory tests prescribed: {', '.join(lab_names)}")
        if rad_names and "radiology prescribed" not in body_lower:
            extra_lines.append(f"Radiology prescribed: {', '.join(rad_names)}")
        if extra_lines:
            body = (body if body != "—" else "") + ("\n\n" if body and body != "—" else "") + "\n".join(extra_lines)
            if not body.strip():
                body = "—"

        vitals_block = ""
        if vitals:
            rows = "".join(
                f"<tr><td>{html_escape(name)}</td><td><strong>{html_escape(result)}</strong></td></tr>"
                for name, result in vitals
                if name and result
            )
            if rows:
                vitals_block = f"""
      <div class="vitals">
        <p class="vitals-title">Vitals</p>
        <table>
          <thead><tr><th>Name</th><th>Result</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>"""

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Prescription</title>
<style>
  * {{ box-sizing: border-box; }}
  @page {{ margin: 12mm; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 0; background: #eef2f7; color: #1e293b; }}
  .pad {{ max-width: 780px; margin: 24px auto; background: #fff; border-left: 10px solid #2563eb; border-right: 10px solid #2563eb; min-height: 920px; display: flex; flex-direction: column; }}
  .header {{ padding: 24px 36px 16px; background: linear-gradient(135deg, #dbeafe 0%, #fff 55%); border-bottom: 1px solid #bfdbfe; display: flex; justify-content: space-between; gap: 16px; }}
  .hosp-name {{ font-size: 28px; font-weight: 800; color: #1d4ed8; margin: 0; }}
  .hosp-meta {{ color: #475569; font-size: 12px; margin-top: 6px; line-height: 1.5; }}
  .caduceus {{ width: 64px; height: 64px; border-radius: 50%; background: #2563eb; color: #fff; display: flex; align-items: center; justify-content: center; font-size: 28px; font-weight: 700; flex-shrink: 0; }}
  .body {{ padding: 12px 36px 24px; flex: 1; }}
  .line {{ display: flex; align-items: baseline; gap: 8px; margin: 10px 0; font-size: 14px; }}
  .line label {{ font-weight: 600; color: #334155; white-space: nowrap; }}
  .line .fill {{ flex: 1; border-bottom: 1px solid #94a3b8; min-height: 22px; padding: 2px 4px; }}
  .row {{ display: flex; gap: 24px; }}
  .row .line {{ flex: 1; }}
  .vitals {{ margin: 14px 0 8px; padding: 12px 14px; border: 1px solid #fecdd3; border-radius: 12px; background: #fff1f2; }}
  .vitals-title {{ margin: 0 0 8px; font-size: 12px; font-weight: 800; letter-spacing: 0.06em; text-transform: uppercase; color: #be123c; }}
  .vitals table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .vitals th {{ text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: #94a3b8; padding: 0 8px 6px 0; }}
  .vitals td {{ padding: 6px 8px 6px 0; border-top: 1px solid #fecdd3; color: #0f172a; }}
  .rx {{ margin-top: 18px; position: relative; min-height: 320px; padding: 8px 8px 8px 56px; }}
  .rx-mark {{ position: absolute; left: 0; top: 0; font-size: 42px; font-weight: 800; color: #2563eb; font-family: Georgia, serif; }}
  .rx-content {{ white-space: pre-wrap; font-size: 15px; line-height: 1.7; min-height: 280px; }}
  .sign {{ margin-top: 40px; text-align: right; padding-right: 12px; }}
  .sign-line {{ display: inline-block; width: 180px; border-top: 1px solid #64748b; padding-top: 6px; font-size: 11px; letter-spacing: 0.12em; color: #64748b; text-align: center; }}
  .footer {{ margin-top: auto; background: linear-gradient(90deg, #dbeafe, #eff6ff); padding: 16px 36px; border-top: 2px solid #93c5fd; display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }}
  .footer .label {{ font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: #94a3b8; margin: 0 0 4px; }}
  .footer p {{ margin: 0; font-size: 12px; color: #334155; }}
  .footer .strong {{ font-weight: 700; color: #1d4ed8; }}
  @media print {{
    body {{ background: #fff; }}
    .pad {{ margin: 0; max-width: none; min-height: 100vh; page-break-inside: avoid; }}
    .header, .footer {{ position: running(none); }}
  }}
</style></head><body>
  <div class="pad">
    <div class="header">
      <div>
        <p class="hosp-name">{html_escape(hosp_name)}</p>
        <p class="hosp-meta">📍 {html_escape(hosp_address)}<br/>☎ {html_escape(hosp_phone)} &nbsp; ✉ {html_escape(hosp_email)}</p>
      </div>
      <div class="caduceus">⚕</div>
    </div>
    <div class="body">
      <div class="line"><label>Patient Name:</label><div class="fill">{html_escape(patient.name if patient else "—")}</div></div>
      <div class="row">
        <div class="line"><label>Age:</label><div class="fill">{patient.age if patient and patient.age is not None else "—"}</div></div>
        <div class="line"><label>Date:</label><div class="fill">{html_escape(created)}</div></div>
      </div>
      <div class="line"><label>Diagnosis:</label><div class="fill">{html_escape(rx.diagnosis)}</div></div>
      {vitals_block}
      <div class="rx">
        <div class="rx-mark">℞</div>
        <div class="rx-content">{html_escape(body)}</div>
      </div>
      <div class="line"><label>Follow-up:</label><div class="fill">{html_escape(follow)}</div></div>
      <div class="sign">
        {f'<img src="{rx.signature_data}" alt="Signature" style="max-height:64px;max-width:200px;display:block;margin-left:auto;margin-bottom:4px"/>' if getattr(rx, "signature_data", None) else ""}
        <div class="sign-line">SIGNATURE</div>
      </div>
    </div>
    <div class="footer">
      <div>
        <p class="label">Phone</p>
        <p>☎ {html_escape(hosp_phone)}</p>
      </div>
      <div>
        <p class="label">Doctor</p>
        <p class="strong">{html_escape(doctor_name)}</p>
        <p>{html_escape(qualification)}</p>
        {f"<p>Reg. No: {html_escape(registration_no)}</p>" if registration_no else ""}
      </div>
      <div>
        <p class="label">Hospital</p>
        <p class="strong">{html_escape(hosp_name)}</p>
        <p>{html_escape(hosp_address)}</p>
      </div>
    </div>
  </div>
  <script>window.onload = function() {{ window.print(); }}</script>
</body></html>"""
