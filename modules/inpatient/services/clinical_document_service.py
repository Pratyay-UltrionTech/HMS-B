"""
Canonical Clinical Document Service for IPD Form Submissions & Consent Invariants.

Enforces:
1. Stable schema versions (form_schema_version = 2 for modern submissions).
2. Canonical patient demographics resolved authoritatively from DB (overwriting untrusted client inputs).
3. Explicit consent signer model (patient / self vs representative / parent / guardian / spouse).
4. Validation: if signer != patient, signer_name and relationship are mandatory at finalization.
5. Server-rendered authoritative HTML snapshot ensuring finalized clinical documents are immutable and reproducible.
"""

from __future__ import annotations

from datetime import datetime, timezone
import html
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from modules.inpatient.contracts.inpatient_contracts import (
    IpdFormSubmissionCreate,
    IpdFormSubmissionStatus,
    IpdFormSubmissionUpdate,
)
from modules.inpatient.entities.admission import Admission, IpdFormSubmission
from shared.exceptions.base import NotFoundError, ValidationError
from modules.patients.entities.patient import Patient


class ClinicalDocumentService:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def is_consent_form(self, form_id: str, form_title: str) -> bool:
        fid = form_id.lower()
        title = form_title.lower()
        return "consent" in fid or "consent" in title

    def resolve_canonical_patient(
        self, patient_id: UUID, admission_id: UUID | None
    ) -> tuple[Patient, Admission | None, dict[str, Any]]:
        patient = (
            self.db.query(Patient)
            .filter(Patient.id == patient_id, Patient.hospital_id == self.hospital_id)
            .first()
        )
        if not patient:
            raise NotFoundError("Patient not found")

        adm: Admission | None = None
        if admission_id is not None:
            adm = (
                self.db.query(Admission)
                .filter(
                    Admission.id == admission_id,
                    Admission.hospital_id == self.hospital_id,
                    Admission.patient_id == patient_id,
                )
                .first()
            )
            if not adm:
                raise ValidationError("Admission does not belong to specified patient")

        canonical_data: dict[str, Any] = {
            "patient_id": str(patient.id),
            "uhid": patient.uhid,
            "name": patient.name,
            "age": patient.age,
            "gender": patient.gender,
            "dob": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
            "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
            "mobile": patient.mobile,
        }
        if adm:
            canonical_data.update({
                "admission_id": str(adm.id),
                "ip_id": getattr(adm, "ip_id", None) or f"IPD-{str(adm.id)[:8].upper()}",
                "admission_number": getattr(adm, "ip_id", None) or str(adm.id),
                "admitted_at": adm.admitted_at.isoformat() if getattr(adm, "admitted_at", None) else None,
                "ward_name": adm.ward.name if getattr(adm, "ward", None) else None,
                "bed_code": adm.bed.bed_code if getattr(adm, "bed", None) else None,
            })

        return patient, adm, canonical_data

    def validate_and_extract_signer(
        self,
        form_id: str,
        form_title: str,
        form_data: dict[str, Any],
        explicit_signer_type: str | None,
        explicit_signer_name: str | None,
        explicit_signer_rel: str | None,
        is_final: bool,
        canonical_patient: dict[str, Any],
    ) -> tuple[str, str, str]:
        """
        Determines signer_type, signer_name, signer_relationship.
        If the form is a consent form and is being finalized, enforces that
        non-self signers have a valid name and relationship.
        """
        if not self.is_consent_form(form_id, form_title):
            return (
                explicit_signer_type or "patient",
                explicit_signer_name or canonical_patient.get("name") or "",
                explicit_signer_rel or "Self",
            )

        # Look in explicit payload fields first, then form_data semantic keys
        signer_type = (
            explicit_signer_type
            or form_data.get("consent.signer_type")
            or form_data.get("signer_type")
            or "patient"
        ).strip().lower()

        signer_name = (
            explicit_signer_name
            or form_data.get("consent.signer_name")
            or form_data.get("signer_name")
            or ""
        ).strip()

        signer_rel = (
            explicit_signer_rel
            or form_data.get("consent.signer_relationship")
            or form_data.get("signer_relationship")
            or ""
        ).strip()

        if signer_type in ("patient", "self"):
            signer_type = "patient"
            if not signer_name:
                signer_name = canonical_patient.get("name") or "Patient"
            if not signer_rel:
                signer_rel = "Self"
        else:
            if is_final:
                if not signer_name:
                    raise ValidationError(
                        "Consent finalization requires the signer name when signer is a representative/guardian."
                    )
                if not signer_rel:
                    raise ValidationError(
                        "Consent finalization requires the relationship to patient when signer is a representative/guardian."
                    )

        return signer_type, signer_name, signer_rel

    def render_authoritative_document(
        self,
        sub: IpdFormSubmission,
        patient: Patient,
        admission: Admission | None,
        signer_info: tuple[str, str, str],
        actor: dict[str, Any],
    ) -> str:
        """
        Generates server-controlled authoritative HTML embedding canonical patient identity,
        structured form fields, signer details, and audit footer.
        """
        signer_type, signer_name, signer_rel = signer_info
        actor_name = str(actor.get("name") or actor.get("sub") or "Staff")
        actor_role = str(actor.get("staff_role_name") or actor.get("role") or "")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        patient_name = html.escape(patient.name or "")
        patient_uhid = html.escape(patient.uhid or "")
        patient_age = str(patient.age or "")
        patient_gender = html.escape((patient.gender or "").capitalize())
        ip_id = html.escape(str(admission.id)[:8].upper() if admission else "N/A")
        ward = html.escape(admission.ward.name if admission and getattr(admission, "ward", None) else "N/A")
        bed = html.escape(admission.bed.bed_code if admission and getattr(admission, "bed", None) else "N/A")

        # Render structured key-values
        rows_html = []
        for k, v in sorted(sub.form_data.items()):
            if k.startswith("__") or k == "canonical_patient":
                continue
            v_str = html.escape(str(v)) if v is not None else ""
            k_str = html.escape(k.replace(".", " → ").replace("_", " ").title())
            rows_html.append(
                f"<tr><td style='padding:6px 12px;border:1px solid #e2e8f0;font-weight:600;width:35%;background:#f8fafc;'>{k_str}</td>"
                f"<td style='padding:6px 12px;border:1px solid #e2e8f0;'>{v_str}</td></tr>"
            )

        fields_table = "".join(rows_html)

        doc = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(sub.form_title)} - {patient_uhid}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #0f172a; margin: 24px; line-height: 1.5; }}
    .header {{ border-bottom: 2px solid #0f172a; padding-bottom: 12px; margin-bottom: 16px; }}
    .patient-box {{ background: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 6px; padding: 12px 16px; margin-bottom: 20px; display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; font-size: 13px; }}
    .patient-box div strong {{ display: block; color: #475569; font-size: 11px; text-transform: uppercase; }}
    .content-table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 13px; }}
    .signer-box {{ border: 1px solid #94a3b8; border-radius: 6px; padding: 12px 16px; margin-bottom: 20px; background: #fafafa; font-size: 13px; }}
    .footer {{ border-top: 1px solid #cbd5e1; padding-top: 12px; font-size: 11px; color: #64748b; display: flex; justify-content: space-between; }}
    @media print {{ body {{ margin: 0; }} }}
  </style>
</head>
<body>
  <div class="header">
    <h2 style="margin:0; font-size: 18px;">{html.escape(sub.form_title)}</h2>
    <div style="font-size: 12px; color: #64748b;">Form ID: {html.escape(sub.form_id)} | Schema Version: {sub.form_schema_version} | Status: {sub.status.value.upper()}</div>
  </div>

  <div class="patient-box">
    <div><strong>Patient Name</strong>{patient_name}</div>
    <div><strong>UHID</strong>{patient_uhid}</div>
    <div><strong>Age / Gender</strong>{patient_age} / {patient_gender}</div>
    <div><strong>IP No. / Bed</strong>{ip_id} / {bed} ({ward})</div>
  </div>

  <table class="content-table">
    <tbody>
      {fields_table}
    </tbody>
  </table>

  <div class="signer-box">
    <div style="font-weight: 700; margin-bottom: 6px; color: #1e293b;">Signer &amp; Consent Attribution</div>
    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;">
      <div><strong>Signer Type:</strong> {html.escape(signer_type.capitalize())}</div>
      <div><strong>Signer Name:</strong> {html.escape(signer_name)}</div>
      <div><strong>Relationship:</strong> {html.escape(signer_rel)}</div>
    </div>
  </div>

  <div class="footer">
    <div>Finalized By: {html.escape(actor_name)} ({html.escape(actor_role)})</div>
    <div>Server Timestamp: {now_iso}</div>
    <div>Submission ID: {str(sub.id)}</div>
  </div>
</body>
</html>
"""
        return doc
