"""
Laboratory panel resolution and catalogue template services.

Provides target-native resolution of lab selection (individual tests + panels),
catalogue seeding, and sample type preference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from hms_migration.modules.laboratory.entities.lab_entities import (
    LabPanelTest,
    LabSampleType,
    LabTestCatalog,
    LabTestPanel,
)


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


def panel_to_response_dict(panel: LabTestPanel) -> dict[str, Any]:
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
        "price": float(panel.price or 0),
        "is_active": panel.is_active,
        "created_at": panel.created_at,
        "updated_at": panel.updated_at,
        "test_count": len(tests),
        "tests": tests,
    }


DEFAULT_PANEL_SEEDS: list[dict[str, Any]] = [
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

STANDARD_LAB_TESTS: list[dict[str, Any]] = [
    {"test_code": "CBC", "test_name": "Complete Blood Count", "department": "Haematology", "price": 350, "sample_type": LabSampleType.blood, "tat_hours": 6, "description": "Complete blood count"},
    {"test_code": "LFT", "test_name": "Liver Function Test", "department": "Biochemistry", "price": 600, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "Liver function panel"},
    {"test_code": "RFT", "test_name": "Renal Function Test", "department": "Biochemistry", "price": 550, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "Renal / kidney function panel"},
    {"test_code": "HBA1C", "test_name": "HbA1c", "department": "Biochemistry", "price": 500, "sample_type": LabSampleType.blood, "tat_hours": 24, "description": "Glycated haemoglobin"},
    {"test_code": "BSF", "test_name": "Blood Sugar Fasting", "department": "Biochemistry", "price": 120, "sample_type": LabSampleType.blood, "tat_hours": 4, "description": "Fasting blood glucose"},
    {"test_code": "BSPP", "test_name": "Blood Sugar PP", "department": "Biochemistry", "price": 120, "sample_type": LabSampleType.blood, "tat_hours": 4, "description": "Post-prandial blood glucose"},
    {"test_code": "LIPID", "test_name": "Lipid Profile", "department": "Biochemistry", "price": 650, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "Lipid profile"},
    {"test_code": "THYROID", "test_name": "Thyroid Profile", "department": "Endocrinology", "price": 700, "sample_type": LabSampleType.blood, "tat_hours": 24, "description": "Thyroid function profile"},
    {"test_code": "VITD", "test_name": "Vitamin D", "department": "Biochemistry", "price": 1200, "sample_type": LabSampleType.blood, "tat_hours": 48, "description": "25-OH Vitamin D"},
    {"test_code": "VITB12", "test_name": "Vitamin B12", "department": "Biochemistry", "price": 900, "sample_type": LabSampleType.blood, "tat_hours": 48, "description": "Vitamin B12"},
    {"test_code": "URINE", "test_name": "Urine Routine", "department": "Clinical Pathology", "price": 150, "sample_type": LabSampleType.urine, "tat_hours": 6, "description": "Urine routine & microscopy"},
    {"test_code": "DENGUE", "test_name": "Dengue Panel", "department": "Serology", "price": 1500, "sample_type": LabSampleType.blood, "tat_hours": 24, "description": "Dengue NS1 / IgG / IgM panel"},
    {"test_code": "MALARIA", "test_name": "Malaria Panel", "department": "Haematology", "price": 400, "sample_type": LabSampleType.blood, "tat_hours": 6, "description": "Malaria parasite / antigen"},
    {"test_code": "TYPHOID", "test_name": "Typhoid Test", "department": "Serology", "price": 450, "sample_type": LabSampleType.blood, "tat_hours": 24, "description": "Typhoid / Widal / IgM"},
    {"test_code": "ESR", "test_name": "ESR", "department": "Haematology", "price": 100, "sample_type": LabSampleType.blood, "tat_hours": 4, "description": "Erythrocyte sedimentation rate"},
    {"test_code": "CRP", "test_name": "CRP", "department": "Biochemistry", "price": 450, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "C-reactive protein"},
    {"test_code": "PTINR", "test_name": "PT/INR", "department": "Haematology", "price": 350, "sample_type": LabSampleType.blood, "tat_hours": 6, "description": "Prothrombin time / INR"},
    {"test_code": "DDIMER", "test_name": "D-Dimer", "department": "Haematology", "price": 1200, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "D-Dimer"},
    {"test_code": "TROPONIN", "test_name": "Troponin", "department": "Cardiology Lab", "price": 1500, "sample_type": LabSampleType.blood, "tat_hours": 6, "description": "Cardiac troponin"},
    {"test_code": "HB", "test_name": "Hemoglobin", "department": "Haematology", "price": 100, "sample_type": LabSampleType.blood, "tat_hours": 4, "description": "Hemoglobin"},
    {"test_code": "WBC", "test_name": "Total Leukocyte Count", "department": "Haematology", "price": 120, "sample_type": LabSampleType.blood, "tat_hours": 4, "description": "TLC / WBC"},
    {"test_code": "PLT", "test_name": "Platelet Count", "department": "Haematology", "price": 120, "sample_type": LabSampleType.blood, "tat_hours": 4, "description": "Platelet count"},
    {"test_code": "HDL", "test_name": "HDL Cholesterol", "department": "Biochemistry", "price": 200, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "HDL"},
    {"test_code": "LDL", "test_name": "LDL Cholesterol", "department": "Biochemistry", "price": 200, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "LDL"},
    {"test_code": "TRIG", "test_name": "Triglycerides", "department": "Biochemistry", "price": 200, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "Triglycerides"},
    {"test_code": "CHOL", "test_name": "Total Cholesterol", "department": "Biochemistry", "price": 180, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "Total cholesterol"},
    {"test_code": "TSH", "test_name": "TSH", "department": "Endocrinology", "price": 350, "sample_type": LabSampleType.blood, "tat_hours": 24, "description": "Thyroid stimulating hormone"},
    {"test_code": "T3", "test_name": "T3", "department": "Endocrinology", "price": 300, "sample_type": LabSampleType.blood, "tat_hours": 24, "description": "Triiodothyronine"},
    {"test_code": "T4", "test_name": "T4", "department": "Endocrinology", "price": 300, "sample_type": LabSampleType.blood, "tat_hours": 24, "description": "Thyroxine"},
    {"test_code": "SGOT", "test_name": "SGOT / AST", "department": "Biochemistry", "price": 150, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "AST"},
    {"test_code": "SGPT", "test_name": "SGPT / ALT", "department": "Biochemistry", "price": 150, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "ALT"},
    {"test_code": "UREA", "test_name": "Blood Urea", "department": "Biochemistry", "price": 150, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "Blood urea"},
    {"test_code": "CREAT", "test_name": "Serum Creatinine", "department": "Biochemistry", "price": 150, "sample_type": LabSampleType.blood, "tat_hours": 12, "description": "Creatinine"},
]
