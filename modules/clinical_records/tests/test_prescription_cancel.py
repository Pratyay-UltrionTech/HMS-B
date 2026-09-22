"""Prescription cancel/discard lifecycle tests."""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from modules.clinical_records.entities.clinical_record import Prescription
from modules.clinical_records.tests.test_clinical_records import (  # noqa: F401
    auth_headers,
    clinical_app,
    client,
    doctor_with_role,
    test_patient,
)
from modules.doctors.entities.doctor import HospitalUser, RolePermission, StaffRole
from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderSource,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestStatus,
    LabSampleType,
    LabTestCatalog,
)
from modules.pharmacy.entities.pharmacy_entities import (
    PharmacyRxRequest,
    PharmacyRxRequestItem,
    PharmacyRxRequestStatus,
)
from modules.tenancy.entities.hospital import Hospital
from shared.audit.entities.audit_log import AuditLog
from shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital, hospital_b  # noqa: F401


def _cancel_url(doctor_id, rx_id) -> str:
    return f"/api/doctors/{doctor_id}/prescriptions/{rx_id}/cancel"


def _create_rx(client, headers, doctor_id, patient_id, *, status="issued", extra=None):
    payload = {
        "patient_id": str(patient_id),
        "symptoms": "Fever",
        "diagnosis": "Viral illness",
        "medicines": "Paracetamol 500mg",
        "dosage": "1 TID",
        "status": status,
    }
    if extra:
        payload.update(extra)
    res = client.post(f"/api/doctors/{doctor_id}/prescriptions", json=payload, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def _staff_headers(hospital: Hospital, doctor: HospitalUser) -> dict[str, str]:
    token = create_access_token(
        {
            "sub": doctor.email,
            "name": doctor.name,
            "role": "hospital_staff",
            "hospital_uuid": str(hospital.id),
            "user_id": str(doctor.id),
            "doctor_id": str(doctor.id),
            "staff_role_id": str(doctor.role_id),
        }
    )
    return {"Authorization": f"Bearer {token}"}


def _admin_headers(hospital: Hospital) -> dict[str, str]:
    token = create_access_token(
        {
            "sub": "admin@hospital.com",
            "name": "Hospital Admin",
            "role": "hospital_admin",
            "hospital_uuid": str(hospital.id),
            "user_id": str(uuid4()),
        }
    )
    return {"Authorization": f"Bearer {token}"}


def test_discard_unused_draft(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
    db_session: Session,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="draft")
    res = client.post(_cancel_url(doc_id, rx["id"]), json={"reason": "Not required"}, headers=auth_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "discarded"
    assert body["cancel_reason"] == "Not required"
    assert body["cancelled_at"] is not None
    assert body["cancelled_by"]
    row = db_session.query(Prescription).filter(Prescription.id == UUID(rx["id"])).one()
    assert row.status == "discarded"


def test_draft_with_pending_investigation_cancels_requests_not_deleted(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
    db_session: Session,
    hospital: Hospital,
):
    lab = LabTestCatalog(
        hospital_id=hospital.id,
        test_code="CBC",
        test_name="Complete Blood Count",
        department="Hematology",
        price=200.0,
        is_active=True,
    )
    db_session.add(lab)
    db_session.commit()

    doc_id = str(doctor_with_role.id)
    rx = _create_rx(
        client,
        auth_headers,
        doc_id,
        test_patient.id,
        status="draft",
        extra={"test_ids": [str(lab.id)]},
    )
    req = (
        db_session.query(LabPrescriptionRequest)
        .filter(LabPrescriptionRequest.prescription_id == UUID(rx["id"]))
        .one()
    )
    req_id = req.id
    assert req.status == LabPrescriptionRequestStatus.pending

    res = client.post(
        _cancel_url(doc_id, rx["id"]),
        json={"reason": "Patient declined tests"},
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "discarded"

    db_session.expire_all()
    still = db_session.query(LabPrescriptionRequest).filter(LabPrescriptionRequest.id == req_id).one()
    assert still.status == LabPrescriptionRequestStatus.cancelled
    assert db_session.query(Prescription).filter(Prescription.id == UUID(rx["id"])).one().status == "discarded"


def test_draft_with_processed_departmental_work_blocked(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
    db_session: Session,
    hospital: Hospital,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="draft")
    order = LabOrder(
        hospital_id=hospital.id,
        order_no="LAB-BLOCK-1",
        patient_id=test_patient.id,
        doctor_id=doctor_with_role.id,
        prescription_id=UUID(rx["id"]),
        order_source=LabOrderSource.doctor_prescribed,
        ordered_by_name=doctor_with_role.name,
        ordered_by_role="doctor",
        status=LabOrderStatus.in_progress,
        sample_type=LabSampleType.blood,
    )
    db_session.add(order)
    db_session.commit()

    res = client.post(
        _cancel_url(doc_id, rx["id"]),
        json={"reason": "Try discard after accession"},
        headers=auth_headers,
    )
    assert res.status_code == 409, res.text
    assert "laboratory work is already in progress" in res.json()["detail"].lower()
    db_session.expire_all()
    assert db_session.query(Prescription).filter(Prescription.id == UUID(rx["id"])).one().status == "draft"
    assert db_session.query(LabOrder).filter(LabOrder.id == order.id).one().status == LabOrderStatus.in_progress


def test_cancel_issued_with_reason(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
    db_session: Session,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="issued")
    res = client.post(_cancel_url(doc_id, rx["id"]), json={"reason": "Wrong patient selected"}, headers=auth_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "cancelled"
    assert body["cancel_reason"] == "Wrong patient selected"
    assert db_session.query(Prescription).filter(Prescription.id == UUID(rx["id"])).one().status == "cancelled"


def test_empty_reason_rejected(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="issued")
    res = client.post(_cancel_url(doc_id, rx["id"]), json={"reason": ""}, headers=auth_headers)
    assert res.status_code in {400, 422}, res.text
    res_ws = client.post(_cancel_url(doc_id, rx["id"]), json={"reason": "   "}, headers=auth_headers)
    assert res_ws.status_code in {400, 422}, res_ws.text


def test_cancelled_remains_on_list_and_cannot_cancel_twice(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="issued")
    first = client.post(_cancel_url(doc_id, rx["id"]), json={"reason": "Duplicate entry"}, headers=auth_headers)
    assert first.status_code == 200
    listed = client.get(
        f"/api/doctors/{doc_id}/prescriptions?patient_id={test_patient.id}",
        headers=auth_headers,
    )
    assert listed.status_code == 200
    found = next(r for r in listed.json() if r["id"] == rx["id"])
    assert found["status"] == "cancelled"
    second = client.post(_cancel_url(doc_id, rx["id"]), json={"reason": "Again"}, headers=auth_headers)
    assert second.status_code == 409


def test_tenant_isolation_other_hospital_404(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
    db_session: Session,
    hospital_b: Hospital,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="issued")

    role_b = StaffRole(id=uuid4(), hospital_id=hospital_b.id, name="Doctor")
    db_session.add(role_b)
    db_session.flush()
    db_session.add(
        RolePermission(
            hospital_id=hospital_b.id,
            role_id=role_b.id,
            module_key="doctors",
            can_view=True,
            can_edit=True,
        )
    )
    other_doc = HospitalUser(
        id=uuid4(),
        hospital_id=hospital_b.id,
        role_id=role_b.id,
        name="Dr Other Hospital",
        email="other.hosp@example.com",
        phone="9000000001",
        password_hash="pwd",
        specialization="GP",
        is_active=True,
    )
    db_session.add(other_doc)
    db_session.commit()
    other_headers = _staff_headers(hospital_b, other_doc)
    res = client.post(
        _cancel_url(other_doc.id, rx["id"]),
        json={"reason": "Cross tenant"},
        headers=other_headers,
    )
    assert res.status_code == 404, res.text


def test_non_owner_doctor_403_hospital_admin_allowed(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
    db_session: Session,
    hospital: Hospital,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="issued")

    other = HospitalUser(
        id=uuid4(),
        hospital_id=hospital.id,
        role_id=doctor_with_role.role_id,
        name="Dr Second",
        email="second.doc@hospital.com",
        phone="9000000002",
        password_hash="pwd",
        specialization="ENT",
        is_active=True,
    )
    db_session.add(other)
    db_session.commit()
    other_headers = _staff_headers(hospital, other)
    forbidden = client.post(
        _cancel_url(other.id, rx["id"]),
        json={"reason": "Not my prescription"},
        headers=other_headers,
    )
    assert forbidden.status_code == 403, forbidden.text

    admin_headers = _admin_headers(hospital)
    allowed = client.post(
        _cancel_url(doc_id, rx["id"]),
        json={"reason": "Admin correction"},
        headers=admin_headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "cancelled"


def test_pharmacy_partially_dispensed_blocks_cancel(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
    db_session: Session,
    hospital: Hospital,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="issued")
    pharm = PharmacyRxRequest(
        hospital_id=hospital.id,
        prescription_id=UUID(rx["id"]),
        patient_id=test_patient.id,
        doctor_id=doctor_with_role.id,
        patient_name=test_patient.name,
        status=PharmacyRxRequestStatus.partially_dispensed,
    )
    db_session.add(pharm)
    db_session.flush()
    db_session.add(
        PharmacyRxRequestItem(
            hospital_id=hospital.id,
            request_id=pharm.id,
            medicine_name="Paracetamol 500mg",
            quantity=10,
            dispensed_quantity=4,
            status=PharmacyRxRequestStatus.partially_dispensed.value,
        )
    )
    db_session.commit()
    res = client.post(_cancel_url(doc_id, rx["id"]), json={"reason": "Stop meds"}, headers=auth_headers)
    assert res.status_code == 409, res.text
    assert "pharmacy" in res.json()["detail"].lower()


def test_billing_charge_not_deleted_when_processed_work_blocks_discard(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
    db_session: Session,
    hospital: Hospital,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="draft")
    order = LabOrder(
        hospital_id=hospital.id,
        order_no="LAB-BILL-1",
        patient_id=test_patient.id,
        doctor_id=doctor_with_role.id,
        prescription_id=UUID(rx["id"]),
        order_source=LabOrderSource.doctor_prescribed,
        ordered_by_name=doctor_with_role.name,
        ordered_by_role="doctor",
        status=LabOrderStatus.ordered,
    )
    db_session.add(order)
    db_session.flush()
    charge = BillingCharge(
        hospital_id=hospital.id,
        patient_id=test_patient.id,
        source_type=BillingSourceType.laboratory,
        source_id=order.id,
        description="CBC",
        charge_amount=200.0,
        net_amount=200.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add(charge)
    db_session.commit()
    charge_id = charge.id

    res = client.post(_cancel_url(doc_id, rx["id"]), json={"reason": "Should not reverse billing"}, headers=auth_headers)
    assert res.status_code == 409, res.text
    db_session.expire_all()
    still = db_session.query(BillingCharge).filter(BillingCharge.id == charge_id).one()
    assert still.status == BillingChargeStatus.pending
    assert still.net_amount == 200.0


def test_cancel_writes_audit_log(
    client: TestClient,
    auth_headers: dict[str, str],
    doctor_with_role: HospitalUser,
    test_patient,
    db_session: Session,
):
    doc_id = str(doctor_with_role.id)
    rx = _create_rx(client, auth_headers, doc_id, test_patient.id, status="issued")
    res = client.post(_cancel_url(doc_id, rx["id"]), json={"reason": "Clinical error"}, headers=auth_headers)
    assert res.status_code == 200, res.text
    logs = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_type == "prescription", AuditLog.entity_id == rx["id"])
        .all()
    )
    assert any(log.action == "cancel" for log in logs)
