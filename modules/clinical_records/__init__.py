"""
Clinical Records module (prescriptions, medical records, patient documents).

This module intentionally has NO standalone `api/` router of its own — it is
NOT dead code, but its API surface is deliberately exposed through the two
modules that own the relevant workflow instead of a third, duplicate one:

  - `modules.doctors.api.doctors_api` exposes the WRITE path
    (`/doctors/{doctor_id}/prescriptions`, `/doctors/{doctor_id}/records`,
    ...) because prescriptions/records are authored by a doctor in the
    context of a consultation.
  - `modules.dms.api` exposes the READ/aggregation path (patient file,
    timeline, document streaming) because that is a cross-record view over
    a patient's full document history, not just this module's own tables.

`modules.laboratory` and `modules.pharmacy` also import this module's
entities directly (a prescription can carry lab/radiology/medicine orders).

Do not add a third `/clinical-records/*` router that re-exposes the same
`actions/clinical_actions.py` classes under a new prefix — that would
duplicate, not fix, this module's API surface. If a new consumer needs
this data, prefer adding a method to `db/clinical_records_repository.py`
or `services/prescription_html_service.py` and calling it from the
consuming module, the same way laboratory/pharmacy/dms already do.
"""
