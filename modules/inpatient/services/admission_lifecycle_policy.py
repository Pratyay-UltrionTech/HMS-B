"""
Centralized Admission Clinical Lifecycle Policy.

Enforces rules regarding which clinical operations are permitted based on the
current state of the inpatient admission (requested, admitted, discharge_requested,
discharged, cancelled).
"""

from __future__ import annotations

from fastapi import HTTPException, status

from modules.inpatient.entities.admission import Admission, AdmissionStatus


ACTIVE_DOCUMENTATION_STATUSES = {
    AdmissionStatus.admitted,
    AdmissionStatus.discharge_requested,
}


def assert_can_document_clinical_record(
    admission: Admission,
    operation_name: str = "document clinical records",
) -> None:
    """
    Assert that the admission is in a state that permits active clinical documentation
    (e.g., care plans, clinical notes, shift handovers, medication scheduling/execution,
    vital signs, intake/output).

    Raises:
        HTTPException(400) if the admission is cancelled, discharged, or still in requested state.
    """
    if admission.status == AdmissionStatus.discharged:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot {operation_name} to a discharged admission",
        )
    if admission.status == AdmissionStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot {operation_name} to a cancelled admission",
        )
    if admission.status == AdmissionStatus.requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot {operation_name} before admission is accepted",
        )
    if admission.status not in ACTIVE_DOCUMENTATION_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot {operation_name} for an admission in '{admission.status.value}' state",
        )
