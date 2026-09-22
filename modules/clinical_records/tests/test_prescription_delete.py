"""Prescription delete lifecycle and sync notification tests."""

from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from modules.clinical_records.entities.clinical_record import Prescription
from modules.clinical_records.tests.test_clinical_records import (  # noqa: F401
    auth_headers,
    clinical_app,
    client,
    doctor_with_role,
    test_patient,
)
from modules.clinical_records.tests.test_prescription_cancel import _staff_headers
from modules.pharmacy.entities.pharmacy_entities import (
    PharmacyRxRequest,
    PharmacyRxRequestItem,
    PharmacyRxRequestStatus,
)
from shared.sync.broker import sync_broker
from tests.conftest import db_session, hospital  # noqa: F401


def _create_prescription(client: TestClient, headers: dict, doctor_id: UUID, patient_id: UUID, status_val: str = "issued"):
    payload = {
        "patient_id": str(patient_id),
        "diagnosis": "Acute Bronchitis",
        "symptoms": "Cough and fever",
        "medicines": "Amoxicillin 500mg TDS x 5d",
        "dosage": "1 TID",
        "status": status_val,
    }
    res = client.post(f"/api/doctors/{doctor_id}/prescriptions", json=payload, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def test_doctor_can_delete_prescription(
    client: TestClient,
    hospital,
    doctor_with_role,
    test_patient,
    db_session: Session,
):
    doc = doctor_with_role
    headers = _staff_headers(hospital, doc)
    rx = _create_prescription(client, headers, doc.id, test_patient.id)
    rx_id = rx["id"]

    # Call DELETE endpoint
    del_res = client.delete(f"/api/doctors/{doc.id}/prescriptions/{rx_id}", headers=headers)
    assert del_res.status_code == 200, del_res.text
    assert del_res.json()["success"] is True

    # Verify gone from database
    persisted = db_session.query(Prescription).filter(Prescription.id == UUID(rx_id)).first()
    assert persisted is None


def test_delete_prescription_blocked_if_pharmacy_dispensed(
    client: TestClient,
    doctor_with_role,
    test_patient,
    hospital,
    db_session: Session,
):
    doc = doctor_with_role
    headers = _staff_headers(hospital, doc)
    rx = _create_prescription(client, headers, doc.id, test_patient.id)
    rx_id = rx["id"]

    # Simulate pharmacy already dispensed
    req = PharmacyRxRequest(
        hospital_id=hospital.id,
        prescription_id=UUID(rx_id),
        patient_id=test_patient.id,
        doctor_id=doc.id,
        status=PharmacyRxRequestStatus.partially_dispensed,
    )
    db_session.add(req)
    db_session.flush()

    item = PharmacyRxRequestItem(
        hospital_id=hospital.id,
        request_id=req.id,
        medicine_name="Amoxicillin 500mg",
        quantity=15,
        dispensed_quantity=5,
        status="partially_dispensed",
    )
    db_session.add(item)
    db_session.commit()

    # Attempt delete -> 409 Conflict
    del_res = client.delete(f"/api/doctors/{doc.id}/prescriptions/{rx_id}", headers=headers)
    assert del_res.status_code == 409
    assert "dispensed" in del_res.json()["detail"].lower()


def test_sync_broker_receives_event_on_mutation(
    client: TestClient,
    doctor_with_role,
    test_patient,
    hospital,
):
    doc = doctor_with_role
    headers = _staff_headers(hospital, doc)
    queue = sync_broker.subscribe(hospital.id)
    try:
        rx = _create_prescription(client, headers, doc.id, test_patient.id)
        # Event should have been published via after_commit hook
        event = queue.get_nowait()
        assert event["hospital_id"] == str(hospital.id)
        assert event["entity_type"] == "prescription"
        assert event["action"] == "create"
    finally:
        sync_broker.unsubscribe(hospital.id, queue)
