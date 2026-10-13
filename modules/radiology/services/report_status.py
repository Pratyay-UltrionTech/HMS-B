"""Radiology Domain Report Status Evaluation.

Authoritative domain-level helper to determine whether a radiology order
has final diagnostic report evidence (findings, impression, or report file).
"""

from __future__ import annotations

from typing import Any


def is_radiology_report_finalized(
    order: Any = None,
    findings: str | None = None,
    impression: str | None = None,
    report_file_name: str | None = None,
    report_file_data: str | None = None,
) -> bool:
    """Return True if authoritative final report evidence exists for the order."""
    if order is not None:
        f = getattr(order, "findings", None)
        i = getattr(order, "impression", None)
        r_name = getattr(order, "report_file_name", None)
        r_data = getattr(order, "report_file_data", None)
        has_file = getattr(order, "has_report_file", False)
        return bool(
            (f and f.strip())
            or (i and i.strip())
            or (r_name and r_name.strip())
            or (r_data and r_data.strip())
            or has_file
        )

    return bool(
        (findings and findings.strip())
        or (impression and impression.strip())
        or (report_file_name and report_file_name.strip())
        or (report_file_data and report_file_data.strip())
    )
