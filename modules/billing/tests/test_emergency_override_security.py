"""P0.1: emergency financial overrides and exception approvals are authorized by real
permissions (never by display names), scoped to one source / admission, and always
leave a persisted AuditLog row plus a published sync event."""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infrastructure.postgres.session import get_transitional_sync_session
from modules.beds.entities.bed import Bed, Room, Ward
from modules.billing.api.billing_api import router as billing_router
from modules.billing.contracts.policy_contracts import AdmissionFinancialPolicyUpdate
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from modules.billing.entities.financial_exception import (
    FinancialClearanceException,
    FinancialExceptionStatus,
)
from modules.billing.services.financial_authorization import (
    OverrideScope,
    authorize_emergency_override,
)
from modules.billing.services.service_financial_clearance import (
    assert_service_financially_cleared,
    set_admission_financial_policy,
)
from modules.doctors.entities.doctor import HospitalUser
from modules.inpatient.actions.admission_lifecycle_actions import (
    AcceptAdmissionAction,
    EnsureAdmissionRequestAction,
)
from modules.inpatient.entities.admission import Admission, AdmissionStatus
from modules.laboratory.actions.laboratory_actions import CollectSampleAction
from modules.laboratory.contracts.lab_contracts import SampleCollectRequest
from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderSource,
    LabOrderStatus,
    LabSampleType,
)
from modules.ot.actions.ot_actions import StartSurgeryAction
from modules.ot.entities.ot_entities import OtPriority, OtSurgery, OtSurgeryStatus
from modules.patients.entities.patient import Patient
from modules.pharmacy.tests.test_pharmacy import pharm_client, pharm_db  # noqa: F401  (fixtures)
from modules.radiology.actions.radiology_actions import ScheduleOrderAction
from modules.radiology.contracts.radiology_contracts import RadScheduleRequest
from modules.radiology.entities.radiology_entities import RadiologyOrder, RadiologyOrderStatus
from modules.tenancy.entities.hospital import Hospital
from shared.audit.entities.audit_log import AuditLog
from shared.auth.dependencies import get_hospital_context, require_hospital_user
from shared.exceptions import register_exception_handlers
from shared.sync.broker import sync_broker
from tests.conftest import db_session, doctor, hospital, patient  # noqa: F401  (fixtures)

EMERGENCY_APPROVE = {"emergency": {"can_view": True, "can_approve": True}}
BILLING_APPROVE = {"billing": {"can_view": True, "can_approve": True}}
RECEPTIONIST_PERMS = {
    "registration": {"can_view": True, "can_edit": True, "can_create": True},
    "appointment": {"can_view": True, "can_edit": True, "can_create": True},
    "billing": {"can_view": True, "can_edit": True, "can_create": True},
}


def _staff(db: Session, hospital_id: uuid.UUID, name: str) -> HospitalUser:
    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        role_id=uuid.uuid4(),
        name=name,
        phone="555-0142",
        email=f"{uuid.uuid4().hex[:10]}@apollocity.com",
        password_hash="dummy_hash_for_test",
    )
    db.add(user)
    db.commit()
    return user


def _claims(user: HospitalUser, hospital_id: uuid.UUID, permissions: dict | None = None, **extra) -> dict:
    return {
        "sub": user.email,
        "user_id": str(user.id),
        "name": user.name,
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_id),
        "permissions": permissions or {},
        **extra,
    }


def _receptionist(db: Session, hospital_id: uuid.UUID) -> dict:
    return _claims(_staff(db, hospital_id, "Front Desk Priya"), hospital_id, RECEPTIONIST_PERMS)


def _dr_fake(db: Session, hospital_id: uuid.UUID) -> dict:
    # Name and role label look clinical; no override permission is granted.
    return _claims(
        _staff(db, hospital_id, "Dr. Fake"),
        hospital_id,
        {"doctors": {"can_view": True, "can_edit": True}},
        staff_role_name="Doctor",
        role_label="Emergency Consultant",
    )


def _er_consultant(db: Session, hospital_id: uuid.UUID) -> dict:
    return _claims(_staff(db, hospital_id, "Dr. Real Emergency"), hospital_id, EMERGENCY_APPROVE)


def _unpaid_charge(db: Session, hospital_id, patient_id, source_type, source_id, amount=1000.0) -> BillingCharge:
    charge = BillingCharge(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        patient_id=patient_id,
        source_type=source_type,
        source_id=source_id,
        description=f"{source_type.value} service",
        quantity=Decimal("1.0"),
        unit_price=Decimal(str(amount)),
        charge_amount=Decimal(str(amount)),
        discount_amount=Decimal("0.0"),
        tax_amount=Decimal("0.0"),
        net_amount=Decimal(str(amount)),
        amount_paid=Decimal("0.0"),
        status=BillingChargeStatus.pending,
    )
    db.add(charge)
    db.commit()
    return charge


def _lab_order(db: Session, h: Hospital, pat: Patient, doc: HospitalUser) -> LabOrder:
    order = LabOrder(
        id=uuid.uuid4(),
        hospital_id=h.id,
        order_no=f"LAB-{uuid.uuid4().hex[:6].upper()}",
        patient_id=pat.id,
        doctor_id=doc.id,
        order_source=LabOrderSource.doctor_prescribed,
        status=LabOrderStatus.ordered,
        sample_type=LabSampleType.blood,
    )
    db.add(order)
    db.commit()
    _unpaid_charge(db, h.id, pat.id, BillingSourceType.laboratory, order.id, 1200.0)
    return order


def _now_ms() -> int:
    return int(time.time() * 1000) - 1


def _events(hospital_id, since: int, entity_type: str, action: str) -> list[dict]:
    return [
        e
        for e in sync_broker.get_events_since(hospital_id, since)
        if e["entity_type"] == entity_type and e["action"] == action
    ]


def _audit_rows(db: Session, hospital_id, entity_type: str, action: str) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.hospital_id == hospital_id,
            AuditLog.entity_type == entity_type,
            AuditLog.action == action,
        )
        .all()
    )


@pytest.fixture
def api(db_session: Session, hospital: Hospital):
    """Billing router with a swappable current user (dict mutated per test)."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(billing_router, prefix="/api")
    current: dict = {}

    def _db():
        yield db_session

    app.dependency_overrides[get_transitional_sync_session] = _db
    app.dependency_overrides[require_hospital_user] = lambda: current
    app.dependency_overrides[get_hospital_context] = lambda: hospital.id

    def as_user(claims: dict) -> TestClient:
        current.clear()
        current.update(claims)
        return client

    client = TestClient(app)
    return as_user


@pytest.fixture
def ward_bed(db_session: Session, hospital: Hospital):
    ward = Ward(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Emergency Ward",
        ward_type="general",
        admission_fee=500.0,
        bed_charge_per_day=1500.0,
        is_active=True,
    )
    room = Room(id=uuid.uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_code="E-1", is_active=True)
    bed = Bed(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code="E-1-A",
        is_occupied=False,
        is_active=True,
    )
    db_session.add_all([ward, room, bed])
    db_session.commit()
    return ward, room, bed


def _override_body(patient_id, source_id, **overrides) -> dict:
    body = {
        "patient_id": str(patient_id),
        "service_type": "laboratory",
        "action_type": "sample_collection",
        "source_id": str(source_id) if source_id else None,
        "required_amount": 1200.0,
        "reason": "Anaphylactic shock, immediate troponin required",
    }
    body.update(overrides)
    return body


# ── Standalone endpoint ────────────────────────────────────────────────────


def test_standalone_override_denied_for_receptionist_and_name_only_doctor(
    api, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    order = _lab_order(db_session, hospital, patient, doctor)
    body = _override_body(patient.id, order.id)

    for claims in (_receptionist(db_session, hospital.id), _dr_fake(db_session, hospital.id)):
        res = api(claims).post("/api/billing/exceptions/emergency-override", json=body)
        assert res.status_code == 403, res.text

    institutional_admin = {"sub": hospital.email, "name": "Hospital Admin", "role": "hospital_admin",
                           "hospital_uuid": str(hospital.id)}
    res = api(institutional_admin).post("/api/billing/exceptions/emergency-override", json=body)
    assert res.status_code == 403, res.text

    assert db_session.query(FinancialClearanceException).count() == 0
    assert _audit_rows(db_session, hospital.id, "financial_clearance_exception", "emergency_override") == []


def test_standalone_override_authorized_persists_audit_and_publishes_sync(
    api, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    order = _lab_order(db_session, hospital, patient, doctor)
    consultant = _er_consultant(db_session, hospital.id)
    since = _now_ms()

    res = api(consultant).post(
        "/api/billing/exceptions/emergency-override", json=_override_body(patient.id, order.id)
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["is_emergency"] is True
    assert data["source_id"] == str(order.id)
    assert data["requested_by_id"] == consultant["user_id"]
    assert data["approved_by_id"] == consultant["user_id"]
    assert data["is_consumed"] is False

    rows = _audit_rows(db_session, hospital.id, "financial_clearance_exception", "emergency_override")
    assert len(rows) == 1
    details = rows[0].details
    assert rows[0].entity_id == data["id"]
    assert details["patient_id"] == str(patient.id)
    assert details["service_type"] == "laboratory"
    assert details["source_id"] == str(order.id)
    assert details["reason"].startswith("Anaphylactic shock")
    assert details["actor_user_id"] == consultant["user_id"]
    assert details["authorized_by_permission"] == "emergency:approve"
    assert "account_id" in details and "admission_id" in details

    events = _events(hospital.id, since, "financial_clearance_exception", "emergency_override")
    assert [e["entity_id"] for e in events] == [data["id"]]


def test_standalone_override_scope_and_reason_validation(
    api, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    order = _lab_order(db_session, hospital, patient, doctor)
    client = api(_er_consultant(db_session, hospital.id))
    url = "/api/billing/exceptions/emergency-override"

    missing_reason = _override_body(patient.id, order.id)
    del missing_reason["reason"]
    assert client.post(url, json=missing_reason).status_code == 422
    assert client.post(url, json=_override_body(patient.id, order.id, reason="     ")).status_code == 400
    assert client.post(url, json=_override_body(patient.id, None)).status_code == 400
    assert client.post(
        url, json=_override_body(patient.id, None, service_type="admission_bed", action_type="bed_allocation")
    ).status_code == 400
    assert db_session.query(FinancialClearanceException).count() == 0


def test_standalone_override_is_consumed_only_by_its_own_source(
    api, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    scoped = _lab_order(db_session, hospital, patient, doctor)
    unrelated = _lab_order(db_session, hospital, patient, doctor)
    res = api(_er_consultant(db_session, hospital.id)).post(
        "/api/billing/exceptions/emergency-override", json=_override_body(patient.id, scoped.id)
    )
    assert res.status_code == 201, res.text

    with pytest.raises(HTTPException) as blocked:
        assert_service_financially_cleared(db_session, hospital.id, BillingSourceType.laboratory, unrelated.id)
    assert blocked.value.status_code == 402

    state = assert_service_financially_cleared(db_session, hospital.id, BillingSourceType.laboratory, scoped.id)
    assert state.is_cleared
    db_session.commit()
    row = db_session.get(FinancialClearanceException, uuid.UUID(res.json()["id"]))
    assert row.is_consumed and row.status == FinancialExceptionStatus.consumed.value


def test_legacy_null_source_exception_does_not_clear_unrelated_order(
    db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    order = _lab_order(db_session, hospital, patient, doctor)
    legacy = FinancialClearanceException(
        hospital_id=hospital.id,
        patient_id=patient.id,
        service_type="laboratory",
        action_type="sample_collection",
        source_id=None,
        is_emergency=False,
        required_amount=Decimal("1200.00"),
        amount_covered=Decimal("0.00"),
        shortfall_amount=Decimal("1200.00"),
        reason_category="administrative_approval",
        reason="Legacy blanket approval row",
        status=FinancialExceptionStatus.approved.value,
        requested_by_id=uuid.uuid4(),
        requested_by_name="Legacy Requester",
        approved_by_id=uuid.uuid4(),
        approved_by_name="Legacy Approver",
        approved_at=datetime.now(timezone.utc),
    )
    db_session.add(legacy)
    db_session.commit()

    with pytest.raises(HTTPException) as blocked:
        assert_service_financially_cleared(
            db_session, hospital.id, BillingSourceType.laboratory, order.id, patient_id=patient.id
        )
    assert blocked.value.status_code == 402
    db_session.refresh(legacy)
    assert legacy.is_consumed is False


# ── Exception request / decision ───────────────────────────────────────────


def test_request_exception_persists_audit_and_sync_and_rejects_null_source(
    api, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    order = _lab_order(db_session, hospital, patient, doctor)
    requester = _receptionist(db_session, hospital.id)
    client = api(requester)
    body = {
        "patient_id": str(patient.id),
        "service_type": "laboratory",
        "action_type": "sample_collection",
        "source_id": None,
        "required_amount": 1200.0,
        "reason_category": "charity_care",
        "reason": "Patient enrolled in charity scheme",
    }
    assert client.post("/api/billing/exceptions/request", json=body).status_code == 400

    since = _now_ms()
    res = client.post("/api/billing/exceptions/request", json={**body, "source_id": str(order.id)})
    assert res.status_code == 201, res.text
    exc_id = res.json()["id"]
    assert res.json()["status"] == "pending"

    rows = _audit_rows(db_session, hospital.id, "financial_clearance_exception", "create")
    assert [r.entity_id for r in rows] == [exc_id]
    assert rows[0].details["source_id"] == str(order.id)
    assert rows[0].details["patient_id"] == str(patient.id)
    assert rows[0].details["actor_user_id"] == requester["user_id"]
    assert [e["entity_id"] for e in _events(hospital.id, since, "financial_clearance_exception", "create")] == [exc_id]


def test_decide_exception_requires_billing_approve_and_separation_of_duties(
    api, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    order = _lab_order(db_session, hospital, patient, doctor)
    requester = _claims(_staff(db_session, hospital.id, "Billing Clerk"), hospital.id, BILLING_APPROVE)
    res = api(requester).post(
        "/api/billing/exceptions/request",
        json={
            "patient_id": str(patient.id),
            "service_type": "laboratory",
            "action_type": "sample_collection",
            "source_id": str(order.id),
            "reason_category": "government_scheme",
            "reason": "Covered under state health scheme",
        },
    )
    exc_id = res.json()["id"]
    decide = f"/api/billing/exceptions/{exc_id}/decide"
    approve = {"status": "approved", "approval_remarks": "Scheme verified"}

    assert api(_dr_fake(db_session, hospital.id)).post(decide, json=approve).status_code == 403
    assert api(requester).post(decide, json=approve).status_code == 403  # self-approval

    approver = _claims(_staff(db_session, hospital.id, "Finance Head"), hospital.id, BILLING_APPROVE)
    res = api(approver).post(decide, json=approve)
    assert res.status_code == 200, res.text
    assert res.json()["approved_by_id"] == approver["user_id"]
    rows = _audit_rows(db_session, hospital.id, "financial_clearance_exception", "update")
    assert len(rows) == 1 and rows[0].details["decision"] == "approved"


# ── Inline gates ───────────────────────────────────────────────────────────


def test_inline_lab_override_denied_without_permission_and_recorded_when_authorized(
    db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    order = _lab_order(db_session, hospital, patient, doctor)
    payload = SampleCollectRequest(
        collected_by="Lab Tech",
        is_emergency_override=True,
        emergency_override_reason="Septic shock, lactate needed now",
    )
    for claims in (_receptionist(db_session, hospital.id), _dr_fake(db_session, hospital.id)):
        with pytest.raises(HTTPException) as denied:
            CollectSampleAction(db_session, hospital.id).execute(order.id, payload, claims)
        assert denied.value.status_code == 403
        db_session.rollback()
    db_session.refresh(order)
    assert order.status == LabOrderStatus.ordered

    with pytest.raises(HTTPException) as no_reason:
        CollectSampleAction(db_session, hospital.id).execute(
            order.id,
            SampleCollectRequest(collected_by="Lab Tech", is_emergency_override=True),
            _er_consultant(db_session, hospital.id),
        )
    assert no_reason.value.status_code == 400
    db_session.rollback()

    since = _now_ms()
    consultant = _claims(_staff(db_session, hospital.id, "IPD Head"), hospital.id,
                         {"all_ipd": {"can_view": True, "can_approve": True}})
    res = CollectSampleAction(db_session, hospital.id).execute(order.id, payload, consultant)
    assert res.status == LabOrderStatus.sample_collected

    rec = db_session.query(FinancialClearanceException).filter_by(source_id=order.id).one()
    assert rec.is_emergency and rec.is_consumed
    assert rec.approved_by_id == uuid.UUID(consultant["user_id"])
    rows = _audit_rows(db_session, hospital.id, "financial_clearance_exception", "emergency_override")
    assert len(rows) == 1 and rows[0].details["authorized_by_permission"] == "all_ipd:approve"
    assert _events(hospital.id, since, "financial_clearance_exception", "emergency_override")


def test_inline_radiology_override_denied_for_receptionist(
    db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser
):
    order = RadiologyOrder(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        order_no=f"RAD-{uuid.uuid4().hex[:6].upper()}",
        patient_id=patient.id,
        doctor_id=doctor.id,
        scan_code="CT-BRAIN",
        scan_name="CT Brain",
        category="CT",
        price=3500.0,
        status=RadiologyOrderStatus.ordered,
    )
    db_session.add(order)
    db_session.commit()
    _unpaid_charge(db_session, hospital.id, patient.id, BillingSourceType.radiology, order.id, 3500.0)
    payload = RadScheduleRequest(
        scheduled_at=datetime.now(timezone.utc),
        machine="CT-1",
        technician_name="Rad Tech",
        is_emergency_override=True,
        emergency_override_reason="Suspected stroke within thrombolysis window",
    )
    with pytest.raises(HTTPException) as denied:
        ScheduleOrderAction(db_session, hospital.id, _receptionist(db_session, hospital.id)).execute(order.id, payload)
    assert denied.value.status_code == 403
    db_session.rollback()
    db_session.refresh(order)
    assert order.status == RadiologyOrderStatus.ordered


def _emergency_surgery(db: Session, h: Hospital, pat: Patient) -> OtSurgery:
    surgery = OtSurgery(
        id=uuid.uuid4(),
        hospital_id=h.id,
        surgery_no=f"OT-{uuid.uuid4().hex[:6].upper()}",
        patient_id=pat.id,
        surgery_type="Emergency Laparotomy",
        priority=OtPriority.emergency,
        scheduled_at=datetime.now(timezone.utc),
        status=OtSurgeryStatus.scheduled,
    )
    db.add(surgery)
    db.commit()
    _unpaid_charge(db, h.id, pat.id, BillingSourceType.ot, surgery.id, 25000.0)
    return surgery


def test_ot_emergency_priority_does_not_bypass_without_authorized_actor(
    db_session: Session, hospital: Hospital, patient: Patient
):
    surgery = _emergency_surgery(db_session, hospital, patient)
    for claims in (_receptionist(db_session, hospital.id), _dr_fake(db_session, hospital.id)):
        with pytest.raises(HTTPException) as blocked:
            StartSurgeryAction(db_session, hospital.id, claims).execute(surgery.id)
        assert blocked.value.status_code == 402
        db_session.rollback()
    db_session.refresh(surgery)
    assert surgery.status == OtSurgeryStatus.scheduled
    assert db_session.query(FinancialClearanceException).count() == 0

    res = StartSurgeryAction(db_session, hospital.id, _er_consultant(db_session, hospital.id)).execute(surgery.id)
    assert res.status == OtSurgeryStatus.in_progress
    rec = db_session.query(FinancialClearanceException).filter_by(source_id=surgery.id).one()
    assert rec.is_emergency and rec.is_consumed and rec.service_type == "ot"


def test_pharmacy_walk_in_override_requires_permission(pharm_client, pharm_db):
    from modules.pharmacy.tests.test_pharmacy_sale_payment_integrity import _available_stock, _stock_medicine

    client, hospital_id, current_user = pharm_client
    med_id = _stock_medicine(client)

    pharmacist = _staff(pharm_db, hospital_id, "Counter Pharmacist")
    dispense = {"pharmacy": {"can_view": True, "can_dispense": True, "can_edit": True}}
    current_user.clear()
    current_user.update(_claims(pharmacist, hospital_id, dispense))

    sale = {
        "customer_name": "Walk-in",
        "customer_phone": "9876500000",
        "is_emergency_override": True,
        "emergency_override_reason": "Unconscious walk-in, adrenaline needed",
        "items": [{"medicine_id": med_id, "quantity": 2.0}],
    }
    res = client.post("/api/pharmacy/sales", json=sale)
    assert res.status_code == 403, res.text
    assert _available_stock(client, med_id) == 100.0
    assert _audit_rows(pharm_db, hospital_id, "pharmacy_sale", "emergency_override") == []

    current_user.update(_claims(pharmacist, hospital_id, {**dispense, **EMERGENCY_APPROVE}))
    res = client.post("/api/pharmacy/sales", json=sale)
    assert res.status_code == 201, res.text
    assert _available_stock(client, med_id) == 98.0
    rows = _audit_rows(pharm_db, hospital_id, "pharmacy_sale", "emergency_override")
    assert len(rows) == 1
    assert rows[0].details["actor_user_id"] == str(pharmacist.id)
    assert rows[0].details["reason"].startswith("Unconscious walk-in")


# ── Admission financial policy ─────────────────────────────────────────────


def test_emergency_bypass_disallowed_blocks_bed_override(
    api, db_session: Session, hospital: Hospital, patient: Patient, doctor: HospitalUser, ward_bed
):
    ward, room, bed = ward_bed
    admin = _claims(_staff(db_session, hospital.id, "Admin Staff"), hospital.id, role="hospital_admin")
    set_admission_financial_policy(
        db_session, hospital.id, AdmissionFinancialPolicyUpdate(emergency_bypass_allowed=False), admin
    )
    policy_rows = _audit_rows(db_session, hospital.id, "admission_financial_policy", "update")
    assert len(policy_rows) == 1
    assert policy_rows[0].details["changed_fields"] == ["emergency_bypass_allowed"]

    consultant = _er_consultant(db_session, hospital.id)
    adm, _ = EnsureAdmissionRequestAction(db_session).execute(
        hospital_id=hospital.id, patient_id=patient.id, actor=consultant, doctor_id=doctor.id,
        notes="Head injury",
    )
    adm_id = adm.id
    with pytest.raises(HTTPException) as blocked:
        AcceptAdmissionAction(db_session).execute(
            hospital_id=hospital.id,
            actor=consultant,
            admission_id=adm_id,
            ward_id=ward.id,
            room_id=room.id,
            bed_id=bed.id,
            is_emergency_override=True,
            emergency_override_reason="GCS 8, needs immediate monitored bed",
        )
    assert blocked.value.status_code == 403
    db_session.rollback()
    assert db_session.get(Admission, adm_id).status == AdmissionStatus.requested
    assert db_session.get(Bed, bed.id).is_occupied is False

    res = api(consultant).post(
        "/api/billing/exceptions/emergency-override",
        json={
            "patient_id": str(patient.id),
            "admission_id": str(adm_id),
            "service_type": "admission_bed",
            "action_type": "bed_allocation",
            "reason": "GCS 8, needs immediate monitored bed",
        },
    )
    assert res.status_code == 403, res.text

    # Service overrides are governed by permission only, not by the admission policy flag.
    actor = authorize_emergency_override(
        db_session,
        hospital.id,
        consultant,
        scope=OverrideScope.service("laboratory", uuid.uuid4(), patient_id=patient.id),
        reason="Stat lactate for septic shock",
    )
    assert actor.granted_by == "emergency:approve"
