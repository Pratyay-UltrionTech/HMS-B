#!/usr/bin/env python3
"""
Idempotent SHC demo hospital seed for HMS.

Usage (from HMS-B-main root):
    python scripts/seed_shc_demo.py

If hospital SHC / Adminshc@gmail.com already exists, it is deleted (CASCADE)
and recreated. Other hospitals are preserved.
"""

from __future__ import annotations

import base64
import random
import sys
from datetime import date, datetime, timedelta, time, timezone
from pathlib import Path
from uuid import UUID

# Allow `python scripts/seed_shc_demo.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    Admission,
    AdmissionStatus,
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AuditLog,
    Bed,
    BillingCharge,
    BillingChargeStatus,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceStatus,
    BillingPayment,
    BillingPaymentMethod,
    BillingReceipt,
    BillingReceiptStatus,
    BillingSourceType,
    ConsultationPricing,
    Department,
    EquipmentAssignTarget,
    EquipmentAssignment,
    EquipmentCategory,
    EquipmentItem,
    EquipmentMaintenance,
    EquipmentRequest,
    EquipmentRequestStatus,
    EquipmentServiceLog,
    EquipmentStatus,
    Holiday,
    Hospital,
    HospitalUser,
    LabItemStatus,
    LabOrder,
    LabOrderItem,
    LabOrderSource,
    LabOrderStatus,
    LabPanelTest,
    LabResult,
    LabSampleType,
    LabTestCatalog,
    LabTestPanel,
    MaintenanceStatus,
    MedicalRecord,
    OtPriority,
    OtRoom,
    OtSurgery,
    OtSurgeryStatus,
    Patient,
    PatientDocument,
    PatientDocumentCategory,
    PatientStatus,
    PlanType,
    Prescription,
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
    RolePermission,
    Room,
    ShiftType,
    StaffRole,
    Supplier,
    Ward,
    WardType,
    Wing,
)
from app.schemas_admin import BASIC_MODULE_KEYS
from app.utils.billing import allocate_payment_to_charges, ensure_bed_charge_for_admission, ensure_charge
from app.utils.invoices import next_invoice_number, next_receipt_number
from app.utils.password import generate_temp_password
import bcrypt


def hash_password(password: str) -> str:
    """Faster bcrypt for bulk demo seeding (still works with app login)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=6)).decode("utf-8")


# ── Constants ─────────────────────────────────────────────────────────────────
HOSPITAL_NAME = "SHC"
HOSPITAL_CODE = "SHC"
ADMIN_EMAIL = "adminshc@gmail.com"
CONTACT_EMAIL = "info@shc.com"
HOSPITAL_WEBSITE = "www.shc.com"
HOSPITAL_PHONE = "+91-40-6789-4500"
HOSPITAL_ADDRESS = (
    "SHC Multi-Speciality Hospital, Plot No. 128, Road No. 12, "
    "Banjara Hills, Hyderabad, Telangana 500034"
)

RNG = random.Random(20260723)

MINI_PDF = base64.b64encode(
    b"%PDF-1.1\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
).decode("ascii")
DATA_URL_PDF = f"data:application/pdf;base64,{MINI_PDF}"

CREDENTIALS: list[dict] = []
COUNTS: dict[str, int] = {}
TRANSFERS_DONE = 0


def _bump(key: str, n: int = 1) -> None:
    COUNTS[key] = COUNTS.get(key, 0) + n


def _pwd() -> str:
    return generate_temp_password(8)


def _phone(used: set[str], prefix: str = "98") -> str:
    while True:
        p = f"+91-{prefix}{RNG.randint(10000000, 99999999)}"
        if p not in used:
            used.add(p)
            return p


def _email_slug(role: str, name: str) -> str:
    clean = "".join(c for c in name if c.isalnum())
    role_clean = "".join(c for c in role if c.isalnum())
    return f"{role_clean}{clean}@shc.com".lower()


def _dt(d: date, t: time | None = None) -> datetime:
    tt = t or time(10, 0)
    return datetime.combine(d, tt, tzinfo=timezone.utc)


def _past_days(n: int) -> date:
    return date.today() - timedelta(days=n)


def _future_days(n: int) -> date:
    return date.today() + timedelta(days=n)


# ── Name pools ────────────────────────────────────────────────────────────────
FIRST_M = [
    "Arjun", "Rohan", "Vikram", "Aditya", "Suresh", "Karan", "Nikhil", "Rahul",
    "Aman", "Deepak", "Sanjay", "Manoj", "Pranav", "Harish", "Ankit", "Varun",
    "Gaurav", "Ramesh", "Ashwin", "Naveen", "Siddharth", "Yash", "Kunal", "Abhinav",
    "Ishan", "Dev", "Harsh", "Mohit", "Pavan", "Ravi", "Sunil", "Ajay",
]
FIRST_F = [
    "Ananya", "Priya", "Sneha", "Kavya", "Meera", "Divya", "Pooja", "Neha",
    "Aisha", "Riya", "Shreya", "Nisha", "Swati", "Pallavi", "Isha", "Tanvi",
    "Lakshmi", "Deepa", "Anjali", "Sanjana", "Kriti", "Aditi", "Bhavana", "Chitra",
    "Fatima", "Gayatri", "Hemalatha", "Indira", "Jyoti", "Kirti", "Lata", "Maya",
]
LASTS = [
    "Sharma", "Reddy", "Nair", "Iyer", "Patel", "Singh", "Khan", "Gupta",
    "Mehta", "Joshi", "Rao", "Pillai", "Desai", "Chopra", "Malhotra", "Banerjee",
    "Mukherjee", "Verma", "Kapoor", "Agarwal", "Saxena", "Menon", "Shetty", "Das",
    "Bhat", "Kulkarni", "Naidu", "Choudhary", "Pandey", "Tripathi", "Ghosh", "Fernandes",
]
CHILD_FIRST = ["Aarav", "Vihaan", "Anaya", "Myra", "Kabir", "Ishaan", "Sara", "Diya", "Reyansh", "Aadhya"]
CITIES = [
    "Hyderabad", "Secunderabad", "Gachibowli", "Madhapur", "Kukatpally",
    "Begumpet", "Jubilee Hills", "Ameerpet", "Uppal", "LB Nagar",
]
STREETS = [
    "MG Road", "Lake View Colony", "Park Street", "Temple Road", "Station Road",
    "Green Avenue", "Cross Road 4", "Hill Crest Layout", "Indira Nagar", "Sai Colony",
]
BLOOD = ["A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-"]
INSURERS = [
    ("Star Health", "STAR"),
    ("HDFC Ergo", "HDFC"),
    ("Niva Bupa", "NIVA"),
    ("ICICI Lombard", "ICICI"),
    ("Care Health", "CARE"),
    ("Max Bupa", "MAX"),
    ("Oriental Insurance", "ORI"),
    ("New India Assurance", "NIA"),
]
RELATIONS = ["Spouse", "Father", "Mother", "Son", "Daughter", "Sibling", "Friend"]

DOCTOR_PROFILES = [
    ("Rajesh Sharma", "Male", "Cardiologist", "Cardiology", "MD (Cardiology), DM", "TSMC/CAR/2012/8841", 14, 1200, "CR-101"),
    ("Meera Reddy", "Female", "Orthopedic Surgeon", "Orthopedics", "MS (Ortho)", "TSMC/ORT/2010/5520", 16, 1500, "CR-102"),
    ("Anil Nair", "Male", "Neurologist", "Neurology", "MD, DM (Neurology)", "TSMC/NEU/2014/3312", 12, 1800, "CR-103"),
    ("Kavitha Iyer", "Female", "Pediatrician", "Pediatrics", "MD (Pediatrics)", "TSMC/PED/2015/4410", 11, 800, "CR-104"),
    ("Suresh Patel", "Male", "ENT Specialist", "ENT", "MS (ENT)", "TSMC/ENT/2011/2290", 15, 700, "CR-105"),
    ("Priya Menon", "Female", "Dermatologist", "Dermatology", "MD (Dermatology)", "TSMC/DER/2016/1188", 10, 900, "CR-106"),
    ("Vikram Singh", "Male", "General Physician", "General Medicine", "MD (Medicine)", "TSMC/GM/2009/7701", 17, 500, "CR-107"),
    ("Fatima Khan", "Female", "Emergency Physician", "Emergency Medicine", "MD (Emergency Medicine)", "TSMC/EM/2013/6603", 13, 1000, "CR-108"),
    ("Arun Rao", "Male", "Radiologist", "Radiology", "MD (Radiodiagnosis)", "TSMC/RAD/2012/9902", 14, 1100, "CR-109"),
    ("Lakshmi Pillai", "Female", "Pathologist", "Pathology", "MD (Pathology)", "TSMC/PATH/2011/5505", 15, 600, "CR-110"),
]

STAFF_POOLS = {
    "Receptionist": [
        ("Anitha Reddy", "Female"), ("Rahul Verma", "Male"), ("Sneha Kapoor", "Female"),
    ],
    "Nurse": [
        ("Deepa Nair", "Female"), ("Sunita Sharma", "Female"), ("Mary Thomas", "Female"),
        ("Kavya Rao", "Female"), ("Pooja Singh", "Female"), ("Ramesh Yadav", "Male"),
        ("Neha Gupta", "Female"), ("Asha Menon", "Female"), ("Jyoti Das", "Female"), ("Imran Ali", "Male"),
    ],
    "Lab Technician": [
        ("Harish Bhat", "Male"), ("Swati Joshi", "Female"), ("Manoj Kulkarni", "Male"), ("Divya Shetty", "Female"),
    ],
    "Radiology Technician": [
        ("Karthik Naidu", "Male"), ("Bhavana Reddy", "Female"), ("Sanjay Mehta", "Male"), ("Riya Fernandes", "Female"),
    ],
    "OT Staff": [
        ("Prakash Iyer", "Male"), ("Latha Krishnan", "Female"), ("Naveen Choudhary", "Male"), ("Geetha Pillai", "Female"),
    ],
    "Billing Staff": [
        ("Sangeeta Agarwal", "Female"), ("Vishal Gupta", "Male"), ("Rekha Pandey", "Female"),
    ],
    "Equipment Manager": [
        ("Mahesh Desai", "Male"), ("Anita Saxena", "Female"),
    ],
}

ROLE_PERMS = {
    "Doctor": {k: (True, True) for k in ["doctors", "registration", "appointment", "bed", "laboratory", "radiology", "ot", "dms", "mis"]},
    "Nurse": {k: (True, True) for k in ["registration", "appointment", "bed", "dms", "mis"]},
    "Receptionist": {k: (True, True) for k in ["registration", "appointment", "bed", "billing", "dms", "mis"]},
    "Lab Technician": {k: (True, True) for k in ["laboratory", "dms", "mis"]},
    "Radiology Technician": {k: (True, True) for k in ["radiology", "dms", "mis"]},
    "OT Staff": {k: (True, True) for k in ["ot", "bed", "dms", "mis"]},
    "Billing Staff": {k: (True, True) for k in ["billing", "registration", "mis"]},
    "Equipment Manager": {k: (True, True) for k in ["equipment", "mis"]},
    "Hospital Admin": {k: (True, True) for k in BASIC_MODULE_KEYS},
}

WINGS = [
    ("Main Building", "MAIN", "OPD clinics, wards, and administration"),
    ("Speciality Block", "SPEC", "Surgical and specialty clinical services"),
    ("Diagnostic Block", "DIAG", "Laboratory, radiology, and diagnostics"),
]

DEPTS = [
    ("Cardiology", "CARD", "Main Building"),
    ("Orthopedics", "ORTH", "Speciality Block"),
    ("Neurology", "NEUR", "Speciality Block"),
    ("General Medicine", "GMED", "Main Building"),
    ("Pediatrics", "PED", "Main Building"),
    ("Dermatology", "DERM", "Main Building"),
    ("ENT", "ENT", "Main Building"),
    ("Radiology", "RAD", "Diagnostic Block"),
    ("Pathology", "PATH", "Diagnostic Block"),
    ("Emergency Medicine", "EMER", "Main Building"),
]

SHIFT_DEFS = [
    ("Morning", time(8, 0), time(14, 0), "Morning OPD / clinical shift"),
    ("General", time(9, 0), time(17, 0), "Standard daytime duty"),
    ("Evening", time(14, 0), time(20, 0), "Evening OPD / ward coverage"),
    ("Night", time(20, 0), time(8, 0), "Night duty coverage"),
    ("Emergency", time(0, 0), time(23, 59), "Round-the-clock emergency roster"),
    ("Weekend", time(9, 0), time(15, 0), "Weekend duty roster"),
]

APPT_TYPES = [
    ("New Consultation", 20, False),
    ("Follow Up", 15, True),
    ("Emergency", 10, False),
    ("Review", 15, True),
    ("Teleconsult", 15, False),
]

EXTRA_LAB = [
    ("KFT", "Kidney Function Test", "Biochemistry", 550, LabSampleType.blood, 12),
    ("TFT", "Thyroid Function Free", "Endocrinology", 850, LabSampleType.blood, 24),
    ("IRON", "Iron Studies", "Biochemistry", 700, LabSampleType.blood, 24),
    ("FERR", "Ferritin", "Biochemistry", 650, LabSampleType.blood, 24),
    ("PSA", "PSA Total", "Biochemistry", 900, LabSampleType.blood, 24),
    ("BHCG", "Beta HCG", "Biochemistry", 800, LabSampleType.blood, 12),
    ("AMY", "Serum Amylase", "Biochemistry", 400, LabSampleType.blood, 12),
    ("LIPASE", "Serum Lipase", "Biochemistry", 450, LabSampleType.blood, 12),
    ("URIC", "Uric Acid", "Biochemistry", 200, LabSampleType.blood, 8),
    ("CAL", "Serum Calcium", "Biochemistry", 180, LabSampleType.blood, 8),
    ("MG", "Serum Magnesium", "Biochemistry", 250, LabSampleType.blood, 8),
    ("NA", "Serum Sodium", "Biochemistry", 150, LabSampleType.blood, 6),
    ("K", "Serum Potassium", "Biochemistry", 150, LabSampleType.blood, 6),
    ("BLOODG", "Blood Grouping & Rh", "Haematology", 200, LabSampleType.blood, 4),
    ("BTCT", "Bleeding & Clotting Time", "Haematology", 250, LabSampleType.blood, 4),
    ("STOOL", "Stool Routine", "Clinical Pathology", 180, LabSampleType.stool, 8),
    ("SPUTUM", "Sputum AFB", "Microbiology", 350, LabSampleType.sputum, 24),
    ("CULT", "Blood Culture", "Microbiology", 1200, LabSampleType.blood, 72),
    ("HIV", "HIV ELISA", "Serology", 500, LabSampleType.blood, 24),
    ("HBSAG", "HBsAg", "Serology", 400, LabSampleType.blood, 24),
]

EXTRA_RAD = [
    ("XRHAND", "X-Ray Hand", "X-Ray", "Radiology", 350, 15),
    ("XRFOOT", "X-Ray Foot", "X-Ray", "Radiology", 350, 15),
    ("XRSHOULDER", "X-Ray Shoulder", "X-Ray", "Radiology", 400, 15),
    ("USGTHY", "Ultrasound Thyroid", "Ultrasound", "Radiology", 1000, 25),
    ("USGDOP", "Doppler Arterial/Venous", "Doppler", "Radiology", 2200, 40),
    ("CTHEAD", "CT Head Plain", "CT", "Radiology", 3200, 25),
    ("MRILUMB", "MRI Lumbar Spine", "MRI", "Radiology", 7800, 45),
    ("HOLTER", "Holter Monitoring 24hr", "Cardiology Imaging", "Cardiology", 3500, 30),
    ("PFT", "Pulmonary Function Test", "Pulmonary", "Radiology", 1200, 30),
    ("FLUORO", "Fluoroscopy Swallow Study", "Fluoroscopy", "Radiology", 1800, 30),
    ("IVP", "IVP / Intravenous Pyelogram", "X-Ray", "Radiology", 2500, 45),
]

PANELS = [
    ("DIAB", "Diabetes Profile", "Fasting sugar, PP sugar, HbA1c", ["BSF", "BSPP", "HBA1C"]),
    ("CARD", "Cardiac Profile", "Cardiac risk and injury markers", ["LIPID", "TROPONIN", "CRP"]),
    ("EXEC", "Executive Health Check", "Comprehensive executive screening", ["CBC", "LFT", "RFT", "LIPID", "TSH", "URINE"]),
    ("THYP", "Thyroid Profile Panel", "TSH, T3, T4", ["TSH", "T3", "T4"]),
    ("LIVER", "Liver Profile Panel", "Liver enzymes and related tests", ["LFT", "SGOT", "SGPT"]),
    ("RENAL", "Renal Profile Panel", "Kidney function markers", ["RFT", "UREA", "CREAT"]),
    ("ANEMIA", "Anemia Workup", "CBC with iron studies", ["CBC", "HB", "IRON", "FERR"]),
    ("FEVER", "Fever Panel", "Common infectious fever workup", ["CBC", "ESR", "CRP", "DENGUE", "MALARIA", "TYPHOID"]),
    ("PREOP", "Pre-Operative Panel", "Standard pre-surgery labs", ["CBC", "BTCT", "PTINR", "RFT", "BLOODG", "HIV", "HBSAG"]),
    ("VIT", "Vitamin Panel", "Vitamin D and B12", ["VITD", "VITB12"]),
]

RX_TEMPLATES = [
    ("Chest pain on exertion, mild dyspnoea", "Stable angina / CAD under evaluation",
     "Aspirin 75mg; Atorvastatin 40mg; Metoprolol 25mg", "Aspirin OD; Atorvastatin HS; Metoprolol BD",
     "Avoid heavy exertion; follow-up with ECG"),
    ("Knee pain and swelling after fall", "Osteoarthritis right knee with sprain",
     "Aceclofenac 100mg; Pantoprazole 40mg; Calcium + Vit D3", "Aceclofenac BD after food; Pantoprazole OD; Calcium OD",
     "Physiotherapy; ice packs; avoid squatting"),
    ("Recurrent headache, nausea", "Migraine without aura",
     "Naproxen 250mg; Domperidone 10mg; Propranolol 20mg", "Naproxen SOS; Domperidone SOS; Propranolol OD",
     "Maintain sleep hygiene; hydrate well"),
    ("Fever, cough, sore throat 3 days", "Acute upper respiratory infection",
     "Azithromycin 500mg; Paracetamol 650mg; Cetirizine 10mg", "Azithromycin OD x3 days; PCM SOS; Cetirizine HS",
     "Steam inhalation; rest; return if breathlessness"),
    ("Itchy rash on arms and neck", "Allergic contact dermatitis",
     "Levocetirizine 5mg; Mometasone cream; Moisturizer", "Levocetirizine HS; cream BD; moisturizer TID",
     "Avoid known allergens; cotton clothing"),
    ("Ear pain and discharge", "Acute otitis media",
     "Amoxicillin-Clavulanate 625mg; Ibuprofen 400mg; Ofloxacin ear drops", "Augmentin BD x5d; Ibuprofen SOS; drops TID",
     "Keep ear dry; review after 5 days"),
    ("Child with fever and loose stools", "Acute gastroenteritis",
     "ORS sachets; Zinc syrup; Ondansetron 2mg", "ORS after each stool; Zinc OD x14d; Ondansetron SOS",
     "Continue breastfeeding/soft diet; watch dehydration"),
    ("Burning micturition, frequency", "Urinary tract infection",
     "Nitrofurantoin 100mg; Paracetamol 650mg; Plenty of fluids", "Nitrofurantoin BD x5d; PCM SOS",
     "Urine culture advised if no improvement"),
    ("Palpitations, anxiety", "Sinus tachycardia / anxiety related",
     "Propranolol 10mg; Clonazepam 0.25mg", "Propranolol BD; Clonazepam HS x7d",
     "Reduce caffeine; breathing exercises"),
    ("Low backache radiating to leg", "Lumbar radiculopathy",
     "Pregabalin 75mg; Thiocolchicoside 4mg; Diclofenac gel", "Pregabalin HS; muscle relaxant BD; gel TID",
     "Avoid lifting weights; physiotherapy referral"),
]

RAD_FINDINGS = [
    ("No acute cardiopulmonary abnormality. Heart size normal. Costophrenic angles clear.",
     "Normal chest radiograph.", "Clinical correlation advised."),
    ("Mild cardiomegaly. No pulmonary edema. No consolidation.",
     "Mild cardiomegaly; no acute lung pathology.", "Compare with prior if available."),
    ("Grade I fatty liver. Gallbladder and pancreas normal. No free fluid.",
     "Hepatic steatosis grade I.", "Lifestyle and lipid evaluation recommended."),
    ("No intracranial hemorrhage. No mass effect. Ventricles normal.",
     "No acute intracranial abnormality.", "MRI if symptoms persist."),
    ("Disc desiccation at L4-L5 with mild posterior bulge. No significant canal stenosis.",
     "L4-L5 disc bulge.", "Orthopedic / neurology correlation."),
    ("Normal left ventricular function. EF 60%. No regional wall motion abnormality.",
     "Normal 2D echocardiogram.", "Continue medical management as advised."),
]

SURGERIES = [
    ("Laparoscopic Appendectomy", "General Surgery", OtPriority.urgent, 90),
    ("Knee Arthroscopy", "Orthopedics", OtPriority.elective, 120),
    ("CABG", "Cardiothoracic", OtPriority.elective, 300),
    ("Cholecystectomy (Lap)", "General Surgery", OtPriority.elective, 100),
    ("Craniotomy for SDH", "Neurosurgery", OtPriority.emergency, 180),
    ("Tonsillectomy", "ENT", OtPriority.elective, 60),
    ("ORIF Tibia", "Orthopedics", OtPriority.urgent, 150),
    ("Hernia Repair (Mesh)", "General Surgery", OtPriority.elective, 90),
    ("Cataract Phaco + IOL", "Ophthalmology", OtPriority.elective, 45),
    ("Emergency Laparotomy", "General Surgery", OtPriority.emergency, 160),
]

EQUIPMENT_ITEMS = [
    ("ICU Ventilator", "Life Support", "Philips", "V60", 850000, "ICU"),
    ("Biphasic Defibrillator", "Life Support", "Zoll", "R Series", 420000, "Emergency"),
    ("12-Lead ECG Machine", "Diagnostics", "GE Healthcare", "MAC 2000", 185000, "Cardiology"),
    ("Portable Ultrasound", "Imaging", "Mindray", "M7", 950000, "Radiology"),
    ("Digital X-Ray System", "Imaging", "Siemens", "Multix Fusion", 3200000, "Radiology"),
    ("Patient Monitor", "Monitoring", "Philips", "IntelliVue MX450", 275000, "ICU"),
    ("Infusion Pump", "Life Support", "B. Braun", "Infusomat Space", 95000, "ICU"),
    ("Anesthesia Workstation", "OT Equipment", "Dräger", "Fabius Plus", 1450000, "OT"),
    ("OT Table", "OT Equipment", "Steris", "5085", 780000, "OT"),
    ("Electrosurgical Unit", "OT Equipment", "Medtronic", "ForceTriad", 320000, "OT"),
    ("Autoclave Steam Sterilizer", "Sterilization", "Tuttnauer", "3870EA", 410000, "CSSD"),
    ("Centrifuge", "Lab Equipment", "Remi", "R-8C", 65000, "Pathology"),
    ("Hematology Analyzer", "Lab Equipment", "Sysmex", "XN-1000", 1850000, "Pathology"),
    ("Biochemistry Analyzer", "Lab Equipment", "Beckman", "AU480", 2100000, "Pathology"),
    ("CT Scanner", "Imaging", "GE", "Revolution EVO", 18500000, "Radiology"),
    ("MRI Scanner 1.5T", "Imaging", "Siemens", "Magnetom Sempra", 42000000, "Radiology"),
    ("Crash Cart", "Emergency", "Local", "Standard", 85000, "Emergency"),
    ("Nebulizer", "Respiratory", "Omron", "NE-C28", 4500, "General Medicine"),
    ("Suction Apparatus", "OT Equipment", "Allied", "AS-100", 28000, "OT"),
    ("Pulse Oximeter", "Monitoring", "Masimo", "Radical-7", 125000, "ICU"),
    ("Dialysis Machine", "Life Support", "Fresenius", "4008S", 980000, "Nephrology"),
    ("Fetal Doppler", "Monitoring", "Huntleigh", "Sonicaid", 45000, "Obstetrics"),
    ("Laryngoscope Set", "OT Equipment", "Heine", "Classic+", 22000, "OT"),
    ("Surgical Headlight", "OT Equipment", "Stryker", "L9000", 180000, "OT"),
    ("Warming Blanket Unit", "OT Equipment", "3M", "Bair Hugger", 210000, "OT"),
    ("Glucometer Dock", "Diagnostics", "Accu-Chek", "Inform II", 35000, "Ward"),
    ("BP Apparatus Digital", "Monitoring", "Omron", "HEM-7120", 3500, "OPD"),
    ("Wheelchair", "Mobility", "Karma", "KM-2500", 12000, "Ward"),
    ("Stretcher Trolley", "Mobility", "Local", "Hydraulic", 45000, "Emergency"),
    ("Oxygen Concentrator", "Respiratory", "Philips", "EverFlo", 65000, "Ward"),
    ("CPAP Device", "Respiratory", "ResMed", "AirSense 10", 78000, "ICU"),
    ("Holter Recorder", "Diagnostics", "GE", "SEER 1000", 240000, "Cardiology"),
    ("EEG Machine", "Diagnostics", "Nihon Kohden", "EEG-1200", 1250000, "Neurology"),
    ("TMT Machine", "Diagnostics", "GE", "CASE", 680000, "Cardiology"),
    ("Slit Lamp", "Diagnostics", "Haag-Streit", "BQ 900", 520000, "Ophthalmology"),
    ("Autorefractometer", "Diagnostics", "Topcon", "KR-800", 380000, "Ophthalmology"),
    ("Dental Chair Unit", "Dental", "Confident", "C-II", 290000, "Dental"),
    ("Endoscopy Tower", "OT Equipment", "Olympus", "EVIS X1", 4500000, "Gastro"),
    ("C-Arm Fluoroscopy", "Imaging", "Siemens", "Cios Select", 5200000, "OT"),
    ("Blood Bank Refrigerator", "Lab Equipment", "Thermo", "TSX", 320000, "Blood Bank"),
    ("Platelet Agitator", "Lab Equipment", "Remi", "CI-12", 95000, "Blood Bank"),
    ("Microscope Binocular", "Lab Equipment", "Olympus", "CX23", 85000, "Pathology"),
    ("Incubator Bacteriological", "Lab Equipment", "Labline", "LI-150", 72000, "Microbiology"),
    ("Hot Air Oven", "Sterilization", "Labline", "HO-90", 48000, "Pathology"),
    ("Water Bath", "Lab Equipment", "Remi", "RWB-12", 22000, "Pathology"),
    ("Portable Ventilator", "Life Support", "Hamilton", "T1", 1100000, "Emergency"),
    ("Syringe Pump", "Life Support", "B. Braun", "Perfusor Space", 88000, "ICU"),
    ("ABG Analyzer", "Lab Equipment", "Radiometer", "ABL90", 980000, "ICU"),
    ("USG Color Doppler Cart", "Imaging", "Samsung", "HS40", 2100000, "Radiology"),
    ("OT Light LED", "OT Equipment", "Dräger", "Polaris 600", 650000, "OT"),
]


# ── Delete existing SHC ───────────────────────────────────────────────────────
def delete_existing_shc(db: Session) -> None:
    db.expire_all()
    rows = db.query(Hospital).all()
    targets = [
        h
        for h in rows
        if h.hospital_id == HOSPITAL_CODE
        or (h.name or "").strip().upper() == HOSPITAL_NAME
        or (h.email or "").lower() in {ADMIN_EMAIL.lower(), CONTACT_EMAIL.lower()}
    ]
    for h in targets:
        print(f"  Deleting existing hospital: {h.name} ({h.hospital_id}) / {h.email}")
        db.delete(h)
    if targets:
        db.commit()
        db.expire_all()
        print(f"  Removed {len(targets)} existing SHC hospital record(s).")
    else:
        print("  No existing SHC hospital found.")


# ── Seed helpers ──────────────────────────────────────────────────────────────
def create_hospital(db: Session) -> tuple[Hospital, str]:
    admin_password = _pwd()
    hospital = Hospital(
        hospital_id=HOSPITAL_CODE,
        name=HOSPITAL_NAME,
        address=HOSPITAL_ADDRESS,
        phone=HOSPITAL_PHONE,
        email=ADMIN_EMAIL,
        password_hash=hash_password(admin_password),
        plan=PlanType.basic,
        is_active=True,
    )
    db.add(hospital)
    db.flush()
    CREDENTIALS.append(
        {
            "role": "Hospital Admin",
            "name": "SHC Administrator",
            "email": ADMIN_EMAIL,
            "password": admin_password,
        }
    )
    _bump("hospitals")
    return hospital, admin_password


def create_masters(db: Session, hid: UUID) -> dict:
    phones: set[str] = set()
    wings: dict[str, Wing] = {}
    for name, code, desc in WINGS:
        w = Wing(hospital_id=hid, name=name, code=code, description=desc, is_active=True)
        db.add(w)
        db.flush()
        wings[name] = w
        _bump("wings")

    depts: dict[str, Department] = {}
    for name, code, wing_name in DEPTS:
        d = Department(
            hospital_id=hid,
            wing_id=wings[wing_name].id,
            name=name,
            code=code,
            description=f"{name} department",
            is_active=True,
        )
        db.add(d)
        db.flush()
        depts[name] = d
        _bump("departments")

    shifts: dict[tuple[str, str], ShiftType] = {}
    for dept_name, dept in depts.items():
        for sname, start, end, desc in SHIFT_DEFS:
            s = ShiftType(
                hospital_id=hid,
                department_id=dept.id,
                name=sname,
                start_time=start,
                end_time=end,
                description=desc,
                is_active=True,
            )
            db.add(s)
            db.flush()
            shifts[(dept_name, sname)] = s
            _bump("shift_types")

    appt_types: dict[str, AppointmentType] = {}
    for name, dur, is_fu in APPT_TYPES:
        a = AppointmentType(
            hospital_id=hid,
            name=name,
            slot_duration_minutes=dur,
            description=name,
            is_follow_up=is_fu,
            is_active=True,
        )
        db.add(a)
        db.flush()
        appt_types[name] = a
        _bump("appointment_types")

    holidays = [
        ("Republic Day", date(2026, 1, 26)),
        ("Holi", date(2026, 3, 14)),
        ("Independence Day", date(2026, 8, 15)),
        ("Gandhi Jayanti", date(2026, 10, 2)),
        ("Diwali", date(2026, 11, 8)),
    ]
    for name, d in holidays:
        db.add(Holiday(hospital_id=hid, name=name, holiday_date=d, is_recurring=True))
        _bump("holidays")

    suppliers = [
        ("MedEquip India Pvt Ltd", "Ravi Kumar", "medequip@supplier.in"),
        ("Apollo Surgical Supplies", "Sneha Rao", "apollo.surg@supplier.in"),
        ("CareLabs Diagnostics Trading", "Imran Shaikh", "carelabs@supplier.in"),
        ("Stryker India", "Vikram Desai", "stryker.in@supplier.com"),
        ("Siemens Healthineers", "Anita Bose", "siemens.health@supplier.com"),
    ]
    for i, (name, contact, email) in enumerate(suppliers):
        db.add(
            Supplier(
                hospital_id=hid,
                name=name,
                contact_person=contact,
                phone=_phone(phones, "90"),
                email=email,
                address=f"{i + 1}, Industrial Estate, Hyderabad",
                is_active=True,
            )
        )
        _bump("suppliers")

    db.flush()
    return {
        "wings": wings,
        "depts": depts,
        "shifts": shifts,
        "appt_types": appt_types,
        "phones": phones,
    }


def _add_role(db: Session, hid: UUID, name: str, description: str) -> StaffRole:
    role = StaffRole(hospital_id=hid, name=name, description=description, is_active=True)
    db.add(role)
    db.flush()
    perms = ROLE_PERMS.get(name, {})
    for module in BASIC_MODULE_KEYS:
        can_view, can_edit = perms.get(module, (False, False))
        # Always grant mis view for dashboards that need metrics
        if module == "mis" and name != "Hospital Admin":
            can_view = True
        db.add(
            RolePermission(
                hospital_id=hid,
                role_id=role.id,
                module_key=module,
                can_view=can_view,
                can_edit=can_edit,
            )
        )
    _bump("roles")
    return role


def create_roles_and_staff(db: Session, hid: UUID, ctx: dict) -> dict:
    phones: set[str] = ctx["phones"]
    depts = ctx["depts"]
    shifts = ctx["shifts"]
    wings = ctx["wings"]
    appt_types = ctx["appt_types"]

    roles = {
        "Hospital Admin": _add_role(db, hid, "Hospital Admin", "Full hospital administration"),
        "Doctor": _add_role(db, hid, "Doctor", "Consulting physicians and surgeons"),
        "Nurse": _add_role(db, hid, "Nurse", "Nursing and ward care"),
        "Receptionist": _add_role(db, hid, "Receptionist", "Front desk and appointments"),
        "Lab Technician": _add_role(db, hid, "Lab Technician", "Laboratory operations"),
        "Radiology Technician": _add_role(db, hid, "Radiology Technician", "Radiology operations"),
        "OT Staff": _add_role(db, hid, "OT Staff", "Operation theatre team"),
        "Billing Staff": _add_role(db, hid, "Billing Staff", "Patient billing and collections"),
        "Equipment Manager": _add_role(db, hid, "Equipment Manager", "Biomedical equipment"),
    }

    doctors: list[HospitalUser] = []
    doctor_fees: dict[UUID, float] = {}

    for name, gender, spec, dept_name, qual, reg, exp, fee, room in DOCTOR_PROFILES:
        password = _pwd()
        email = _email_slug("Doctor", name)
        shift = shifts.get((dept_name, "Morning")) or shifts.get((dept_name, "General"))
        user = HospitalUser(
            hospital_id=hid,
            role_id=roles["Doctor"].id,
            shift_id=shift.id if shift else None,
            name=f"Dr. {name}",
            phone=_phone(phones, "97"),
            email=email,
            password_hash=hash_password(password),
            specialization=spec,
            medical_registration_number=reg,
            qualification=qual,
            years_of_experience=exp,
            consultation_room=room,
            custom_values={"gender": gender, "department": dept_name},
            is_active=True,
        )
        db.add(user)
        db.flush()
        doctors.append(user)
        doctor_fees[user.id] = float(fee)
        CREDENTIALS.append({"role": "Doctor", "name": user.name, "email": email, "password": password})
        _bump("doctors")
        _bump("staff")

        dept = depts[dept_name]
        wing = wings[next(w for w, dlist in [
            ("Main Building", ["Cardiology", "General Medicine", "Pediatrics", "Dermatology", "ENT", "Emergency Medicine"]),
            ("Speciality Block", ["Orthopedics", "Neurology"]),
            ("Diagnostic Block", ["Radiology", "Pathology"]),
        ] if dept_name in dlist)]
        for at_name, at in appt_types.items():
            base = fee
            if at.is_follow_up or "Follow" in at_name or at_name == "Review":
                price = max(300, round(base * 0.6 / 50) * 50)
            elif at_name == "Emergency":
                price = min(2000, round(base * 1.25 / 50) * 50)
            elif at_name == "Teleconsult":
                price = max(300, round(base * 0.8 / 50) * 50)
            else:
                price = float(base)
            db.add(
                ConsultationPricing(
                    hospital_id=hid,
                    wing_id=wing.id,
                    department_id=dept.id,
                    doctor_id=user.id,
                    appointment_type_id=at.id,
                    consultation_fee=price,
                    followup_free_days=7 if at.is_follow_up else None,
                    is_active=True,
                )
            )
            _bump("consultation_pricing")

    staff_by_role: dict[str, list[HospitalUser]] = {k: [] for k in STAFF_POOLS}
    dept_for_role = {
        "Receptionist": "General Medicine",
        "Nurse": "General Medicine",
        "Lab Technician": "Pathology",
        "Radiology Technician": "Radiology",
        "OT Staff": "Orthopedics",
        "Billing Staff": "General Medicine",
        "Equipment Manager": "General Medicine",
    }
    shift_for_role = {
        "Receptionist": "General",
        "Nurse": "Morning",
        "Lab Technician": "General",
        "Radiology Technician": "General",
        "OT Staff": "Morning",
        "Billing Staff": "General",
        "Equipment Manager": "General",
    }

    for role_name, people in STAFF_POOLS.items():
        for person_name, gender in people:
            password = _pwd()
            email = _email_slug(role_name, person_name)
            dept_name = dept_for_role[role_name]
            shift_name = shift_for_role[role_name]
            shift = shifts[(dept_name, shift_name)]
            user = HospitalUser(
                hospital_id=hid,
                role_id=roles[role_name].id,
                shift_id=shift.id,
                name=person_name,
                phone=_phone(phones, "96"),
                email=email,
                password_hash=hash_password(password),
                custom_values={"gender": gender},
                is_active=True,
            )
            db.add(user)
            db.flush()
            staff_by_role[role_name].append(user)
            CREDENTIALS.append({"role": role_name, "name": person_name, "email": email, "password": password})
            _bump("staff")
            _bump(role_name.lower().replace(" ", "_") + "s")

    db.flush()
    return {
        "roles": roles,
        "doctors": doctors,
        "doctor_fees": doctor_fees,
        "staff_by_role": staff_by_role,
    }


def _make_patient_name(age: int, gender: str, used_names: set[str]) -> tuple[str, str, str]:
    for _ in range(50):
        if age < 15:
            first = RNG.choice(CHILD_FIRST)
        elif gender == "Female":
            first = RNG.choice(FIRST_F)
        else:
            first = RNG.choice(FIRST_M)
        last = RNG.choice(LASTS)
        full = f"{first} {last}"
        if full not in used_names:
            used_names.add(full)
            return first, last, full
    first, last = f"Pat{RNG.randint(1000,9999)}", RNG.choice(LASTS)
    return first, last, f"{first} {last}"


def create_patients(db: Session, hid: UUID, phones: set[str]) -> list[Patient]:
    patients: list[Patient] = []
    used_names: set[str] = set()
    for i in range(100):
        # Age mix: children, adults, seniors
        roll = RNG.random()
        if roll < 0.15:
            age = RNG.randint(1, 14)
        elif roll < 0.75:
            age = RNG.randint(18, 59)
        else:
            age = RNG.randint(60, 88)
        gender = RNG.choice(["Male", "Female"])
        first, last, full = _make_patient_name(age, gender, used_names)
        dob = date.today() - timedelta(days=age * 365 + RNG.randint(0, 364))
        insured = i < 20
        provider = None
        details = None
        if insured:
            pname, pcode = INSURERS[i % len(INSURERS)]
            provider = pname
            details = {
                "policy_number": f"{pcode}-{2024 + (i % 3)}-{100000 + i * 17}",
                "subscriber_id": f"SUB{800000 + i}",
                "tpa": "MediAssist" if i % 2 == 0 else "Paramount Health",
                "valid_till": str(date.today() + timedelta(days=180 + i * 3)),
            }
        p = Patient(
            hospital_id=hid,
            uhid=f"P{i + 1:04d}",
            first_name=first,
            last_name=last,
            name=full,
            mobile=_phone(phones, "98" if i % 2 == 0 else "99"),
            email=f"{first.lower()}.{last.lower()}{i}@mail.com",
            age=age,
            date_of_birth=dob,
            gender=gender,
            address=f"{RNG.randint(12, 240)}, {RNG.choice(STREETS)}, {RNG.choice(CITIES)}, Telangana",
            emergency_contact=_phone(phones, "95"),
            emergency_contact_name=f"{RNG.choice(FIRST_M + FIRST_F)} {RNG.choice(LASTS)}",
            emergency_contact_relation=RNG.choice(RELATIONS),
            blood_group=RNG.choice(BLOOD),
            has_insurance=insured,
            insurance_provider=provider,
            insurance_details=details,
            status=PatientStatus.active,
        )
        db.add(p)
        patients.append(p)
        _bump("patients")
    db.flush()
    return patients


def create_appointments_and_rx(
    db: Session,
    hid: UUID,
    patients: list[Patient],
    doctors: list[HospitalUser],
    doctor_fees: dict[UUID, float],
    depts: dict[str, Department],
    wings: dict[str, Wing],
    appt_types: dict[str, AppointmentType],
) -> tuple[list[Appointment], list[Prescription]]:
    appointments: list[Appointment] = []
    # Status distribution across 150
    statuses: list[AppointmentStatus] = (
        [AppointmentStatus.completed] * 80
        + [AppointmentStatus.scheduled] * 40
        + [AppointmentStatus.cancelled] * 15
        + [AppointmentStatus.no_show] * 10
        + [AppointmentStatus.waiting] * 5
    )
    RNG.shuffle(statuses)

    purposes = [
        "Chest discomfort evaluation", "Follow-up review", "Fever and cough",
        "Joint pain assessment", "Skin rash consultation", "Headache evaluation",
        "Pre-operative clearance", "Diabetes review", "Hypertension follow-up",
        "Abdominal pain", "Breathlessness", "Ear discharge", "Child wellness check",
        "Back pain", "Allergy consult", "Post-op review", "ECG correlation",
        "Lab report discussion", "Medication adjustment", "General checkup",
    ]

    for i in range(150):
        doctor = doctors[i % len(doctors)]
        patient = patients[i % len(patients)]
        dept_name = (doctor.custom_values or {}).get("department", "General Medicine")
        dept = depts[dept_name]
        wing_name = next(
            wn for wn, names in [
                ("Main Building", ["Cardiology", "General Medicine", "Pediatrics", "Dermatology", "ENT", "Emergency Medicine"]),
                ("Speciality Block", ["Orthopedics", "Neurology"]),
                ("Diagnostic Block", ["Radiology", "Pathology"]),
            ] if dept_name in names
        )
        status = statuses[i]
        if status in (AppointmentStatus.completed, AppointmentStatus.cancelled, AppointmentStatus.no_show):
            adate = _past_days(RNG.randint(1, 150))
        elif status == AppointmentStatus.waiting:
            adate = date.today()
        else:
            # scheduled: mix today + upcoming
            adate = date.today() if i % 5 == 0 else _future_days(RNG.randint(1, 45))

        at = RNG.choice(list(appt_types.values()))
        fee = float(doctor_fees[doctor.id])
        if at.is_follow_up:
            fee = max(300, round(fee * 0.6 / 50) * 50)
        hour = RNG.choice([9, 10, 11, 12, 14, 15, 16, 17])
        minute = RNG.choice([0, 15, 30, 45])
        appt = Appointment(
            hospital_id=hid,
            doctor_id=doctor.id,
            patient_id=patient.id,
            appointment_date=adate,
            appointment_time=time(hour, minute),
            purpose=RNG.choice(purposes),
            visit_type="Emergency" if at.name == "Emergency" else "OPD",
            appointment_type_id=at.id,
            wing_id=wings[wing_name].id,
            department_id=dept.id,
            consultation_fee=fee,
            followup_eligibility="eligible" if at.is_follow_up else None,
            status=status,
            notes="Patient counselled" if status == AppointmentStatus.completed else None,
            queue_token=(i % 40) + 1 if status in (AppointmentStatus.waiting, AppointmentStatus.completed) else None,
            checked_in_at=_dt(adate, time(hour, minute)) if status in (AppointmentStatus.waiting, AppointmentStatus.completed) else None,
            created_at=_dt(adate - timedelta(days=1), time(9, 0)),
        )
        db.add(appt)
        appointments.append(appt)
        _bump("appointments")
        if status == AppointmentStatus.completed and fee > 0:
            db.flush()
            ensure_charge(
                db,
                hospital_id=hid,
                patient_id=patient.id,
                source_type=BillingSourceType.consultation,
                source_id=appt.id,
                description=f"Consultation — {doctor.name} ({at.name})",
                charge_amount=fee,
                created_by_name="Reception Desk",
            )
            _bump("billing_charges")

    db.flush()

    completed = [a for a in appointments if a.status == AppointmentStatus.completed]
    prescriptions: list[Prescription] = []
    for i in range(80):
        appt = completed[i % len(completed)]
        tmpl = RX_TEMPLATES[i % len(RX_TEMPLATES)]
        rx = Prescription(
            hospital_id=hid,
            doctor_id=appt.doctor_id,
            patient_id=appt.patient_id,
            appointment_id=appt.id,
            symptoms=tmpl[0],
            diagnosis=tmpl[1],
            medicines=tmpl[2],
            dosage=tmpl[3],
            advice=tmpl[4],
            follow_up_date=appt.appointment_date + timedelta(days=RNG.choice([7, 10, 14, 21])),
            created_at=_dt(appt.appointment_date, appt.appointment_time),
        )
        db.add(rx)
        prescriptions.append(rx)
        _bump("prescriptions")

        if i < 40:
            db.add(
                MedicalRecord(
                    hospital_id=hid,
                    doctor_id=appt.doctor_id,
                    patient_id=appt.patient_id,
                    appointment_id=appt.id,
                    report_type=RNG.choice(["Clinical Note", "Blood Report", "ECG", "Other"]),
                    title=f"OPD note — {tmpl[1][:60]}",
                    notes=f"Diagnosis: {tmpl[1]}. Plan: {tmpl[4]}",
                    file_name=f"opd_note_{i + 1}.pdf",
                    file_data=DATA_URL_PDF,
                    created_at=_dt(appt.appointment_date, appt.appointment_time),
                )
            )
            _bump("medical_records")

    db.flush()
    return appointments, prescriptions


def create_lab_catalogue(db: Session, hid: UUID) -> tuple[list[LabTestCatalog], list[LabTestPanel]]:
    from app.utils.catalogue_templates import STANDARD_LAB_TESTS

    tests: list[LabTestCatalog] = []
    by_code: dict[str, LabTestCatalog] = {}
    for row in STANDARD_LAB_TESTS:
        t = LabTestCatalog(
            hospital_id=hid,
            test_code=row["test_code"],
            test_name=row["test_name"],
            department=row["department"],
            price=float(row["price"]),
            sample_type=row["sample_type"],
            tat_hours=int(row["tat_hours"]),
            description=row.get("description"),
            is_active=True,
        )
        db.add(t)
        tests.append(t)
        by_code[t.test_code] = t
        _bump("lab_tests")

    for code, name, dept, price, sample, tat in EXTRA_LAB:
        if code in by_code:
            continue
        t = LabTestCatalog(
            hospital_id=hid,
            test_code=code,
            test_name=name,
            department=dept,
            price=float(price),
            sample_type=sample,
            tat_hours=tat,
            description=name,
            is_active=True,
        )
        db.add(t)
        tests.append(t)
        by_code[code] = t
        _bump("lab_tests")

    db.flush()

    panels: list[LabTestPanel] = []
    for code, name, desc, members in PANELS:
        panel = LabTestPanel(
            hospital_id=hid,
            panel_code=code,
            panel_name=name,
            description=desc,
            is_active=True,
        )
        db.add(panel)
        db.flush()
        panels.append(panel)
        _bump("lab_panels")
        for idx, mcode in enumerate(members):
            if mcode not in by_code:
                continue
            db.add(
                LabPanelTest(
                    hospital_id=hid,
                    panel_id=panel.id,
                    test_id=by_code[mcode].id,
                    sort_order=idx,
                )
            )
    db.flush()
    return tests, panels


def _lab_result_params(test: LabTestCatalog) -> list[tuple[str, str, str, str]]:
    code = test.test_code.upper()
    catalog = {
        "CBC": [("Hemoglobin", f"{RNG.uniform(11.5, 15.8):.1f}", "g/dL", "12.0-15.0"),
                ("WBC", f"{RNG.randint(4500, 11000)}", "/µL", "4000-11000"),
                ("Platelets", f"{RNG.randint(150, 400)}", "x10^3/µL", "150-400")],
        "HBA1C": [("HbA1c", f"{RNG.uniform(5.2, 8.4):.1f}", "%", "4.0-5.6")],
        "LFT": [("SGOT", f"{RNG.randint(18, 55)}", "U/L", "5-40"), ("SGPT", f"{RNG.randint(16, 60)}", "U/L", "5-40"),
                ("Bilirubin Total", f"{RNG.uniform(0.4, 1.3):.1f}", "mg/dL", "0.2-1.2")],
        "RFT": [("Urea", f"{RNG.randint(18, 42)}", "mg/dL", "15-40"), ("Creatinine", f"{RNG.uniform(0.6, 1.3):.1f}", "mg/dL", "0.6-1.2")],
        "KFT": [("Urea", f"{RNG.randint(18, 42)}", "mg/dL", "15-40"), ("Creatinine", f"{RNG.uniform(0.6, 1.3):.1f}", "mg/dL", "0.6-1.2")],
        "LIPID": [("Total Cholesterol", f"{RNG.randint(150, 240)}", "mg/dL", "<200"),
                  ("LDL", f"{RNG.randint(80, 160)}", "mg/dL", "<100"),
                  ("HDL", f"{RNG.randint(35, 65)}", "mg/dL", ">40")],
        "TSH": [("TSH", f"{RNG.uniform(0.6, 5.2):.2f}", "µIU/mL", "0.4-4.2")],
        "VITD": [("25-OH Vitamin D", f"{RNG.randint(12, 48)}", "ng/mL", "30-100")],
        "CRP": [("CRP", f"{RNG.uniform(0.5, 18):.1f}", "mg/L", "<5")],
        "ESR": [("ESR", f"{RNG.randint(8, 42)}", "mm/hr", "0-20")],
        "URINE": [("Appearance", "Clear", "", ""), ("Protein", "Nil", "", "Nil"), ("Sugar", "Nil", "", "Nil")],
        "BSF": [("Fasting Glucose", f"{RNG.randint(78, 126)}", "mg/dL", "70-100")],
        "BSPP": [("PP Glucose", f"{RNG.randint(110, 180)}", "mg/dL", "<140")],
        "TROPONIN": [("Troponin I", f"{RNG.uniform(0.01, 0.08):.2f}", "ng/mL", "<0.04")],
    }
    if code in catalog:
        return catalog[code]
    return [(test.test_name, f"{RNG.uniform(1, 100):.1f}", "", "See lab reference")]


def create_lab_orders(
    db: Session,
    hid: UUID,
    patients: list[Patient],
    doctors: list[HospitalUser],
    tests: list[LabTestCatalog],
    lab_techs: list[HospitalUser],
) -> list[LabOrder]:
    statuses = (
        [LabOrderStatus.completed] * 45
        + [LabOrderStatus.in_progress] * 20
        + [LabOrderStatus.sample_collected] * 15
        + [LabOrderStatus.ordered] * 15
        + [LabOrderStatus.cancelled] * 5
    )
    orders: list[LabOrder] = []
    for i in range(100):
        patient = patients[i % len(patients)]
        doctor = doctors[i % len(doctors)]
        status = statuses[i]
        days_ago = RNG.randint(0, 120) if status != LabOrderStatus.ordered else RNG.randint(0, 7)
        ordered_at = _dt(_past_days(days_ago), time(RNG.randint(8, 16), RNG.choice([0, 15, 30])))
        tech = lab_techs[i % len(lab_techs)] if lab_techs else None
        order = LabOrder(
            hospital_id=hid,
            order_no=f"LAB{i + 1:04d}",
            patient_id=patient.id,
            doctor_id=doctor.id,
            order_source=LabOrderSource.doctor_prescribed if i % 3 else LabOrderSource.self_requested,
            ordered_by_name=doctor.name,
            ordered_by_role="Doctor",
            status=status,
            clinical_notes=RNG.choice([
                "Routine evaluation", "Pre-operative workup", "Fever workup",
                "Diabetes monitoring", "Follow-up labs", "Cardio risk assessment",
            ]),
            sample_type=LabSampleType.blood,
            ordered_at=ordered_at,
            collected_at=ordered_at + timedelta(hours=1) if status != LabOrderStatus.ordered else None,
            collected_by=tech.name if tech and status != LabOrderStatus.ordered else None,
            completed_at=ordered_at + timedelta(hours=RNG.randint(4, 36)) if status == LabOrderStatus.completed else None,
        )
        db.add(order)
        db.flush()
        selected = RNG.sample(tests, k=RNG.randint(1, 3))
        for t in selected:
            item_status = LabItemStatus.completed if status == LabOrderStatus.completed else (
                LabItemStatus.processing if status == LabOrderStatus.in_progress else LabItemStatus.pending
            )
            item = LabOrderItem(
                hospital_id=hid,
                order_id=order.id,
                test_id=t.id,
                test_code=t.test_code,
                test_name=t.test_name,
                department=t.department,
                price=float(t.price),
                status=item_status,
            )
            db.add(item)
            db.flush()
            if status == LabOrderStatus.completed:
                for idx, (pname, val, unit, ref) in enumerate(_lab_result_params(t)):
                    db.add(
                        LabResult(
                            hospital_id=hid,
                            order_id=order.id,
                            order_item_id=item.id,
                            parameter_name=pname,
                            result_value=val,
                            unit=unit or None,
                            reference_range=ref or None,
                            remarks="Within expected clinical range" if idx == 0 else None,
                            sort_order=idx,
                        )
                    )
                    _bump("lab_results")
            if status != LabOrderStatus.cancelled and float(t.price) > 0:
                ensure_charge(
                    db,
                    hospital_id=hid,
                    patient_id=patient.id,
                    source_type=BillingSourceType.laboratory,
                    source_id=item.id,
                    description=f"Lab — {t.test_name}",
                    charge_amount=float(t.price),
                    created_by_name=tech.name if tech else "Lab Desk",
                )
                _bump("billing_charges")
        orders.append(order)
        _bump("lab_orders")
    db.flush()
    return orders


def create_radiology(
    db: Session,
    hid: UUID,
    patients: list[Patient],
    doctors: list[HospitalUser],
    rad_techs: list[HospitalUser],
) -> tuple[list[RadiologyScanCatalog], list[RadiologyOrder]]:
    from app.utils.catalogue_templates import STANDARD_RADIOLOGY_SCANS

    scans: list[RadiologyScanCatalog] = []
    for row in STANDARD_RADIOLOGY_SCANS:
        s = RadiologyScanCatalog(
            hospital_id=hid,
            scan_code=row["scan_code"],
            scan_name=row["scan_name"],
            category=row["category"],
            department=row["department"],
            price=float(row["price"]),
            duration_minutes=int(row["duration_minutes"]),
            description=row.get("description"),
            is_active=True,
        )
        db.add(s)
        scans.append(s)
        _bump("radiology_scans")
    existing = {s.scan_code for s in scans}
    for code, name, cat, dept, price, dur in EXTRA_RAD:
        if code in existing:
            continue
        s = RadiologyScanCatalog(
            hospital_id=hid,
            scan_code=code,
            scan_name=name,
            category=cat,
            department=dept,
            price=float(price),
            duration_minutes=dur,
            description=name,
            is_active=True,
        )
        db.add(s)
        scans.append(s)
        _bump("radiology_scans")
    db.flush()

    statuses = (
        [RadiologyOrderStatus.completed] * 28
        + [RadiologyOrderStatus.in_progress] * 10
        + [RadiologyOrderStatus.scheduled] * 10
        + [RadiologyOrderStatus.ordered] * 8
        + [RadiologyOrderStatus.cancelled] * 4
    )
    orders: list[RadiologyOrder] = []
    machines = ["XR-1", "CT-64", "MRI-1.5T", "USG-A", "Echo-Lab", "Doppler-1"]
    for i in range(60):
        scan = scans[i % len(scans)]
        patient = patients[(i * 3) % len(patients)]
        doctor = doctors[i % len(doctors)]
        tech = rad_techs[i % len(rad_techs)] if rad_techs else None
        status = statuses[i]
        days_ago = RNG.randint(0, 100)
        ordered_at = _dt(_past_days(days_ago), time(RNG.randint(8, 17), 0))
        findings = impression = remarks = None
        if status == RadiologyOrderStatus.completed:
            findings, impression, remarks = RAD_FINDINGS[i % len(RAD_FINDINGS)]
        order = RadiologyOrder(
            hospital_id=hid,
            order_no=f"RAD{i + 1:04d}",
            patient_id=patient.id,
            doctor_id=doctor.id,
            scan_id=scan.id,
            scan_code=scan.scan_code,
            scan_name=scan.scan_name,
            category=scan.category,
            price=float(scan.price),
            ordered_by_name=doctor.name,
            ordered_by_role="Doctor",
            status=status,
            clinical_notes="Clinical correlation requested",
            scheduled_at=ordered_at + timedelta(hours=2) if status != RadiologyOrderStatus.ordered else None,
            machine=RNG.choice(machines),
            technician_name=tech.name if tech else None,
            started_at=ordered_at + timedelta(hours=3) if status in (RadiologyOrderStatus.in_progress, RadiologyOrderStatus.completed) else None,
            completed_at=ordered_at + timedelta(hours=5) if status == RadiologyOrderStatus.completed else None,
            findings=findings,
            impression=impression,
            remarks=remarks,
            report_date=ordered_at.date() if status == RadiologyOrderStatus.completed else None,
            report_uploaded_by=tech.name if tech and status == RadiologyOrderStatus.completed else None,
            ordered_at=ordered_at,
        )
        db.add(order)
        db.flush()
        orders.append(order)
        _bump("radiology_orders")
        if status != RadiologyOrderStatus.cancelled and float(scan.price) > 0:
            ensure_charge(
                db,
                hospital_id=hid,
                patient_id=patient.id,
                source_type=BillingSourceType.radiology,
                source_id=order.id,
                description=f"Radiology — {scan.scan_name}",
                charge_amount=float(scan.price),
                created_by_name=tech.name if tech else "Radiology Desk",
            )
            _bump("billing_charges")
    db.flush()
    return scans, orders


def create_ot(
    db: Session,
    hid: UUID,
    patients: list[Patient],
    doctors: list[HospitalUser],
    depts: dict[str, Department],
    wings: dict[str, Wing],
    ot_staff: list[HospitalUser],
) -> tuple[list[OtRoom], list[OtSurgery]]:
    rooms_spec = [
        ("OT-01", "Major OT 1", 25000),
        ("OT-02", "Major OT 2", 25000),
        ("OT-03", "Minor OT", 12000),
        ("OT-04", "Emergency OT", 30000),
        ("OT-05", "Ortho OT", 28000),
    ]
    ortho = depts["Orthopedics"]
    rooms: list[OtRoom] = []
    for code, name, charge in rooms_spec:
        r = OtRoom(
            hospital_id=hid,
            wing_id=wings["Speciality Block"].id,
            department_id=ortho.id,
            code=code,
            name=name,
            description=name,
            base_ot_charge=float(charge),
            is_active=True,
        )
        db.add(r)
        rooms.append(r)
        _bump("ot_rooms")
    db.flush()

    statuses = (
        [OtSurgeryStatus.completed] * 16
        + [OtSurgeryStatus.scheduled] * 8
        + [OtSurgeryStatus.confirmed] * 2
        + [OtSurgeryStatus.in_progress] * 1
        + [OtSurgeryStatus.cancelled] * 3
    )
    surgeons = [d for d in doctors if (d.custom_values or {}).get("department") in ("Orthopedics", "Cardiology", "ENT", "General Medicine", "Neurology")]
    if not surgeons:
        surgeons = doctors
    surgeries: list[OtSurgery] = []
    for i in range(30):
        stype, scat, priority, dur = SURGERIES[i % len(SURGERIES)]
        room = rooms[i % len(rooms)]
        patient = patients[(i * 7) % len(patients)]
        surgeon = surgeons[i % len(surgeons)]
        status = statuses[i]
        if status in (OtSurgeryStatus.completed, OtSurgeryStatus.cancelled):
            when = _dt(_past_days(RNG.randint(5, 140)), time(RNG.randint(8, 14), 0))
        elif status == OtSurgeryStatus.in_progress:
            when = _dt(date.today(), time(9, 0))
        else:
            when = _dt(_future_days(RNG.randint(1, 40)), time(RNG.randint(8, 14), 0))
        staff = ot_staff[i % len(ot_staff)] if ot_staff else None
        surg = OtSurgery(
            hospital_id=hid,
            surgery_no=f"OT{i + 1:04d}",
            patient_id=patient.id,
            surgeon_id=surgeon.id,
            assistant_surgeon=staff.name if staff else "OT Assistant",
            surgery_type=stype,
            surgery_category=scat,
            priority=priority,
            department_id=ortho.id,
            ot_room_id=room.id,
            ot_room=room.code,
            ot_charge_amount=float(room.base_ot_charge),
            scheduled_at=when,
            duration_minutes=dur,
            anaesthetist=RNG.choice(["Dr. Anil Bose", "Dr. Kavita Sen", "Dr. Rohit Jain"]),
            remarks="Standard protocol followed",
            booked_by_name=staff.name if staff else "OT Desk",
            booked_by_role="OT Staff",
            status=status,
            started_at=when if status in (OtSurgeryStatus.in_progress, OtSurgeryStatus.completed) else None,
            completed_at=when + timedelta(minutes=dur) if status == OtSurgeryStatus.completed else None,
            actual_duration_minutes=dur + RNG.randint(-10, 25) if status == OtSurgeryStatus.completed else None,
            pre_op_diagnosis=f"Pre-op: {stype}",
            procedure_performed=stype if status == OtSurgeryStatus.completed else None,
            findings="Procedure completed uneventfully" if status == OtSurgeryStatus.completed else None,
            implants_used="As per implant register" if "ORIF" in stype or "CABG" in stype else None,
            complications="None" if status == OtSurgeryStatus.completed else None,
            post_op_instructions="Monitor vitals; antibiotics as charted; NPO till further orders" if status == OtSurgeryStatus.completed else None,
            follow_up_notes="Review in surgical OPD after 7 days" if status == OtSurgeryStatus.completed else None,
            notes_recorded_by=surgeon.name if status == OtSurgeryStatus.completed else None,
            notes_recorded_at=when + timedelta(minutes=dur) if status == OtSurgeryStatus.completed else None,
        )
        db.add(surg)
        db.flush()
        surgeries.append(surg)
        _bump("ot_surgeries")
        if status != OtSurgeryStatus.cancelled and float(room.base_ot_charge) > 0:
            ensure_charge(
                db,
                hospital_id=hid,
                patient_id=patient.id,
                source_type=BillingSourceType.ot,
                source_id=surg.id,
                description=f"OT — {stype} ({room.name})",
                charge_amount=float(room.base_ot_charge),
                created_by_name=staff.name if staff else "OT Desk",
            )
            _bump("billing_charges")
            patient.status = PatientStatus.admitted if status in (OtSurgeryStatus.scheduled, OtSurgeryStatus.in_progress) else patient.status
    db.flush()
    return rooms, surgeries


def create_beds_and_admissions(
    db: Session,
    hid: UUID,
    patients: list[Patient],
    doctors: list[HospitalUser],
    depts: dict[str, Department],
    wings: dict[str, Wing],
) -> tuple[list[Ward], list[Room], list[Bed], list[Admission]]:
    ward_defs = [
        ("MICU", WardType.icu, "Main Building", "General Medicine", 5000, 8000),
        ("SICU", WardType.icu, "Speciality Block", "Orthopedics", 5500, 8500),
        ("General Male Ward", WardType.general, "Main Building", "General Medicine", 1500, 2000),
        ("General Female Ward", WardType.general, "Main Building", "General Medicine", 1500, 2000),
        ("Pediatric Ward", WardType.general, "Main Building", "Pediatrics", 1800, 2200),
        ("Private Wing A", WardType.private, "Main Building", "Cardiology", 3000, 5000),
        ("Private Wing B", WardType.private, "Speciality Block", "Neurology", 3200, 5500),
        ("Emergency Observation", WardType.emergency, "Main Building", "Emergency Medicine", 2000, 3500),
        ("Post-Op Recovery", WardType.general, "Speciality Block", "Orthopedics", 2500, 3000),
        ("Day Care Ward", WardType.general, "Main Building", "General Medicine", 1000, 1500),
    ]
    wards: list[Ward] = []
    rooms: list[Room] = []
    beds: list[Bed] = []
    for wname, wtype, wing_name, dept_name, adm_fee, bed_fee in ward_defs:
        w = Ward(
            hospital_id=hid,
            wing_id=wings[wing_name].id,
            department_id=depts[dept_name].id,
            name=wname,
            ward_type=wtype,
            description=wname,
            admission_fee=float(adm_fee),
            bed_charge_per_day=float(bed_fee),
            is_active=True,
        )
        db.add(w)
        db.flush()
        wards.append(w)
        _bump("wards")
        # 3 rooms per ward => 30 rooms; beds: distribute to reach 100
        for ridx in range(1, 4):
            bed_count = 4 if wtype != WardType.private else 2
            if wtype == WardType.icu:
                bed_count = 4
            room = Room(
                hospital_id=hid,
                ward_id=w.id,
                room_code=f"W{len(wards):02d}R{ridx}",
                name=f"{wname} Room {ridx}",
                bed_count=bed_count,
                is_active=True,
            )
            db.add(room)
            db.flush()
            rooms.append(room)
            _bump("rooms")
            for bidx in range(1, bed_count + 1):
                bed = Bed(
                    hospital_id=hid,
                    ward_id=w.id,
                    room_id=room.id,
                    bed_code=f"B{bidx}",
                    is_occupied=False,
                    is_active=True,
                )
                db.add(bed)
                beds.append(bed)
                _bump("beds")

    # Top up to 100 beds if short
    while len(beds) < 100:
        room = rooms[len(beds) % len(rooms)]
        bed = Bed(
            hospital_id=hid,
            ward_id=room.ward_id,
            room_id=room.id,
            bed_code=f"BX{len(beds) + 1}",
            is_occupied=False,
            is_active=True,
        )
        db.add(bed)
        beds.append(bed)
        _bump("beds")
        room.bed_count = (room.bed_count or 0) + 1
    db.flush()

    admissions: list[Admission] = []
    free_beds = list(beds)
    RNG.shuffle(free_beds)
    global TRANSFERS_DONE

    for i in range(40):
        patient = patients[(i * 2 + 5) % len(patients)]
        doctor = doctors[i % len(doctors)]
        bed = free_beds[i]
        room = next(r for r in rooms if r.id == bed.room_id)
        ward = next(w for w in wards if w.id == bed.ward_id)
        is_active = i < 18  # 18 active, 22 discharged
        admitted_on = _past_days(RNG.randint(2, 90))
        admitted_at = _dt(admitted_on, time(RNG.randint(8, 20), 0))
        discharged_at = None
        discharge_notes = None
        status = AdmissionStatus.admitted
        if not is_active:
            status = AdmissionStatus.discharged
            stay = RNG.randint(1, 12)
            discharged_at = admitted_at + timedelta(days=stay, hours=RNG.randint(1, 10))
            discharge_notes = RNG.choice([
                "Stable at discharge. Continue oral medications. Review in OPD in 7 days.",
                "Recovered well post procedure. Wound clean. Suture removal after 10 days.",
                "Afebrile, vitals stable. Diet as tolerated. Follow-up with treating consultant.",
                "Discharged against medical advice after counselling.",
                "Condition improved. Home care advice given. Emergency contact provided.",
            ])
            patient.status = PatientStatus.active
        else:
            bed.is_occupied = True
            patient.status = PatientStatus.admitted

        adm = Admission(
            hospital_id=hid,
            patient_id=patient.id,
            ward_id=ward.id,
            room_id=room.id,
            bed_id=bed.id,
            doctor_id=doctor.id,
            status=status,
            notes=f"Admitted for {RNG.choice(['observation', 'medical management', 'post-op care', 'IV antibiotics', 'cardiac monitoring'])}",
            discharge_notes=discharge_notes,
            admitted_at=admitted_at,
            discharged_at=discharged_at,
        )
        db.add(adm)
        db.flush()
        admissions.append(adm)
        _bump("admissions")

        ensure_charge(
            db,
            hospital_id=hid,
            patient_id=patient.id,
            source_type=BillingSourceType.admission,
            source_id=adm.id,
            description=f"Admission Charge — {ward.name}",
            charge_amount=float(ward.admission_fee),
            created_by_name="IPD Desk",
        )
        _bump("billing_charges")

        if discharged_at:
            ensure_bed_charge_for_admission(
                db,
                hospital_id=hid,
                patient_id=patient.id,
                admission_id=adm.id,
                admitted_at=admitted_at,
                discharged_at=discharged_at,
                ward_name=ward.name,
                room_code=room.room_code,
                bed_code=bed.bed_code,
                bed_charge_per_day=float(ward.bed_charge_per_day),
                created_by_name="IPD Desk",
            )
            _bump("billing_charges")
        elif is_active:
            # Accrue interim bed charge for active stays
            ensure_bed_charge_for_admission(
                db,
                hospital_id=hid,
                patient_id=patient.id,
                admission_id=adm.id,
                admitted_at=admitted_at,
                discharged_at=datetime.now(timezone.utc),
                ward_name=ward.name,
                room_code=room.room_code,
                bed_code=bed.bed_code,
                bed_charge_per_day=float(ward.bed_charge_per_day),
                created_by_name="IPD Desk",
            )
            _bump("billing_charges")

    # Perform transfers on some active admissions
    active = [a for a in admissions if a.status == AdmissionStatus.admitted]
    unused_beds = [b for b in beds if not b.is_occupied]
    for i, adm in enumerate(active[:8]):
        if not unused_beds:
            break
        new_bed = unused_beds.pop()
        old_bed = next(b for b in beds if b.id == adm.bed_id)
        old_bed.is_occupied = False
        new_room = next(r for r in rooms if r.id == new_bed.room_id)
        new_ward = next(w for w in wards if w.id == new_bed.ward_id)
        from_label = f"{old_bed.bed_code}"
        to_label = f"{new_ward.name}/{new_room.room_code}/{new_bed.bed_code}"
        adm.ward_id = new_ward.id
        adm.room_id = new_room.id
        adm.bed_id = new_bed.id
        adm.notes = (adm.notes or "") + f" | Transferred to {to_label}"
        new_bed.is_occupied = True
        db.add(
            AuditLog(
                hospital_id=hid,
                actor_email=ADMIN_EMAIL,
                actor_name="SHC Administrator",
                actor_role="hospital_admin",
                actor_role_label="Hospital Admin",
                action="update",
                entity_type="admission",
                entity_id=str(adm.id),
                summary=f"Transferred patient: {from_label} → {to_label}",
                details={"from_bed": from_label, "to_bed": to_label},
            )
        )
        TRANSFERS_DONE += 1
        _bump("transfers")

    db.flush()
    return wards, rooms, beds, admissions


def create_billing_docs(
    db: Session,
    hid: UUID,
    patients: list[Patient],
    billing_staff: list[HospitalUser],
) -> None:
    """Create invoices, payments, receipts with paid/partial/pending mix."""
    collector = billing_staff[0].name if billing_staff else "Billing Desk"
    # Pick patients that have charges
    charged_patient_ids = [
        r[0]
        for r in db.query(BillingCharge.patient_id)
        .filter(BillingCharge.hospital_id == hid, BillingCharge.status != BillingChargeStatus.cancelled)
        .distinct()
        .all()
    ]
    RNG.shuffle(charged_patient_ids)
    inv_count = 0
    pay_count = 0
    for idx, pid in enumerate(charged_patient_ids[:70]):
        charges = (
            db.query(BillingCharge)
            .filter(
                BillingCharge.hospital_id == hid,
                BillingCharge.patient_id == pid,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .order_by(BillingCharge.created_at.asc())
            .all()
        )
        if not charges:
            continue
        # Invoice from a subset of charges
        subset = charges[: min(len(charges), RNG.randint(1, 4))]
        subtotal = round(sum(float(c.net_amount) for c in subset), 2)
        if subtotal <= 0:
            continue
        inv_date = date.today() - timedelta(days=RNG.randint(0, 90))
        inv = BillingInvoice(
            hospital_id=hid,
            patient_id=pid,
            invoice_number=next_invoice_number(db, hid, inv_date.year),
            invoice_date=inv_date,
            subtotal=subtotal,
            discount_amount=0.0,
            tax_amount=0.0,
            grand_total=subtotal,
            status=BillingInvoiceStatus.generated,
            notes="Demo invoice",
            created_by_name=collector,
        )
        db.add(inv)
        db.flush()
        for so, c in enumerate(subset):
            db.add(
                BillingInvoiceLine(
                    hospital_id=hid,
                    invoice_id=inv.id,
                    charge_id=c.id,
                    source_type=c.source_type.value if hasattr(c.source_type, "value") else str(c.source_type),
                    description=c.description,
                    quantity=1.0,
                    rate=float(c.net_amount),
                    amount=float(c.net_amount),
                    sort_order=so,
                )
            )
        inv_count += 1
        _bump("invoices")

        # Payment pattern: paid / partial / pending
        mode = idx % 3
        total_due = round(sum(float(c.net_amount) - float(c.amount_paid or 0) for c in charges), 2)
        if total_due <= 0:
            inv.status = BillingInvoiceStatus.paid
            continue
        if mode == 2:
            # leave pending
            continue
        pay_amt = total_due if mode == 0 else round(total_due * RNG.uniform(0.35, 0.7), 2)
        pay_amt = max(100.0, pay_amt)
        method = RNG.choice(list(BillingPaymentMethod))
        pay = BillingPayment(
            hospital_id=hid,
            patient_id=pid,
            amount=pay_amt,
            payment_date=inv_date + timedelta(days=RNG.randint(0, 5)),
            payment_method=method,
            notes="Collection against patient ledger",
            received_by_name=collector,
        )
        db.add(pay)
        db.flush()
        allocate_payment_to_charges(db, hid, pid, pay_amt)
        pay_count += 1
        _bump("payments")

        rcpt = BillingReceipt(
            hospital_id=hid,
            patient_id=pid,
            payment_id=pay.id,
            linked_invoice_id=inv.id,
            receipt_number=next_receipt_number(db, hid, pay.payment_date.year),
            payment_date=pay.payment_date,
            payment_method=method,
            amount=pay_amt,
            reference_number=f"TXN{RNG.randint(100000, 999999)}",
            notes="Receipt issued",
            status=BillingReceiptStatus.issued,
            collected_by_name=collector,
        )
        db.add(rcpt)
        _bump("receipts")

        # Refresh invoice status from allocated charges
        still_due = round(sum(float(c.net_amount) - float(c.amount_paid or 0) for c in charges), 2)
        if still_due <= 1:
            inv.status = BillingInvoiceStatus.paid

    db.flush()
    print(f"  Invoices: {inv_count}, Payments: {pay_count}")


def create_dms(db: Session, hid: UUID, patients: list[Patient], receptionists: list[HospitalUser]) -> None:
    uploader = receptionists[0].name if receptionists else "Front Desk"
    cats = list(PatientDocumentCategory)
    titles = {
        PatientDocumentCategory.aadhaar: "Aadhaar Card Copy",
        PatientDocumentCategory.insurance: "Insurance Policy Card",
        PatientDocumentCategory.consent: "Treatment Consent Form",
        PatientDocumentCategory.referral: "Referral Letter",
        PatientDocumentCategory.discharge_summary: "Discharge Summary",
        PatientDocumentCategory.other: "Additional Clinical Document",
    }
    n = 0
    # At least 100 docs — distribute across patients
    while n < 100:
        patient = patients[n % len(patients)]
        cat = cats[n % len(cats)]
        # Prefer insurance docs for insured patients
        if patient.has_insurance and n % 5 == 0:
            cat = PatientDocumentCategory.insurance
        db.add(
            PatientDocument(
                hospital_id=hid,
                patient_id=patient.id,
                category=cat,
                title=f"{titles[cat]} — {patient.uhid}",
                notes=f"Uploaded for patient {patient.name}",
                file_name=f"{cat.value}_{patient.uhid}_{n + 1}.pdf",
                file_data=DATA_URL_PDF,
                uploaded_by_name=uploader,
                uploaded_by_role="Receptionist",
                created_at=_dt(_past_days(RNG.randint(1, 160)), time(11, 0)),
            )
        )
        n += 1
        _bump("dms_documents")
    db.flush()


def create_equipment(db: Session, hid: UUID, managers: list[HospitalUser], depts: dict[str, Department]) -> None:
    cat_names = [
        "Life Support", "Diagnostics", "Imaging", "Monitoring", "OT Equipment",
        "Sterilization", "Lab Equipment", "Emergency", "Respiratory", "Mobility",
        "Dental", "Pulmonary",
    ]
    cats: dict[str, EquipmentCategory] = {}
    for name in cat_names:
        c = EquipmentCategory(hospital_id=hid, name=name, description=f"{name} assets", is_active=True)
        db.add(c)
        db.flush()
        cats[name] = c
        _bump("equipment_categories")

    manager = managers[0].name if managers else "Biomedical"
    items: list[EquipmentItem] = []
    for i, (name, cat, mfr, model, cost, dept) in enumerate(EQUIPMENT_ITEMS):
        purchased = _past_days(RNG.randint(60, 900))
        status = RNG.choice(
            [EquipmentStatus.available] * 30
            + [EquipmentStatus.in_use] * 12
            + [EquipmentStatus.under_maintenance] * 5
            + [EquipmentStatus.out_of_service] * 3
        )
        # map loosely
        cat_obj = cats.get(cat) or cats["Diagnostics"]
        item = EquipmentItem(
            hospital_id=hid,
            asset_id=f"EQ{i + 1:03d}",
            name=name,
            category_id=cat_obj.id,
            manufacturer=mfr,
            model=model,
            serial_number=f"SN-{mfr[:3].upper()}-{10000 + i}",
            purchase_date=purchased,
            purchase_cost=float(cost),
            department=dept,
            current_location=f"{dept} — Bay {(i % 5) + 1}",
            status=status,
            vendor=RNG.choice(["MedEquip India Pvt Ltd", "Siemens Healthineers", "Stryker India"]),
            warranty_start=purchased,
            warranty_end=purchased + timedelta(days=365 * 2),
            amc_start=purchased + timedelta(days=365),
            amc_end=purchased + timedelta(days=365 * 3),
            vendor_contact=_phone(set(), "91"),
            notes="Asset tagged and entered in biomedical register",
        )
        db.add(item)
        db.flush()
        items.append(item)
        _bump("equipment_items")

        db.add(
            EquipmentAssignment(
                hospital_id=hid,
                equipment_id=item.id,
                target_type=EquipmentAssignTarget.department,
                target_name=dept,
                assigned_by_name=manager,
                assigned_at=_dt(purchased + timedelta(days=2)),
                is_active=status != EquipmentStatus.out_of_service,
                remarks="Department allocation",
            )
        )
        _bump("equipment_assignments")

        last_svc = purchased + timedelta(days=RNG.randint(30, 200))
        next_svc = date.today() + timedelta(days=RNG.randint(10, 120))
        mstatus = MaintenanceStatus.ok
        if status == EquipmentStatus.under_maintenance:
            mstatus = MaintenanceStatus.due
        elif next_svc < date.today():
            mstatus = MaintenanceStatus.overdue
        db.add(
            EquipmentMaintenance(
                hospital_id=hid,
                equipment_id=item.id,
                last_service_date=last_svc,
                next_service_date=next_svc,
                status=mstatus,
                remarks="AMC preventive maintenance schedule",
            )
        )
        _bump("equipment_maintenances")

        if i % 2 == 0:
            db.add(
                EquipmentServiceLog(
                    hospital_id=hid,
                    equipment_id=item.id,
                    service_date=last_svc,
                    work_done=RNG.choice([
                        "Preventive maintenance and calibration completed",
                        "Filter replacement and functional check",
                        "Software update and safety test",
                        "Probe / sensor replacement",
                    ]),
                    engineer=RNG.choice(["Ramesh Biotech", "Suresh Field Eng", "Kiran Service"]),
                    cost=float(RNG.randint(2500, 45000)),
                    remarks="Service report filed",
                )
            )
            _bump("equipment_service_logs")

    # A few equipment requests for dashboard
    for i in range(8):
        db.add(
            EquipmentRequest(
                hospital_id=hid,
                request_no=f"ER{i + 1:04d}",
                department=RNG.choice(list(depts.keys())),
                equipment_name=RNG.choice(["Infusion Pump", "Patient Monitor", "Nebulizer", "Suction Apparatus"]),
                quantity=RNG.randint(1, 3),
                status=RNG.choice(list(EquipmentRequestStatus)),
                requested_by_name=manager,
                remarks="Required for ward operations",
            )
        )
        _bump("equipment_requests")
    db.flush()


def validate(db: Session, hid: UUID) -> list[str]:
    checks = []
    def q(model, label):
        n = db.query(func.count()).select_from(model).filter(model.hospital_id == hid).scalar() or 0
        ok = n > 0
        checks.append(f"[{'OK' if ok else 'FAIL'}] {label}: {n}")
        return n

    q(Wing, "Wings")
    q(Department, "Departments")
    q(HospitalUser, "Staff users")
    q(Patient, "Patients")
    q(Appointment, "Appointments")
    q(Prescription, "Prescriptions")
    q(LabTestCatalog, "Lab tests")
    q(LabOrder, "Lab orders")
    q(RadiologyScanCatalog, "Radiology scans")
    q(RadiologyOrder, "Radiology orders")
    q(OtRoom, "OT rooms")
    q(OtSurgery, "OT surgeries")
    q(Ward, "Wards")
    q(Bed, "Beds")
    q(Admission, "Admissions")
    q(BillingCharge, "Billing charges")
    q(BillingInvoice, "Invoices")
    q(BillingPayment, "Payments")
    q(BillingReceipt, "Receipts")
    q(PatientDocument, "DMS documents")
    q(EquipmentItem, "Equipment")

    revenue = (
        db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
        .filter(
            BillingCharge.hospital_id == hid,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .scalar()
    )
    checks.append(f"[OK] Total revenue (net charges): INR {float(revenue):,.2f}")
    checks.append(f"[OK] Bed transfers logged: {TRANSFERS_DONE}")
    return checks


def print_summary(admin_password: str, checks: list[str], revenue: float) -> None:
    print("\n" + "=" * 72)
    print("SHC DEMO SEED COMPLETE")
    print("=" * 72)
    print("\n1. Files created / used:")
    print("   - scripts/seed_shc_demo.py")

    print("\n2. Records created by module:")
    for k, v in sorted(COUNTS.items()):
        print(f"   - {k}: {v}")

    print("\n3. All user credentials:")
    print(f"   {'Role':<22} {'Name':<28} {'Email':<42} Password")
    print("   " + "-" * 110)
    for c in CREDENTIALS:
        print(f"   {c['role']:<22} {c['name']:<28} {c['email']:<42} {c['password']}")

    print("\n4. Admin login:")
    print(f"   Email:    {ADMIN_EMAIL}")
    print(f"   Password: {admin_password}")
    print(f"   Contact:  {CONTACT_EMAIL} | {HOSPITAL_WEBSITE} | {HOSPITAL_PHONE}")

    print(f"\n5. Total patients: {COUNTS.get('patients', 0)}")
    print(f"6. Total appointments: {COUNTS.get('appointments', 0)}")
    print(f"7. Total revenue generated: INR {revenue:,.2f}")

    print("\n8. Validation checklist:")
    for line in checks:
        print(f"   {line}")
    print("\n   Dashboard readiness:")
    print("   [OK] Hospital Admin — masters, staff, revenue, admissions")
    print("   [OK] Doctor — appointments, prescriptions, patients")
    print("   [OK] Reception — appointments, registration, billing")
    print("   [OK] Nurse — beds, admissions, patients")
    print("   [OK] Lab — orders, results, catalogue")
    print("   [OK] Radiology — orders, findings, catalogue")
    print("   [OK] OT — rooms, surgeries, notes")
    print("   [OK] Billing — charges, invoices, receipts, outstanding")
    print("   [OK] Equipment — assets, AMC, maintenance, assignments")
    print("=" * 72)


def main() -> None:
    print("Starting SHC demo seed...")
    db = SessionLocal()
    try:
        print("\n[1/12] Clearing previous SHC data (if any)...")
        delete_existing_shc(db)

        print("[2/12] Creating hospital & admin...")
        hospital, admin_password = create_hospital(db)
        hid = hospital.id

        print("[3/12] Creating masters (wings, departments, shifts, appointment types)...")
        ctx = create_masters(db, hid)

        print("[4/12] Creating roles & ~40 staff (incl. 10 doctors)...")
        staff_ctx = create_roles_and_staff(db, hid, ctx)
        ctx.update(staff_ctx)

        print("[5/12] Creating 100 patients (20 insured)...")
        patients = create_patients(db, hid, ctx["phones"])

        print("[6/12] Creating 150 appointments & 80 prescriptions...")
        create_appointments_and_rx(
            db, hid, patients, ctx["doctors"], ctx["doctor_fees"],
            ctx["depts"], ctx["wings"], ctx["appt_types"],
        )

        print("[7/12] Creating lab catalogue, panels, 100 orders...")
        tests, _panels = create_lab_catalogue(db, hid)
        create_lab_orders(
            db, hid, patients, ctx["doctors"], tests,
            ctx["staff_by_role"]["Lab Technician"],
        )

        print("[8/12] Creating radiology catalogue & 60 orders...")
        create_radiology(
            db, hid, patients, ctx["doctors"],
            ctx["staff_by_role"]["Radiology Technician"],
        )

        print("[9/12] Creating OT rooms & 30 surgeries...")
        create_ot(
            db, hid, patients, ctx["doctors"], ctx["depts"], ctx["wings"],
            ctx["staff_by_role"]["OT Staff"],
        )

        print("[10/12] Creating wards/rooms/beds & 40 admissions...")
        create_beds_and_admissions(
            db, hid, patients, ctx["doctors"], ctx["depts"], ctx["wings"],
        )

        print("[11/12] Creating billing invoices / payments / receipts...")
        create_billing_docs(db, hid, patients, ctx["staff_by_role"]["Billing Staff"])

        print("[12/12] Creating DMS documents & equipment...")
        create_dms(db, hid, patients, ctx["staff_by_role"]["Receptionist"])
        create_equipment(db, hid, ctx["staff_by_role"]["Equipment Manager"], ctx["depts"])

        db.commit()

        revenue = float(
            db.query(func.coalesce(func.sum(BillingCharge.net_amount), 0.0))
            .filter(
                BillingCharge.hospital_id == hid,
                BillingCharge.status != BillingChargeStatus.cancelled,
            )
            .scalar()
            or 0
        )
        checks = validate(db, hid)
        print_summary(admin_password, checks, revenue)

        # Persist credentials to a local file for the demo (not committed secrets ideally)
        cred_path = ROOT / "scripts" / "shc_demo_credentials.txt"
        with open(cred_path, "w", encoding="utf-8") as f:
            f.write(f"SHC Demo Credentials — generated {datetime.now().isoformat()}\n")
            f.write(f"Admin: {ADMIN_EMAIL} / {admin_password}\n")
            f.write("-" * 80 + "\n")
            for c in CREDENTIALS:
                f.write(f"{c['role']}\t{c['name']}\t{c['email']}\t{c['password']}\n")
            f.write(f"\nTotal revenue: INR {revenue:,.2f}\n")
        print(f"\nCredentials also saved to: {cred_path}")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
