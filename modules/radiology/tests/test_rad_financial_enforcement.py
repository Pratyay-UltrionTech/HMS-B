"""
Radiology execution-step financial gating and multi-scan request semantics.

- Schedule, start, attachment upload, scan completion and report upload are gated on
  the order's resolved charge; unpaid → 402 and no file is written.
- Attachments are refused once the report is finalized; report amendments on a
  completed order are not re-gated.
- An authorized emergency override is scoped to the order.
- A multi-scan request keeps one charge; cancelling one of its orders returns only
  that scan to pending, and it can be re-accessioned without re-billing.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingPaymentMethod,
    BillingSourceType,
)
from modules.billing.services.billing_service import create_payment
from modules.billing.services.service_charge_resolver import resolve_active_service_charge
from modules.doctors.entities.doctor import HospitalUser
from modules.emergency.contracts.emergency_contracts import EmergencyOrderCreate, EmergencyOrderType
from modules.laboratory.services.lab_prescription_service import (
    create_investigation_requests_for_prescription,
)
from modules.radiology.actions.radiology_actions import (
    AddAttachmentAction,
    CancelOrderAction,
    CancelRadPrescriptionRequestAction,
    CompleteScanAction,
    CreateOrdersAction,
    StartScanAction,
    UploadReportAction,
)
from modules.radiology.contracts.radiology_contracts import (
    RadCompleteScanRequest,
    RadOrderCreate,
    RadReportRequest,
)
from modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
    RadPrescriptionRequest,
    RadPrescriptionRequestStatus,
    RadRequestItemStatus,
)
from tests.conftest import db_session, doctor, hospital, patient  # noqa: F401

EMERGENCY_APPROVE = {"emergency": {"can_view": True, "can_approve": True}}
IMG = "data:image/png;base64,aW1n"


def _staff(db: Session, hospital_id: uuid.UUID, name: str) -> HospitalUser:
    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        role_id=uuid.uuid4(),
        name=name,
        phone="555-0199",
        email=f"{uuid.uuid4().hex[:10]}@apollocity.com",
        password_hash="x",
    )
    db.add(user)
    db.commit()
    return user


def _claims(user: HospitalUser, hospital_id: uuid.UUID, permissions: dict | None = None) -> dict:
    return {
        "sub": user.email,
        "user_id": str(user.id),
        "name": user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_id),
        "permissions": permissions or {},
    }


def _scan(db: Session, hospital_id, code: str, price: float) -> RadiologyScanCatalog:
    s = RadiologyScanCatalog(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        scan_code=code,
        scan_name=f"Scan {code}",
        category="X-Ray",
        department="Radiology",
        price=price,
        duration_minutes=15,
        is_active=True,
    )
    db.add(s)
    db.commit()
    return s


def _pay(db: Session, charge: BillingCharge) -> None:
    amt = float(charge.net_amount) - float(charge.amount_paid)
    create_payment(
        db,
        hospital_id=charge.hospital_id,
        patient_id=charge.patient_id,
        amount=amt,
        payment_date=date.today(),
        payment_method=BillingPaymentMethod.cash,
        notes=None,
        received_by_name="Cashier",
        allocations_plan=[{"charge_id": charge.id, "amount": amt}],
    )
    db.commit()
    db.refresh(charge)


def _report(**extra) -> RadReportRequest:
    return RadReportRequest(findings="No acute findings.", impression="Normal study.", image_file_data=IMG, **extra)


@pytest.fixture
def rad_env(db_session: Session, hospital, patient, monkeypatch):
    written: list[str] = []

    def _fake_save(**kwargs):
        path = f"mem://{kwargs['order_id']}/{kwargs['file_name']}"
        written.append(path)
        return path

    import shared.storage.local_storage as storage

    monkeypatch.setattr(storage, "save_attachment_file", _fake_save)
    tech = _staff(db_session, hospital.id, "Radiographer Kishore")
    return {
        "db": db_session,
        "h": hospital,
        "p": patient,
        "user": _claims(tech, hospital.id),
        "written": written,
        "cxr": _scan(db_session, hospital.id, "CXR", 500.0),
        "usg": _scan(db_session, hospital.id, "USG", 1200.0),
    }


def _direct(env, scan) -> RadiologyOrder:
    db, h, p = env["db"], env["h"], env["p"]
    resp = CreateOrdersAction(db, h.id, env["user"]).execute(RadOrderCreate(patient_id=p.id, scan_ids=[scan.id]))
    return db.get(RadiologyOrder, resp[0].id)


def _attach(env, order, user=None, **kw):
    return AddAttachmentAction(env["db"], env["h"].id, user or env["user"]).execute(
        order.id, "plate.png", "image/png", 3, file_bytes=b"img", **kw
    )


def test_unpaid_attachment_report_and_completion_return_402_without_writing(rad_env):
    db, h, user = rad_env["db"], rad_env["h"], rad_env["user"]
    order = _direct(rad_env, rad_env["cxr"])

    with pytest.raises(HTTPException) as exc:
        _attach(rad_env, order)
    assert exc.value.status_code == 402
    assert exc.value.detail["code"] == "FINANCIAL_CLEARANCE_REQUIRED"
    assert exc.value.detail["source_id"] == str(order.id)
    assert rad_env["written"] == []
    db.rollback()

    with pytest.raises(HTTPException) as exc:
        UploadReportAction(db, h.id, user).execute(order.id, _report())
    assert exc.value.status_code == 402
    db.rollback()

    order = db.get(RadiologyOrder, order.id)
    order.status = RadiologyOrderStatus.in_progress
    db.commit()
    with pytest.raises(HTTPException) as exc:
        CompleteScanAction(db, h.id, user).execute(order.id)
    assert exc.value.status_code == 402
    db.rollback()

    order = db.get(RadiologyOrder, order.id)
    assert order.status == RadiologyOrderStatus.in_progress
    assert order.attachments == [] and order.findings is None


def test_paid_radiology_flow_and_attachment_after_finalized_report(rad_env):
    db, h, user = rad_env["db"], rad_env["h"], rad_env["user"]
    order = _direct(rad_env, rad_env["cxr"])
    _pay(db, resolve_active_service_charge(db, h.id, BillingSourceType.radiology, order.id))

    att = _attach(rad_env, order)
    assert att.storage_path in rad_env["written"]
    assert db.get(RadiologyOrder, order.id).status == RadiologyOrderStatus.in_progress
    assert CompleteScanAction(db, h.id, user).execute(order.id).status == RadiologyOrderStatus.completed
    resp = UploadReportAction(db, h.id, user).execute(order.id, _report())
    assert resp.status == RadiologyOrderStatus.completed

    with pytest.raises(HTTPException) as exc:
        _attach(rad_env, db.get(RadiologyOrder, order.id))
    assert exc.value.status_code == 400
    assert len(rad_env["written"]) == 1
    db.rollback()

    amended = UploadReportAction(db, h.id, user).execute(
        order.id, _report(amendment_reason="Laterality corrected")
    )
    assert amended.is_amended is True


def test_emergency_override_scoped_to_order(rad_env):
    db, h, user = rad_env["db"], rad_env["h"], rad_env["user"]
    er_doc = _claims(_staff(db, h.id, "Dr. ER Lead"), h.id, EMERGENCY_APPROVE)
    order = _direct(rad_env, rad_env["cxr"])
    other = _direct(rad_env, rad_env["usg"])

    with pytest.raises(HTTPException) as exc:
        _attach(rad_env, order, is_emergency_override=True, emergency_override_reason="Polytrauma")
    assert exc.value.status_code == 403
    assert rad_env["written"] == []
    db.rollback()

    _attach(rad_env, order, user=er_doc, is_emergency_override=True, emergency_override_reason="Polytrauma")
    # Remaining steps of the same order go through for a regular user, without re-override.
    CompleteScanAction(db, h.id, user).execute(order.id, RadCompleteScanRequest())
    UploadReportAction(db, h.id, user).execute(order.id, _report())
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.radiology, order.id)
    assert charge.status == BillingChargeStatus.pending

    with pytest.raises(HTTPException) as exc:
        StartScanAction(db, h.id, user).execute(other.id)
    assert exc.value.status_code == 402


def _rad_request(db, h, p, doctor, scan_ids) -> RadPrescriptionRequest:
    _, rad_req = create_investigation_requests_for_prescription(
        db,
        hospital_id=h.id,
        doctor_id=doctor.id,
        patient_id=p.id,
        appointment_id=None,
        prescription_id=uuid.uuid4(),
        scan_ids=scan_ids,
    )
    db.commit()
    return rad_req


def test_multi_scan_request_cancel_one_order_and_reaccession(rad_env, doctor):
    db, h, p, user = rad_env["db"], rad_env["h"], rad_env["p"], rad_env["user"]
    req = _rad_request(db, h, p, doctor, [rad_env["cxr"].id, rad_env["usg"].id])
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.radiology, req.id)
    assert float(charge.net_amount) == 1700.0
    _pay(db, charge)

    orders = CreateOrdersAction(db, h.id, user).execute(
        RadOrderCreate(patient_id=p.id, prescription_request_id=req.id)
    )
    assert len(orders) == 2
    db.refresh(req)
    assert req.status == RadPrescriptionRequestStatus.partially_processed
    cxr_order = next(o for o in orders if o.scan_code == "CXR")
    usg_order = next(o for o in orders if o.scan_code == "USG")

    # The whole request cannot be cancelled while its orders are live.
    with pytest.raises(HTTPException) as exc:
        CancelRadPrescriptionRequestAction(db, h.id).execute(req.id, "changed mind", user)
    assert exc.value.status_code == 409
    db.rollback()

    CancelOrderAction(db, h.id, user).execute(usg_order.id)
    db.refresh(req)
    db.refresh(charge)
    by_code = {i.scan_code: i.status for i in req.items}
    assert by_code == {"CXR": RadRequestItemStatus.ordered, "USG": RadRequestItemStatus.pending}
    assert req.status == RadPrescriptionRequestStatus.partially_processed
    assert charge.status == BillingChargeStatus.paid and charge.source_id == req.id

    # Only the returned scan is re-accessioned; nothing is re-billed.
    again = CreateOrdersAction(db, h.id, user).execute(
        RadOrderCreate(patient_id=p.id, prescription_request_id=req.id)
    )
    assert [o.scan_code for o in again] == ["USG"]
    active = (
        db.query(BillingCharge)
        .filter(
            BillingCharge.source_type == BillingSourceType.radiology,
            BillingCharge.patient_id == p.id,
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .all()
    )
    assert [c.id for c in active] == [charge.id]

    for oid in (cxr_order.id, again[0].id):
        o = db.get(RadiologyOrder, oid)
        _attach(rad_env, o)
        UploadReportAction(db, h.id, user).execute(oid, _report())
    db.refresh(req)
    assert req.status == RadPrescriptionRequestStatus.completed


def test_emergency_console_radiology_alias():
    assert EmergencyOrderCreate(order_type="radiology", description="CT head").order_type == (
        EmergencyOrderType.radiology_scan
    )
    assert EmergencyOrderCreate(order_type="stat_medication", description="Adrenaline").order_type == (
        EmergencyOrderType.medication
    )
