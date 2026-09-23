"""Print label HTML inside an off-screen iframe (mirrors PrintPromptContext) and measure PDF."""

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


def build_label_html() -> str:
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
    appt = LabelAppointmentContext(
        op_id="OP-2026-00098",
        appointment_date=date(2026, 9, 22),
        appointment_time=time(16, 41),
        doctor_name="Dr. Test",
        department_name="Cardiology",
    )
    return render_patient_label_html(p, h, appointment=appt, auto_print=False)


def main() -> None:
    out_dir = ROOT.parent / "docs" / "hms-appointment" / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / "label-p0098-iframe-shell.pdf"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        label_path = tmp_path / "label.html"
        label_path.write_text(build_label_html(), encoding="utf-8")
        shell = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Print shell</title>
<style>
  .print-document-frame {{
    position: fixed; top: 0; left: -10000px; width: 52mm; height: 27mm;
    border: 0; opacity: 0; overflow: hidden;
  }}
</style></head>
<body>
<iframe class="print-document-frame" src="{label_path.as_uri()}" title="print"></iframe>
</body></html>"""
        shell_path = tmp_path / "shell.html"
        shell_path.write_text(shell, encoding="utf-8")
        cmd = [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            shell_path.as_uri(),
        ]
        subprocess.run(cmd, check=True, capture_output=True)

    reader = PdfReader(str(pdf_path))
    print(f"iframe shell PDF: {pdf_path}")
    print(f"pages: {len(reader.pages)}")
    for i, page in enumerate(reader.pages):
        w = pt_to_mm(float(page.mediabox.width))
        h = pt_to_mm(float(page.mediabox.height))
        rot = int(page.get("/Rotate", 0) or 0)
        print(f"  page {i+1}: {w:.2f}mm x {h:.2f}mm rot={rot}")


if __name__ == "__main__":
    main()
