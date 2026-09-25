"""
Laboratory Specimen Tube Label rendering service.

Renders high-contrast, pure monochrome 50×25mm thermal sticker labels for physical
laboratory specimen tubes and containers.

Specimen Label Requirements:
- Pure monochrome (#000000 on #ffffff) with zero gray/color dithering
- 50mm × 25mm dimensions (203 DPI thermal printer standard)
- Barcode identifies the physical SPECIMEN (specimen_no: S24092600001), NOT just the lab order or patient UHID
- Patient identity clearly legible (Name, UHID, Age/Gender)
- Specimen & container type clearly identified (e.g. EDTA Blood, Serum SST, Urine)
- Associated test abbreviations
- Collection timestamp and accession barcode with quiet zones
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from shared.barcodes.code128 import generate_code128_svg


def render_specimen_label_html(
    *,
    specimen_no: str,
    order_no: str = "",
    sample_type: str,
    container_type: str,
    patient_name: str,
    patient_uhid: str,
    patient_age: int | str | None = None,
    patient_gender: str | None = None,
    test_names: list[str] | None = None,
    collected_at: datetime | None = None,
    hospital_name: str = "ULTRION HOSPITAL",
) -> str:
    """
    Renders pure monochrome 50×25 mm thermal sticker HTML for a laboratory specimen container.
    """
    safe_hospital = html.escape((hospital_name or "ULTRION HOSPITAL").strip().upper())
    safe_name = html.escape((patient_name or "UNKNOWN PATIENT").strip().upper())
    safe_uhid = html.escape((patient_uhid or "NO-UHID").strip().upper())
    safe_specimen_no = html.escape((specimen_no or "").strip().upper())
    safe_order_no = html.escape((order_no or "").strip().upper())

    # Format Demographics
    demo_parts = []
    if patient_age is not None and str(patient_age).strip():
        demo_parts.append(f"{patient_age}Y")
    if patient_gender:
        g = patient_gender.strip().upper()
        demo_parts.append(g[0] if g else "")
    safe_demo = html.escape(" / ".join(filter(None, demo_parts)))

    # Specimen & Container details
    short_container = container_type.split("(")[0].strip().upper()
    safe_container = html.escape(short_container or sample_type.upper())

    # Test summary (shortened for 50x25mm space)
    tests = test_names or []
    if len(tests) > 3:
        test_summary = ", ".join(tests[:3]) + f" (+{len(tests)-3})"
    else:
        test_summary = ", ".join(tests) if tests else "ROUTINE"
    safe_tests = html.escape(test_summary.upper())

    # Collection timestamp
    dt = collected_at or datetime.now(timezone.utc)
    dt_str = dt.strftime("%d/%b %H:%M").upper()

    # Generate barcode for SPECIMEN NO
    barcode_svg = generate_code128_svg(
        code=specimen_no,
        height=32,
        quiet_modules=6,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Specimen Label - {safe_specimen_no}</title>
  <style>
    @page {{
      size: 50mm 25mm;
      margin: 0;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    body {{
      width: 50mm;
      height: 25mm;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      color: #000000;
      background: #ffffff;
      padding: 1.0mm 1.5mm;
    }}
    .label-container {{
      width: 100%;
      height: 100%;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .header-row {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      border-bottom: 0.5pt solid #000000;
      padding-bottom: 0.3mm;
    }}
    .patient-name {{
      font-size: 7pt;
      font-weight: 800;
      line-height: 1.0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 33mm;
      letter-spacing: -0.2px;
    }}
    .uhid-tag {{
      font-size: 6.5pt;
      font-weight: 800;
      font-family: monospace;
      line-height: 1.0;
    }}
    .meta-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 0.3mm;
      font-size: 5.5pt;
      line-height: 1.0;
      font-weight: 700;
    }}
    .container-badge {{
      font-weight: 800;
    }}
    .tests-row {{
      font-size: 5pt;
      font-weight: 600;
      line-height: 1.0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-top: 0.2mm;
    }}
    .barcode-section {{
      display: flex;
      flex-direction: column;
      align-items: center;
      margin-top: 0.2mm;
    }}
    .barcode-svg {{
      width: 100%;
      max-width: 44mm;
      height: 6.5mm;
      display: block;
    }}
    .barcode-caption {{
      display: flex;
      justify-content: space-between;
      width: 100%;
      font-family: monospace;
      font-size: 5.5pt;
      font-weight: 800;
      line-height: 1.0;
      margin-top: 0.2mm;
    }}
  </style>
</head>
<body>
  <div class="label-container">
    <div class="header-row">
      <span class="patient-name">{safe_name}</span>
      <span class="uhid-tag">{safe_uhid}</span>
    </div>

    <div class="meta-row">
      <span class="container-badge">{safe_container}</span>
      <span>{safe_demo}</span>
      <span>{dt_str}</span>
    </div>

    <div class="tests-row">
      <span>TESTS: {safe_tests}</span>
    </div>

    <div class="barcode-section">
      <div class="barcode-svg">{barcode_svg}</div>
      <div class="barcode-caption">
        <span>SPEC: {safe_specimen_no}</span>
        <span>ORD: {safe_order_no}</span>
        <span>{safe_hospital}</span>
      </div>
    </div>
  </div>
</body>
</html>
"""
