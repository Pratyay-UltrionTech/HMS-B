"""
Laboratory specimen resolution and container mapping service.

Maps laboratory test requirements into physical collection containers/specimens:
- EDTA Blood (CBC, HbA1c, ESR, Hemoglobin, Blood Group, etc.)
- Citrate Blood (PT/INR, D-Dimer, Coagulation)
- Fluoride Blood (Blood Sugar Fasting, Blood Sugar PP)
- Serum / SST Blood (LFT, RFT, Lipid, Thyroid, VitD, VitB12, Electrolytes, Serology)
- Sterile Urine Cup (Urine Routine, Urine Culture)
- Stool Container (Stool Examination)
- Swab / Sterile Swab Tube (Culture swabs, RT-PCR)
- Sputum Cup (Sputum AFB)
"""

from __future__ import annotations

from typing import Any
from modules.laboratory.entities.lab_entities import LabSampleType


CONTAINER_EDTA = "EDTA Tube (Lavender Top)"
CONTAINER_CITRATE = "Citrate Tube (Light Blue Top)"
CONTAINER_FLUORIDE = "Fluoride Tube (Gray Top)"
CONTAINER_SERUM = "Serum / SST Tube (Gold/Yellow Top)"
CONTAINER_URINE = "Sterile Urine Cup"
CONTAINER_STOOL = "Stool Specimen Cup"
CONTAINER_SPUTUM = "Sterile Sputum Cup"
CONTAINER_SWAB = "Sterile Swab Container"
CONTAINER_GENERAL = "General Specimen Container"


def determine_specimen_requirement(
    sample_type: LabSampleType | str,
    test_code: str,
    test_name: str,
    department: str = "",
) -> tuple[LabSampleType, str]:
    """
    Returns (resolved_sample_type, container_type) based on test and sample nature.
    """
    code = (test_code or "").upper().strip()
    name = (test_name or "").upper().strip()
    dept = (department or "").upper().strip()

    # 1. Non-blood specimen categories
    if sample_type == LabSampleType.urine or "URINE" in code or "URINE" in name:
        return (LabSampleType.urine, CONTAINER_URINE)
    if sample_type == LabSampleType.stool or "STOOL" in code or "STOOL" in name:
        return (LabSampleType.stool, CONTAINER_STOOL)
    if sample_type == LabSampleType.sputum or "SPUTUM" in code or "SPUTUM" in name:
        return (LabSampleType.sputum, CONTAINER_SPUTUM)
    if sample_type == LabSampleType.swab or "SWAB" in code or "SWAB" in name:
        return (LabSampleType.swab, CONTAINER_SWAB)

    # 2. Blood specimens differentiated by tube / anticoagulant requirement
    # EDTA blood (whole blood haematology)
    edta_markers = ["CBC", "HB", "HEMOGLOBIN", "HBA1C", "ESR", "WBC", "PLT", "PLATELET", "MALARIA", "MP", "BLOOD GROUP", "PERIPHERAL"]
    if any(m in code or m in name for m in edta_markers) and not any(k in code for k in ["BSF", "BSPP", "RFT", "LFT"]):
        return (LabSampleType.blood, CONTAINER_EDTA)

    # Citrate blood (coagulation studies)
    citrate_markers = ["PT", "INR", "PTINR", "DDIMER", "D-DIMER", "APTT", "FIBRINOGEN"]
    if any(m in code or m in name for m in citrate_markers):
        return (LabSampleType.blood, CONTAINER_CITRATE)

    # Fluoride blood (glycolysis inhibition for blood sugar)
    fluoride_markers = ["BSF", "BSPP", "GLUCOSE", "FASTING SUGAR", "PP SUGAR"]
    if any(m in code or m in name for m in fluoride_markers) and "HBA1C" not in code:
        return (LabSampleType.blood, CONTAINER_FLUORIDE)

    # Serum SST (Biochemistry, Serology, Endocrinology, Immunology)
    if sample_type == LabSampleType.blood or dept in ["BIOCHEMISTRY", "SEROLOGY", "ENDOCRINOLOGY", "CARDIOLOGY LAB"]:
        return (LabSampleType.blood, CONTAINER_SERUM)

    # Fallback to general container for other types
    st = sample_type if isinstance(sample_type, LabSampleType) else LabSampleType.other
    return (st, CONTAINER_GENERAL)
