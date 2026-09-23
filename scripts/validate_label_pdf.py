"""Generate label HTML and Chrome headless PDF; report page dimensions (mm)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from datetime import date, time
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.patients.entities.patient import Patient, PatientStatus
from modules.patients.services.patient_label_service import (
    LabelAppointmentContext,
    render_patient_label_html,
)
from modules.tenancy.entities.hospital import Hospital

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def pt_to_mm(pt: float) -> float:
    return pt * 25.4 / 72.0


def sample_html(*, appointment: bool) -> str:
    h = Hospital(
        id=uuid.uuid4(),
        hospital_id="SHC",
        name="SHC Hospital",
        address="a",
        phone="1",
        email="t@t.com",
        password_hash="x",
    )
    h.facility_settings = {"institutional_code": "SHC"}
    p = Patient(
        hospital_id=h.id,
        uhid="P0098",
        first_name="Shoba",
        last_name="rani",
        name="Shoba rani",
        mobile="9999999999",
        gender="Female",
        age=38,
        blood_group=None,
        status=PatientStatus.active,
    )
    appt = None
    if appointment:
        appt = LabelAppointmentContext(
            op_id="OP-2026-00098",
            appointment_date=date(2026, 9, 22),
            appointment_time=time(16, 41),
            doctor_name="Dr. Test",
            department_name="Cardiology",
        )
    return render_patient_label_html(p, h, appointment=appt, auto_print=False)


def print_to_pdf(html: str, pdf_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "label.html"
        html_path.write_text(html, encoding="utf-8")
        if not CHROME.is_file():
            raise SystemExit(f"Chrome not found at {CHROME}")
        cmd = [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"Chrome failed: {proc.stderr or proc.stdout}")


def report_pdf(pdf_path: Path) -> tuple[bool, str]:
    reader = PdfReader(str(pdf_path))
    lines: list[str] = []
    ok = True
    if len(reader.pages) != 1:
        ok = False
        lines.append(f"FAIL: expected 1 page, got {len(reader.pages)}")
    for i, page in enumerate(reader.pages):
        w_mm = pt_to_mm(float(page.mediabox.width))
        h_mm = pt_to_mm(float(page.mediabox.height))
        rot = int(page.get("/Rotate", 0) or 0)
        lines.append(f"  page {i + 1}: {w_mm:.2f}mm x {h_mm:.2f}mm  rotation={rot}deg")
        if not (49.0 <= w_mm <= 51.5 and 24.0 <= h_mm <= 26.5):
            ok = False
            lines.append("  FAIL: page size outside 50x25mm tolerance")
        if w_mm <= h_mm:
            ok = False
            lines.append("  FAIL: width must exceed height (landscape horizontal label)")
        if rot not in (0, 180):
            ok = False
            lines.append(f"  FAIL: unexpected page rotation {rot}")
        text = (page.extract_text() or "").strip()
        if i == 0 and "APPOINTMENT" not in text and "P0098" not in text:
            ok = False
            lines.append("  FAIL: expected label content on page 1")
        if i > 0 and text:
            ok = False
            lines.append("  FAIL: trailing page must be blank")
    return ok, "\n".join(lines)


def main() -> None:
    out_dir = ROOT.parent / "docs" / "hms-appointment" / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / "label-p0098-validation.pdf"
    html = sample_html(appointment=True)
    print_to_pdf(html, pdf_path)
    print(f"PDF written: {pdf_path}")
    print("Dimensions:")
    ok, detail = report_pdf(pdf_path)
    print(detail)
    print("RESULT:", "PASS" if ok else "FAIL")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
