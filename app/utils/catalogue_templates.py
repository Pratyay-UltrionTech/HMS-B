"""Hospital catalogue template packs (lab + radiology).

Future packs (NABL, regional, specialty, corporate) can be registered in
TEMPLATE_PACKS without schema redesign. Seeding is always hospital-scoped
and idempotent by catalogue code.
"""

from __future__ import annotations

from typing import Any

from app.models import LabSampleType

# Pack registry — add future packs here (nabl, regional, specialty, corporate).
TEMPLATE_PACKS: dict[str, dict[str, Any]] = {}


def _register(pack: dict[str, Any]) -> None:
    TEMPLATE_PACKS[pack["id"]] = pack


STANDARD_LAB_TESTS: list[dict[str, Any]] = [
    # Primary pathology catalogue (orderable items)
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
    # Supporting analytes (help default panels resolve richer member sets)
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

STANDARD_RADIOLOGY_SCANS: list[dict[str, Any]] = [
    {"scan_code": "XRCHEST", "scan_name": "X-Ray Chest", "category": "X-Ray", "department": "Radiology", "price": 400, "duration_minutes": 15, "description": "Chest radiograph"},
    {"scan_code": "XRKNEE", "scan_name": "X-Ray Knee", "category": "X-Ray", "department": "Radiology", "price": 450, "duration_minutes": 15, "description": "Knee radiograph"},
    {"scan_code": "XRSPINE", "scan_name": "X-Ray Spine", "category": "X-Ray", "department": "Radiology", "price": 500, "duration_minutes": 20, "description": "Spine radiograph"},
    {"scan_code": "XRPELVIS", "scan_name": "X-Ray Pelvis", "category": "X-Ray", "department": "Radiology", "price": 450, "duration_minutes": 15, "description": "Pelvis radiograph"},
    {"scan_code": "USGABD", "scan_name": "Ultrasound Abdomen", "category": "Ultrasound", "department": "Radiology", "price": 900, "duration_minutes": 30, "description": "Abdominal ultrasound"},
    {"scan_code": "USGPEL", "scan_name": "Ultrasound Pelvis", "category": "Ultrasound", "department": "Radiology", "price": 900, "duration_minutes": 30, "description": "Pelvic ultrasound"},
    {"scan_code": "USGPREG", "scan_name": "Ultrasound Pregnancy", "category": "Ultrasound", "department": "Radiology", "price": 1200, "duration_minutes": 30, "description": "Obstetric ultrasound"},
    {"scan_code": "CTBRAIN", "scan_name": "CT Brain", "category": "CT", "department": "Radiology", "price": 3500, "duration_minutes": 30, "description": "CT brain"},
    {"scan_code": "CTCHEST", "scan_name": "CT Chest", "category": "CT", "department": "Radiology", "price": 4500, "duration_minutes": 30, "description": "CT chest"},
    {"scan_code": "CTABD", "scan_name": "CT Abdomen", "category": "CT", "department": "Radiology", "price": 5000, "duration_minutes": 40, "description": "CT abdomen"},
    {"scan_code": "CTSPINE", "scan_name": "CT Spine", "category": "CT", "department": "Radiology", "price": 4500, "duration_minutes": 30, "description": "CT spine"},
    {"scan_code": "MRIBRAIN", "scan_name": "MRI Brain", "category": "MRI", "department": "Radiology", "price": 7000, "duration_minutes": 45, "description": "MRI brain"},
    {"scan_code": "MRISPINE", "scan_name": "MRI Spine", "category": "MRI", "department": "Radiology", "price": 7500, "duration_minutes": 45, "description": "MRI spine"},
    {"scan_code": "MRIKNEE", "scan_name": "MRI Knee", "category": "MRI", "department": "Radiology", "price": 6500, "duration_minutes": 40, "description": "MRI knee"},
    {"scan_code": "MRISHOULDER", "scan_name": "MRI Shoulder", "category": "MRI", "department": "Radiology", "price": 6500, "duration_minutes": 40, "description": "MRI shoulder"},
    {"scan_code": "ECHO2D", "scan_name": "2D Echo", "category": "Cardiology Imaging", "department": "Cardiology", "price": 2500, "duration_minutes": 30, "description": "2D echocardiography"},
    {"scan_code": "ECG", "scan_name": "ECG", "category": "Cardiology Imaging", "department": "Cardiology", "price": 300, "duration_minutes": 10, "description": "Electrocardiogram"},
    {"scan_code": "MAMMO", "scan_name": "Mammography", "category": "Mammography", "department": "Radiology", "price": 1800, "duration_minutes": 30, "description": "Mammogram"},
    {"scan_code": "DEXA", "scan_name": "Dexa Scan", "category": "Bone Density", "department": "Radiology", "price": 2200, "duration_minutes": 30, "description": "DEXA bone densitometry"},
]

_register(
    {
        "id": "standard",
        "label": "Standard Hospital Catalogue",
        "version": 1,
        "region": "IN",
        "specialty": None,
        "lab_tests": STANDARD_LAB_TESTS,
        "radiology_scans": STANDARD_RADIOLOGY_SCANS,
    }
)


def get_template_pack(pack_id: str = "standard") -> dict[str, Any]:
    pack = TEMPLATE_PACKS.get(pack_id)
    if not pack:
        raise KeyError(f"Unknown catalogue template pack: {pack_id}")
    return pack
