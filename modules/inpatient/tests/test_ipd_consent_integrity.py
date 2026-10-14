"""Integration tests for IPD Form and Consent Document Integrity (Phase 3).

Verifies:
1. Self-consent finalization sets canonical patient identity and succeeds.
2. Representative/guardian consent missing name or relationship fails on finalization.
3. Draft allows partial representative info, but finalization strictly fails closed.
4. Client attempts to tamper with patient demographics in form_data are overwritten by canonical DB values.
5. Finalized submissions are immutable.
6. Server-rendered authoritative HTML snapshot is produced and stored.
"""

from datetime import datetime, timezone
from uuid import uuid4
import pytest

from modules.inpatient.actions.ipd_actions import (
    CreateFormSubmissionAction,
    UpdateFormSubmissionAction,
)
from modules.inpatient.contracts.inpatient_contracts import (
    IpdFormSubmissionCreate,
    IpdFormSubmissionStatus,
    IpdFormSubmissionUpdate,
)
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.patients.entities.patient import Patient
from shared.exceptions.base import ValidationError

ACTOR = {"name": "Dr. Sarah Clinician", "role": "doctor"}


def _setup_patient_and_admission(db_session, hospital):
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid=f"UHID-{uuid4().hex[:6].upper()}",
        first_name="Robert",
        last_name="Bruce",
        name="Robert Bruce",
        gender="male",
        mobile="9876543210",
    )
    db_session.add(patient)
    db_session.commit()

    admission = Admission(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        status=AdmissionStatus.admitted,
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)
    db_session.commit()
    return patient, admission


def test_self_consent_finalization_succeeds(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    sub = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="general_consent_1",
            form_title="General Consent Form",
            form_schema_version=2,
            signer_type="patient",
            signer_name="Robert Bruce",
            form_data={
                "treatment_scope": "Full inpatient medical care",
                "consent.patient_acknowledgement": "Agreed",
            },
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )

    assert sub.status == IpdFormSubmissionStatus.final
    assert sub.signer_type == "patient"
    assert sub.signer_name == "Robert Bruce"
    assert sub.signer_relationship == "Self"
    assert sub.finalized_at is not None
    assert sub.form_schema_version == 2
    assert sub.rendered_html is not None
    assert "Robert Bruce" in sub.rendered_html
    assert patient.uhid in sub.rendered_html
    assert "Self" in sub.rendered_html


def test_representative_consent_requires_name_and_relationship_on_finalization(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # 1. Draft submission with representative but missing relationship succeeds as draft
    draft = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="general_consent_1",
            form_title="General Consent Form",
            form_schema_version=2,
            signer_type="representative",
            signer_name="Mary Bruce",
            signer_relationship="",  # Missing
            form_data={"treatment_scope": "Surgery"},
            status=IpdFormSubmissionStatus.draft,
        ),
        ACTOR,
    )
    assert draft.status == IpdFormSubmissionStatus.draft
    assert draft.signer_type == "representative"

    # 2. Finalization without relationship fails closed
    with pytest.raises(ValidationError, match="relationship to patient"):
        UpdateFormSubmissionAction(db_session).execute(
            hospital.id,
            draft.id,
            IpdFormSubmissionUpdate(
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )

    # 3. Finalization without signer name also fails closed
    with pytest.raises(ValidationError, match="signer name"):
        UpdateFormSubmissionAction(db_session).execute(
            hospital.id,
            draft.id,
            IpdFormSubmissionUpdate(
                signer_name="",
                signer_relationship="Mother",
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )

    # 4. Supplying both valid representative name & relationship succeeds on finalization
    finalized = UpdateFormSubmissionAction(db_session).execute(
        hospital.id,
        draft.id,
        IpdFormSubmissionUpdate(
            signer_name="Mary Bruce",
            signer_relationship="Mother",
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )
    assert finalized.status == IpdFormSubmissionStatus.final
    assert finalized.signer_type == "representative"
    assert finalized.signer_name == "Mary Bruce"
    assert finalized.signer_relationship == "Mother"
    assert finalized.finalized_at is not None
    assert "Mary Bruce" in finalized.rendered_html
    assert "Mother" in finalized.rendered_html


def test_tampered_patient_demographics_overwritten_by_canonical_db_truth(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # Client tries to tamper with UHID, patient name, and admission ID in payload form_data
    sub = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="admission_assessment",
            form_title="Admission Assessment",
            form_schema_version=2,
            form_data={
                "uhid": "FAKE-UHID-9999",
                "name": "Tampered Impostor",
                "clinical_notes": "Patient presents with cough",
            },
            status=IpdFormSubmissionStatus.draft,
        ),
        ACTOR,
    )

    # Server must have overwritten the tampered identity fields with the canonical DB patient values
    assert sub.form_data["uhid"] == patient.uhid
    assert sub.form_data["name"] == "Robert Bruce"
    assert sub.form_data["clinical_notes"] == "Patient presents with cough"


def test_finalized_form_submission_is_immutable(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    finalized = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="general_consent_1",
            form_title="General Consent Form",
            signer_type="patient",
            signer_name="Robert Bruce",
            status=IpdFormSubmissionStatus.final,
            form_data={"procedure": "Appendectomy"},
        ),
        ACTOR,
    )

    with pytest.raises(ValidationError, match="Finalized clinical forms cannot be edited"):
        UpdateFormSubmissionAction(db_session).execute(
            hospital.id,
            finalized.id,
            IpdFormSubmissionUpdate(
                form_data={"procedure": "Tampered modification"},
            ),
            ACTOR,
        )


def test_self_consent_patient_name_mismatch_fails_closed(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # 1. Signer name mismatch when signing as patient
    with pytest.raises(ValidationError, match="Patient name does not match the selected patient"):
        CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="general_consent_1",
                form_title="General Consent Form",
                form_schema_version=2,
                signer_type="patient",
                signer_name="John Impostor",
                form_data={"procedure": "Appendectomy"},
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )

    # 2. Attestation field in form_data mismatch when signing as patient
    with pytest.raises(ValidationError, match="Patient name does not match the selected patient"):
        CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="lama_form",
                form_title="LAMA Undertaking",
                form_schema_version=2,
                signer_type="patient",
                signer_name="Robert Bruce",
                form_data={
                    "lama.patient_name": "Different Name",
                    "reason": "Leaving against advice",
                },
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )


def test_lama_representative_signer_succeeds_without_name_match(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # Representative signer with legitimate relationship must NOT be blocked merely because rep != patient
    sub = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="lama_form",
            form_title="LAMA Form",
            form_schema_version=2,
            signer_type="representative",
            signer_name="Alexander Bruce",
            signer_relationship="Father",
            form_data={
                "reason": "Family relocation",
                "lama.patient_name": "Robert Bruce",
            },
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )

    assert sub.status == IpdFormSubmissionStatus.final
    assert sub.signer_type == "representative"
    assert sub.signer_name == "Alexander Bruce"
    assert sub.signer_relationship == "Father"
    assert "Alexander Bruce" in sub.rendered_html
    assert "Father" in sub.rendered_html


def test_missing_typed_patient_attestation_blocks_finalization(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # Patient self-attestation without typed name must NOT be auto-filled by server
    with pytest.raises(ValidationError, match="requires the patient's name to be entered"):
        CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="general_consent_1",
                form_title="General Consent Form",
                form_schema_version=3,
                signer_type="patient",
                signer_name="",  # Not typed
                form_data={"procedure": "Appendectomy"},
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )


def test_lama_form_real_ui_serialization_matrix(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # A. lama_form — self + correct name in real UI fields -> PASS
    sub_self = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="lama_form",
            form_title="LAMA Form",
            form_schema_version=3,
            form_data={
                "lama_form.signer_type": "patient",
                "lama_form.patient_attestation_name": "Robert Bruce",
                "lama_form.signer_name": "Robert Bruce",
                "lama_form.signatures.doctor.name": "Dr. Smith",
                "lama_form.signatures.witness.name": "Officer Davies",
                "reason": "Personal reasons",
            },
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )
    assert sub_self.status == IpdFormSubmissionStatus.final
    assert sub_self.signer_type == "patient"
    assert sub_self.signer_name == "Robert Bruce"

    # B. lama_form — self + wrong patient name -> BLOCK
    with pytest.raises(ValidationError, match="Patient name does not match the selected patient"):
        CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="lama_form",
                form_title="LAMA Form",
                form_schema_version=3,
                form_data={
                    "lama_form.signer_type": "patient",
                    "lama_form.patient_attestation_name": "Impostor Name",
                    "reason": "Personal reasons",
                },
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )

    # C. lama_form — representative + different name + relationship -> PASS
    sub_rep = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="lama_form",
            form_title="LAMA Form",
            form_schema_version=3,
            form_data={
                "lama_form.signer_type": "representative",
                "lama_form.signer_name": "Mary Bruce",
                "lama_form.signer_relationship": "Spouse",
                "lama_form.patient_attestation_name": "Robert Bruce",
                "lama_form.signatures.doctor.name": "Dr. Sarah Clinician",
                "lama_form.signatures.witness.name": "Witness Jane",
                "reason": "Seeking second opinion",
            },
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )
    assert sub_rep.status == IpdFormSubmissionStatus.final
    assert sub_rep.signer_type == "representative"
    assert sub_rep.signer_name == "Mary Bruce"
    assert sub_rep.signer_relationship == "Spouse"

    # D. lama_form — representative without relationship -> BLOCK
    with pytest.raises(ValidationError, match="relationship to patient"):
        CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="lama_form",
                form_title="LAMA Form",
                form_schema_version=3,
                form_data={
                    "lama_form.signer_type": "representative",
                    "lama_form.signer_name": "Mary Bruce",
                    "lama_form.signer_relationship": "",  # Missing
                    "reason": "Seeking second opinion",
                },
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )


def test_lama_simple_and_eng_attestation_contract(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # lama_simple — self + correct name -> PASS
    sub_simple = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="lama_simple",
            form_title="LAMA Simple",
            form_schema_version=3,
            form_data={
                "lama_simple.signer_type": "patient",
                "lama_simple.patient_attestation_name": "Robert Bruce",
                "lama_simple.signer_name": "Robert Bruce",
                "lama_simple.doctor_name": "Dr. Strange",
            },
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )
    assert sub_simple.status == IpdFormSubmissionStatus.final

    # lama_eng — representative + valid info -> PASS
    sub_eng = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="lama_eng",
            form_title="LAMA Undertaking (English)",
            form_schema_version=3,
            form_data={
                "lama_eng.signer_type": "representative",
                "lama_eng.signer_name": "James Bruce",
                "lama_eng.signer_relationship": "Brother",
                "lama_eng.patient_attestation_name": "Robert Bruce",
                "lama_eng.doctor_name": "Dr. Banner",
            },
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )
    assert sub_eng.status == IpdFormSubmissionStatus.final
    assert sub_eng.signer_name == "James Bruce"


def test_non_lama_consent_attestation_contract(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # dnr_consent — self attestation with matching name -> PASS
    sub_dnr = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="dnr_consent",
            form_title="Do Not Resuscitate Consent",
            form_schema_version=3,
            form_data={
                "dnr_consent.signer_type": "patient",
                "dnr_consent.signer_name": "Robert Bruce",
                "dnr_consent.discussion_date": "2026-10-13",
            },
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )
    assert sub_dnr.status == IpdFormSubmissionStatus.final
    assert sub_dnr.signer_name == "Robert Bruce"


def test_unverified_localized_telugu_template_fails_closed_on_finalization(db_session, hospital):
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # 1. Finalization of unverified Telugu template must FAIL CLOSED
    with pytest.raises(ValidationError, match="unverified translation template"):
        CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="lama_tel",
                form_title="వైద్యుల సలహాకు వ్యతిరేకంగా వెళ్ళడం",
                form_schema_version=3,
                form_data={
                    "lama_tel.signer_type": "patient",
                    "lama_tel.signer_name": "Robert Bruce",
                },
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )

    # 2. Saving as draft is ALLOWED for operational and clinical continuity
    draft = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="lama_tel",
            form_title="వైద్యుల సలహాకు వ్యతిరేకంగా వెళ్ళడం",
            form_schema_version=3,
            form_data={
                "lama_tel.signer_type": "patient",
                "lama_tel.signer_name": "Robert Bruce",
            },
            status=IpdFormSubmissionStatus.draft,
        ),
        ACTOR,
    )
    assert draft.status == IpdFormSubmissionStatus.draft
    assert draft.form_id == "lama_tel"


def test_turbt_3_unverified_localized_fails_closed(db_session, hospital):
    """Proves turbt_3 (Telugu without _tel suffix) fails closed on finalization."""
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    with pytest.raises(ValidationError, match="unverified translation template"):
        CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="turbt_3",
                form_title="Informed Consent for TURBT -3 (Telugu)",
                form_schema_version=3,
                form_data={
                    "turbt_3.signer_type": "patient",
                    "turbt_3.signer_name": "Robert Bruce",
                },
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )

    # Draft must succeed
    draft = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="turbt_3",
            form_title="Informed Consent for TURBT -3 (Telugu)",
            form_schema_version=3,
            form_data={
                "turbt_3.signer_type": "patient",
                "turbt_3.signer_name": "Robert Bruce",
            },
            status=IpdFormSubmissionStatus.draft,
        ),
        ACTOR,
    )
    assert draft.status == IpdFormSubmissionStatus.draft
    assert draft.form_id == "turbt_3"


def test_strict_name_equality_no_fuzzy_or_token_overlap(db_session, hospital):
    """
    Section 18:
    Registered: Balaji Mukesh
    balaji mukesh -> PASS
      Balaji   Mukesh   -> PASS
    Balaji -> FAIL
    Mukesh -> FAIL
    Balaji Kumar -> FAIL
    Balaji Mukesh Rao -> FAIL
    John Mukesh -> FAIL
    """
    from datetime import date
    patient = Patient(
        hospital_id=hospital.id,
        uhid="UHID-BALAJI-01",
        name="Balaji Mukesh",
        gender="male",
        mobile="9876543210",
        date_of_birth=date(1985, 5, 20),
    )
    db_session.add(patient)
    db_session.commit()
    db_session.refresh(patient)

    admission = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        status="admitted",
    )
    db_session.add(admission)
    db_session.commit()
    db_session.refresh(admission)

    # Case-insensitive & whitespace-collapsed equality -> PASS
    for valid_name in ["balaji mukesh", "  Balaji   Mukesh  "]:
        sub = CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="general_consent_1",
                form_title="General Consent Form",
                form_schema_version=3,
                signer_type="patient",
                signer_name=valid_name,
                form_data={"procedure": "Appendectomy"},
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )
        assert sub.status == IpdFormSubmissionStatus.final

    # Partial / Token overlap / Fuzzy mismatches -> MUST FAIL CLOSED
    invalid_names = [
        "Balaji",
        "Mukesh",
        "Balaji Kumar",
        "Balaji Mukesh Rao",
        "John Mukesh",
    ]
    for invalid_name in invalid_names:
        with pytest.raises(ValidationError, match="Patient name does not match"):
            CreateFormSubmissionAction(db_session).execute(
                hospital.id,
                IpdFormSubmissionCreate(
                    patient_id=patient.id,
                    admission_id=admission.id,
                    form_id="general_consent_1",
                    form_title="General Consent Form",
                    form_schema_version=3,
                    signer_type="patient",
                    signer_name=invalid_name,
                    form_data={"procedure": "Appendectomy"},
                    status=IpdFormSubmissionStatus.final,
                ),
                ACTOR,
            )


def test_allow_representative_policy_flag_enforced(db_session, hospital, monkeypatch):
    """Enforces policy flag allow_representative=False rejects representative signer."""
    from modules.inpatient.services.clinical_document_service import IPD_FORM_POLICIES
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    # Temporarily set allow_representative=False for general_consent_1
    monkeypatch.setitem(
        IPD_FORM_POLICIES,
        "general_consent_1",
        {
            "requires_attestation": True,
            "allow_representative": False,
            "requires_typed_patient_name": True,
            "requires_relationship_for_representative": True,
            "requires_witness": False,
        },
    )

    with pytest.raises(ValidationError, match="does not permit representative signature"):
        CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="general_consent_1",
                form_title="General Consent Form",
                form_schema_version=3,
                signer_type="representative",
                signer_name="Mary Bruce",
                signer_relationship="Mother",
                form_data={"procedure": "Appendectomy"},
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )


def test_requires_witness_policy_flag_enforced(db_session, hospital, monkeypatch):
    """Enforces policy flag requires_witness=True checks witness info at finalization."""
    from modules.inpatient.services.clinical_document_service import IPD_FORM_POLICIES
    patient, admission = _setup_patient_and_admission(db_session, hospital)

    monkeypatch.setitem(
        IPD_FORM_POLICIES,
        "general_consent_1",
        {
            "requires_attestation": True,
            "allow_representative": True,
            "requires_typed_patient_name": True,
            "requires_relationship_for_representative": True,
            "requires_witness": True,
        },
    )

    # 1. Missing witness information blocks finalization
    with pytest.raises(ValidationError, match="requires witness information"):
        CreateFormSubmissionAction(db_session).execute(
            hospital.id,
            IpdFormSubmissionCreate(
                patient_id=patient.id,
                admission_id=admission.id,
                form_id="general_consent_1",
                form_title="General Consent Form",
                form_schema_version=3,
                signer_type="patient",
                signer_name="Robert Bruce",
                form_data={"procedure": "Appendectomy"},
                status=IpdFormSubmissionStatus.final,
            ),
            ACTOR,
        )

    # 2. Supplying witness allows finalization
    sub = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="general_consent_1",
            form_title="General Consent Form",
            form_schema_version=3,
            signer_type="patient",
            signer_name="Robert Bruce",
            form_data={
                "procedure": "Appendectomy",
                "general_consent_1.witness_name": "Nurse Sarah Jenkins",
            },
            status=IpdFormSubmissionStatus.final,
        ),
        ACTOR,
    )
    assert sub.status == IpdFormSubmissionStatus.final



