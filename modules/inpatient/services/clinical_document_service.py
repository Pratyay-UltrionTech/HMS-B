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


# Localization Provenance Registry
# Medico-legal localized forms must have an authoritative hospital-approved source.
# When unverified, hospital safety requires failing closed (blocking finalization/signing)
# while allowing viewing and drafting, directing staff to approved English form or master upload.
IPD_LOCALIZATION_PROVENANCE: dict[str, dict[str, Any]] = {
    "anaesthesia_consent_tel_front": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "anaesthesia_consent_tel_back": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "blood_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "intubation_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "hemodialysis_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "endoscopy_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "sedation_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "restraint_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "ct_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "lama_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "contrast_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "invasive_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "mtp_consent_tel": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "general_consent_tel_1": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "general_consent_tel_2": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "blood_transfusion_tel_1": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "blood_transfusion_tel_2": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "burr_hole_tel_1": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "burr_hole_tel_2": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "burr_hole_tel_3": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
    "turbt_3": {"language": "te", "verification_status": "unverified", "source": "None - unverified localization"},
}

# Central Canonical Backend IPD Form Policy Registry
IPD_FORM_POLICIES: dict[str, dict[str, Any]] = {
    "general_consent_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "general_consent_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "anaesthesia_consent_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "anaesthesia_consent_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "anaesthesia_consent_tel_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "anaesthesia_consent_tel_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "blood_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "blood_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surgery_consent_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surgery_consent_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surgery_consent_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "intubation_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "intubation_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "angiography_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "angioplasty_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "ascitic_tapping_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "hemodialysis_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "hemodialysis_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "endoscopy_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "endoscopy_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "sedation_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "sedation_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lama_form": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lama_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "high_risk_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "tracheostomy_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "billing_estimation": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "billing_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "re_estimation": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "restraint_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "restraint_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "ct_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "ct_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "billing_consent_rates": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "angiogram_cag": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "crt_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "dsa_brain": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "cervical_laminectomy_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "cervical_laminectomy_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "laparotomy_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "laparotomy_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "laparotomy_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "dnr_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surgery_procedure_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surgery_procedure_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lama_simple": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lama_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lama_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "contrast_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "contrast_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "invasive_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "invasive_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "mtp_consent_eng": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "mtp_consent_tel": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "tpa_stroke_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "amputation_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "diabetic_foot_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "debridement_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "dj_stent_removal_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "viu_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "pacemaker_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "high_risk_consent_icu": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "icd_device_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "intercostal_drain_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "iron_iv_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lumbar_puncture_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "pleurocentesis_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "rirs_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "rib_fracture_fixation_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "skin_grafting_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "sterilization_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "thoracic_surgery_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "thrombolysis_heart_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "tracheostomy_consent_perc": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "vascular_embolectomy_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "vascular_bypass_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "induction_of_labour_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "cabg_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surrogate_counselling_form": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "iccu_billing_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surgical_billing_estimation": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "insurance_undertaking_letter": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "physiotherapy_consent_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "physiotherapy_consent_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "physiotherapy_consent_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lscs_consent_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lscs_consent_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "urology_consent_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "urology_consent_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "arthroscopic_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "arthroscopic_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "pcnl_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "pcnl_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "general_surgical_hr_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "general_surgical_hr_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "general_surgical_hr_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "icu_admission_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "icu_admission_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "ent_consent_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "ent_consent_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "turbt_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "turbt_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "turbt_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "ivc_filter_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "ivc_filter_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lap_open_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lap_open_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lap_open_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "turp_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "turp_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "trauma_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "trauma_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "dsa_brain_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "dsa_brain_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "dsa_brain_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "laminectomy_discectomy_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "laminectomy_discectomy_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "haematoma_evac_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "haematoma_evac_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lp_shunt_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lp_shunt_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "lp_shunt_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "general_consent_tel_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "general_consent_tel_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "laparoscopic_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "laparoscopic_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "laparoscopic_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "list_new_consents_1": {
        "requires_attestation": False,
        "allow_representative": False,
        "requires_typed_patient_name": False,
        "requires_witness": False,
    },
    "list_new_consents_2": {
        "requires_attestation": False,
        "allow_representative": False,
        "requires_typed_patient_name": False,
        "requires_witness": False,
    },
    "list_new_consents_3": {
        "requires_attestation": False,
        "allow_representative": False,
        "requires_typed_patient_name": False,
        "requires_witness": False,
    },
    "pedicle_screw_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "pedicle_screw_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surgical_ortho_1": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surgical_ortho_2": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "surgical_ortho_3": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "urs_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "urs_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "djs_front": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "djs_back": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "nephrectomy_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "heart_valve_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "vascular_elva": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
    "bronchoscopy_consent": {
        "requires_attestation": True,
        "allow_representative": True,
        "requires_typed_patient_name": True,
        "requires_relationship_for_representative": True,
        "requires_witness": False,
    },
}


class ClinicalDocumentService:
    def __init__(self, db: Session, hospital_id: UUID) -> None:
        self.db = db
        self.hospital_id = hospital_id

    def is_consent_form(self, form_id: str, form_title: str) -> bool:
        if form_id in IPD_FORM_POLICIES:
            return IPD_FORM_POLICIES[form_id].get("requires_attestation", True)
        fid = form_id.lower()
        title = form_title.lower()
        keywords = ("consent", "lama", "refusal", "declaration", "attestation", "undertaking", "dnr")
        return any(k in fid or k in title for k in keywords)

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
        If the form is a consent/attestation form and is being finalized:
        - Rejects finalization if the template is an unverified localized version.
        - If signer is patient/self: enforces that typed name matches registered patient name (normalized) without auto-filling.
        - If signer is representative: enforces valid signer name and relationship.
        """
        canonical_p_name = canonical_patient.get("name") or ""

        def _norm_name(s: str | None) -> str:
            if not s:
                return ""
            return " ".join(s.strip().split()).lower()

        # 1. Localization safety check: unverified localized medico-legal forms fail closed on finalization
        loc = IPD_LOCALIZATION_PROVENANCE.get(form_id)
        if is_final and loc and loc.get("verification_status") == "unverified":
            raise ValidationError(
                f"Localized medico-legal form '{form_id}' uses an unverified translation template. "
                "Hospital policy requires a verified hospital-approved master translation before finalization or signing. "
                "Please use the approved English counterpart or contact the administrator."
            )

        if not self.is_consent_form(form_id, form_title):
            return (
                explicit_signer_type or "patient",
                explicit_signer_name or canonical_p_name,
                explicit_signer_rel or "Self",
            )

        policy = IPD_FORM_POLICIES.get(form_id) or {
            "requires_attestation": True,
            "allow_representative": True,
            "requires_typed_patient_name": True,
            "requires_relationship_for_representative": True,
            "requires_witness": False,
        }

        # Look in form-specific prefix first, then explicit arguments, then generic fallback keys
        signer_type = (
            form_data.get(f"{form_id}.signer_type")
            or explicit_signer_type
            or form_data.get("consent.signer_type")
            or form_data.get("signer_type")
            or form_data.get("lama.signer_type")
            or "patient"
        ).strip().lower()

        signer_name = (
            form_data.get(f"{form_id}.signer_name")
            or explicit_signer_name
            or form_data.get("consent.signer_name")
            or form_data.get("signer_name")
            or form_data.get("lama.signer_name")
            or ""
        ).strip()

        signer_rel = (
            form_data.get(f"{form_id}.signer_relationship")
            or explicit_signer_rel
            or form_data.get("consent.signer_relationship")
            or form_data.get("signer_relationship")
            or form_data.get("lama.signer_relationship")
            or ""
        ).strip()

        attestation_name = (
            form_data.get(f"{form_id}.patient_attestation_name")
            or form_data.get("consent.patient_attestation_name")
            or form_data.get("patient_attestation_name")
            or form_data.get("lama.patient_name")
            or form_data.get(f"{form_id}.patient_name")
            or form_data.get("attestation.patient_name")
            or form_data.get("patient_declaration_name")
            or ""
        ).strip()

        if signer_type in ("patient", "self"):
            signer_type = "patient"
            if not signer_rel:
                signer_rel = "Self"

            if is_final:
                # 1. Attestation required check: Do NOT silently auto-fill missing attestation
                requires_typed = policy.get("requires_typed_patient_name", True)
                effective_entered_name = signer_name or attestation_name
                if requires_typed and not effective_entered_name:
                    raise ValidationError(
                        "Patient self-attestation requires the patient's name to be entered in the signature/declaration field."
                    )

                # 2. Strict normalized equality check (no token-overlap, no fuzzy matching)
                if signer_name and canonical_p_name:
                    if _norm_name(signer_name) != _norm_name(canonical_p_name):
                        raise ValidationError(
                            f"Patient name does not match the selected patient. "
                            f"Please enter the patient's name as registered: {canonical_p_name}"
                        )
                # 3. Attestation field validation against canonical registered patient
                if attestation_name and canonical_p_name:
                    if _norm_name(attestation_name) != _norm_name(canonical_p_name):
                        raise ValidationError(
                            f"Patient name does not match the selected patient. "
                            f"Please enter the patient's name as registered: {canonical_p_name}"
                        )

            # If draft only, provide fallback signer_name for view if empty (NEVER auto-fill finalized)
            if not is_final and not signer_name and canonical_p_name:
                signer_name = canonical_p_name

        else:
            # Representative / Guardian path
            if is_final:
                # Enforce allow_representative policy flag
                if not policy.get("allow_representative", True):
                    raise ValidationError(
                        f"Form '{form_id}' does not permit representative signature; patient self-attestation is required."
                    )

                if not signer_name:
                    raise ValidationError(
                        "Consent finalization requires the signer name when signer is a representative/guardian."
                    )

                # Enforce requires_relationship_for_representative policy flag
                if policy.get("requires_relationship_for_representative", True) and not signer_rel:
                    raise ValidationError(
                        "Consent finalization requires the relationship to patient when signer is a representative/guardian."
                    )

                # If declaration has a separate patient name field, validate that it refers to the patient
                if attestation_name and canonical_p_name:
                    if _norm_name(attestation_name) != _norm_name(canonical_p_name):
                        raise ValidationError(
                            f"Patient name does not match the selected patient. "
                            f"Please enter the patient's name as registered: {canonical_p_name}"
                        )

        # Enforce requires_witness policy flag at finalization
        if is_final and policy.get("requires_witness", False):
            witness_name = (
                form_data.get(f"{form_id}.witness_name")
                or form_data.get(f"{form_id}.witness")
                or form_data.get(f"{form_id}.witness_signature")
                or form_data.get("witness_name")
                or form_data.get("witness.name")
                or form_data.get("lama.witness_name")
                or form_data.get("witness")
                or ""
            )
            if not str(witness_name).strip():
                raise ValidationError(
                    "Consent finalization requires witness information to be recorded."
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
