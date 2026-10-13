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
