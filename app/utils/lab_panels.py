"""Resolve lab catalogue tests from individual tests and/or panels."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.models import LabPanelTest, LabSampleType, LabTestCatalog, LabTestPanel


@dataclass
class ResolvedLabTest:
    test: LabTestCatalog
    panel: LabTestPanel | None = None


def resolve_lab_selection(
    db: Session,
    hospital_id: UUID,
    test_ids: list[UUID] | None = None,
    panel_ids: list[UUID] | None = None,
    *,
    require_non_empty: bool = True,
) -> list[ResolvedLabTest]:
    """
    Expand panels into underlying catalogue tests and merge with direct test_ids.

    - Individual tests come first (no panel provenance).
    - Panel members follow; if a test already appears from an individual pick or earlier panel,
      the first occurrence wins (dedupe by test_id).
    """
    test_ids = list(test_ids or [])
    panel_ids = list(panel_ids or [])
    if require_non_empty and not test_ids and not panel_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one test or panel",
        )

    ordered: list[ResolvedLabTest] = []
    seen: set[UUID] = set()

    if test_ids:
        unique_ids = list(dict.fromkeys(test_ids))
        tests = (
            db.query(LabTestCatalog)
            .filter(
                LabTestCatalog.hospital_id == hospital_id,
                LabTestCatalog.id.in_(unique_ids),
                LabTestCatalog.is_active.is_(True),
            )
            .all()
        )
        by_id = {t.id: t for t in tests}
        if len(by_id) != len(unique_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more tests are invalid or inactive",
            )
        for tid in unique_ids:
            t = by_id[tid]
            if tid not in seen:
                seen.add(tid)
                ordered.append(ResolvedLabTest(test=t, panel=None))

    if panel_ids:
        unique_panels = list(dict.fromkeys(panel_ids))
        panels = (
            db.query(LabTestPanel)
            .options(joinedload(LabTestPanel.tests).joinedload(LabPanelTest.test))
            .filter(
                LabTestPanel.hospital_id == hospital_id,
                LabTestPanel.id.in_(unique_panels),
                LabTestPanel.is_active.is_(True),
            )
            .all()
        )
        by_panel = {p.id: p for p in panels}
        if len(by_panel) != len(unique_panels):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more panels are invalid or inactive",
            )
        for pid in unique_panels:
            panel = by_panel[pid]
            members = sorted(panel.tests or [], key=lambda m: (m.sort_order, str(m.id)))
            if not members:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Panel '{panel.panel_name}' has no tests configured",
                )
            for member in members:
                test = member.test
                if not test or test.hospital_id != hospital_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Panel '{panel.panel_name}' references an invalid test",
                    )
                if not test.is_active:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Panel '{panel.panel_name}' includes inactive test {test.test_code}",
                    )
                if test.id in seen:
                    continue
                seen.add(test.id)
                ordered.append(ResolvedLabTest(test=test, panel=panel))

    if require_non_empty and not ordered:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No laboratory tests resolved from selection",
        )
    return ordered


def prefer_sample_type(resolved: list[ResolvedLabTest]) -> LabSampleType | None:
    if not resolved:
        return None
    sample_type = resolved[0].test.sample_type
    for r in resolved:
        if r.test.sample_type == LabSampleType.blood:
            return LabSampleType.blood
    return sample_type


def panel_to_response_dict(panel: LabTestPanel) -> dict:
    members = sorted(panel.tests or [], key=lambda m: (m.sort_order, str(m.id)))
    tests = []
    for m in members:
        t = m.test
        if not t:
            continue
        tests.append(
            {
                "test_id": t.id,
                "test_code": t.test_code,
                "test_name": t.test_name,
                "sample_type": t.sample_type,
                "is_active": t.is_active,
                "sort_order": m.sort_order,
            }
        )
    return {
        "id": panel.id,
        "hospital_id": panel.hospital_id,
        "panel_code": panel.panel_code,
        "panel_name": panel.panel_name,
        "description": panel.description,
        "is_active": panel.is_active,
        "created_at": panel.created_at,
        "updated_at": panel.updated_at,
        "test_count": len(tests),
        "tests": tests,
    }


# Optional seed templates: match existing catalogue by code or name (case-insensitive).
DEFAULT_PANEL_SEEDS: list[dict] = [
    {
        "panel_code": "LIPID",
        "panel_name": "Lipid Panel",
        "description": "Lipid profile",
        "match": ["LIPID", "HDL", "LDL", "TRIG", "TRIGLYCERIDES", "CHOL", "TOTAL CHOLESTEROL", "TC"],
    },
    {
        "panel_code": "THYROID",
        "panel_name": "Thyroid Panel",
        "description": "Thyroid function",
        "match": ["THYROID", "TSH", "T3", "T4", "FT3", "FT4", "FREE T3", "FREE T4"],
    },
    {
        "panel_code": "LFT",
        "panel_name": "Liver Function Test",
        "description": "Liver function panel",
        "match": ["LFT", "SGOT", "SGPT", "AST", "ALT", "ALP", "BILIRUBIN", "GGT"],
    },
    {
        "panel_code": "RFT",
        "panel_name": "Renal Function Test",
        "description": "Kidney / renal function",
        "match": ["RFT", "KFT", "UREA", "CREAT", "CREATININE", "BUN", "URIC ACID", "EGFR"],
    },
    {
        "panel_code": "CBC",
        "panel_name": "Complete Blood Count",
        "description": "Complete blood count",
        "match": ["CBC", "HB", "HEMOGLOBIN", "WBC", "RBC", "PLT", "PLATELET", "HEMATOCRIT", "PCV"],
    },
    {
        "panel_code": "DENGUE",
        "panel_name": "Dengue Panel",
        "description": "Dengue NS1 / IgG / IgM",
        "match": ["DENGUE", "NS1"],
    },
    {
        "panel_code": "MALARIA",
        "panel_name": "Malaria Panel",
        "description": "Malaria parasite / antigen",
        "match": ["MALARIA", "MP"],
    },
]
