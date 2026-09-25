"""Safety regressions for documents owned by a specific inpatient episode."""

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from modules.inpatient.actions.admission_actions import to_admission_detail
from modules.inpatient.actions.ipd_actions import (
    CreateFormSubmissionAction,
    ListFormSubmissionsAction,
    UpdateFormSubmissionAction,
)
from modules.inpatient.contracts.inpatient_contracts import (
    IpdFormSubmissionCreate,
    IpdFormSubmissionUpdate,
)
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.patients.entities.patient import Patient
from shared.exceptions.base import ValidationError


ACTOR = {"name": "Test clinician", "role": "doctor"}


def _patient(db_session, hospital):
    patient = Patient(
        id=uuid4(),
        hospital_id=hospital.id,
        uhid=f"U{uuid4().hex[:8]}",
        first_name="Alex",
        last_name="Test",
        name="Alex Test",
        mobile="9876500000",
    )
    db_session.add(patient)
    db_session.commit()
    return patient


def _admission(db_session, hospital, patient, status):
    admission = Admission(
        id=uuid4(),
        hospital_id=hospital.id,
        patient_id=patient.id,
        status=status,
        admitted_at=datetime.now(timezone.utc),
    )
    db_session.add(admission)
    db_session.commit()
    return admission


def _draft(db_session, hospital, patient, admission):
    return CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            admission_id=admission.id,
            form_id="doctor_notes",
            form_title="Doctor Notes",
            form_data={"note": "Admission A"},
        ),
        ACTOR,
    )


def test_existing_draft_cannot_move_to_another_admission(db_session, hospital):
    patient = _patient(db_session, hospital)
    old_admission = _admission(db_session, hospital, patient, AdmissionStatus.admitted)
    old_draft = _draft(db_session, hospital, patient, old_admission)
    old_admission.status = AdmissionStatus.discharged
    db_session.commit()
    current_admission = _admission(db_session, hospital, patient, AdmissionStatus.admitted)

    with pytest.raises(ValidationError, match="admission cannot be changed"):
        UpdateFormSubmissionAction(db_session).execute(
            hospital.id,
            old_draft.id,
            IpdFormSubmissionUpdate(
                admission_id=current_admission.id,
                form_data={"note": "Wrong encounter"},
            ),
            ACTOR,
        )

    db_session.expire_all()
    assert old_draft.admission_id == old_admission.id


def test_legacy_unlinked_draft_cannot_be_assigned_to_current_admission(db_session, hospital):
    patient = _patient(db_session, hospital)
    current_admission = _admission(db_session, hospital, patient, AdmissionStatus.admitted)
    legacy = CreateFormSubmissionAction(db_session).execute(
        hospital.id,
        IpdFormSubmissionCreate(
            patient_id=patient.id,
            form_id="doctor_notes",
            form_title="Doctor Notes",
            form_data={"note": "Legacy"},
        ),
        ACTOR,
    )

    with pytest.raises(ValidationError, match="admission cannot be changed"):
        UpdateFormSubmissionAction(db_session).execute(
            hospital.id,
            legacy.id,
            IpdFormSubmissionUpdate(admission_id=current_admission.id),
            ACTOR,
        )


def test_new_document_cannot_be_added_to_discharged_admission(db_session, hospital):
    patient = _patient(db_session, hospital)
    old_admission = _admission(db_session, hospital, patient, AdmissionStatus.discharged)

    with pytest.raises(ValidationError, match="admission is closed"):
        _draft(db_session, hospital, patient, old_admission)


def test_same_patient_drafts_are_listed_only_under_their_exact_admission(db_session, hospital):
    patient = _patient(db_session, hospital)
    first = _admission(db_session, hospital, patient, AdmissionStatus.admitted)
    first_draft = _draft(db_session, hospital, patient, first)
    first.status = AdmissionStatus.discharged
    db_session.commit()
    second = _admission(db_session, hospital, patient, AdmissionStatus.admitted)
    second_draft = _draft(db_session, hospital, patient, second)

    action = ListFormSubmissionsAction(db_session)
    first_rows = action.execute(hospital.id, patient.id, first.id, "doctor_notes")
    second_rows = action.execute(hospital.id, patient.id, second.id, "doctor_notes")
    assert [row.id for row in first_rows] == [first_draft.id]
    assert [row.id for row in second_rows] == [second_draft.id]


def test_draft_cannot_be_updated_after_admission_closes(db_session, hospital):
    patient = _patient(db_session, hospital)
    admission = _admission(db_session, hospital, patient, AdmissionStatus.admitted)
    draft = _draft(db_session, hospital, patient, admission)
    admission.status = AdmissionStatus.discharged
    db_session.commit()
    with pytest.raises(ValidationError, match="admission is closed"):
        UpdateFormSubmissionAction(db_session).execute(
            hospital.id, draft.id, IpdFormSubmissionUpdate(form_data={"note": "late"}), ACTOR,
        )


def test_admission_document_list_can_be_paginated_without_mixing_episodes(db_session, hospital):
    patient = _patient(db_session, hospital)
    first = _admission(db_session, hospital, patient, AdmissionStatus.admitted)
    old = _draft(db_session, hospital, patient, first)
    first.status = AdmissionStatus.discharged
    db_session.commit()
    second = _admission(db_session, hospital, patient, AdmissionStatus.admitted)
    current_one = _draft(db_session, hospital, patient, second)
    current_two = _draft(db_session, hospital, patient, second)
    action = ListFormSubmissionsAction(db_session)
    page_one = action.execute(hospital.id, patient.id, second.id, limit=1, offset=0)
    page_two = action.execute(hospital.id, patient.id, second.id, limit=1, offset=1)
    assert {page_one[0].id, page_two[0].id} == {current_one.id, current_two.id}
    assert old.id not in {page_one[0].id, page_two[0].id}


def test_admission_context_exposes_current_and_admission_demographics(db_session, hospital):
    patient = _patient(db_session, hospital)
    patient.date_of_birth = date(2020, 10, 1)
    patient.gender = "female"
    patient.age = 5
    admission = _admission(db_session, hospital, patient, AdmissionStatus.admitted)
    admission.patient_name = "Earlier Name"
    admission.gender = "male"
    admission.age_at_admission = 3
    db_session.commit()

    detail = to_admission_detail(admission)
    assert detail.hospital_id == hospital.id
    assert detail.patient_date_of_birth == date(2020, 10, 1)
    assert detail.patient_current_name == "Alex Test"
    assert detail.patient_current_gender == "female"
    assert detail.patient_current_age == 5
    assert detail.patient_name == "Earlier Name"
    assert detail.gender_at_admission == "male"
    assert detail.age_at_admission == 3
