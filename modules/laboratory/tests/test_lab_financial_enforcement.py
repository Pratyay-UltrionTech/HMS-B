"""
Lab execution-step financial gating and stable charge linkage.

- Every execution step (collection, processing, results) is gated on the order's
  resolved charge; unpaid → 402 with the structured clearance body.
- Results need collected specimens; amending completed results is not re-gated.
- An authorized emergency override is scoped to the order: it unlocks every later
  step of that order and nothing else.
- Prescription requests own one stable charge (never re-pointed): re-accession after
  an order cancel, prescription edits and legacy re-pointed charges all resolve it.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingPaymentMethod,
    BillingSourceType,
)
from modules.billing.entities.financial_exception import (
    FinancialClearanceException,
    FinancialExceptionStatus,
)
from modules.billing.services.billing_service import create_payment, ensure_charge
from modules.billing.services.service_charge_resolver import (
    ensure_service_charge,
    resolve_active_service_charge,
    sync_service_charge_amount,
)
from modules.doctors.entities.doctor import HospitalUser
from modules.laboratory.actions.laboratory_actions import (
    CancelLabOrderAction,
    CollectSampleAction,
    CreateLabOrderAction,
    SaveLabResultsAction,
    UpdateItemStatusAction,
)
from modules.laboratory.contracts.lab_contracts import (
    ItemStatusUpdate,
    LabOrderCreate,
    LabReportSaveRequest,
    LabResultInput,
    SampleCollectRequest,
)
from modules.laboratory.entities.lab_entities import (
    LabItemStatus,
    LabOrder,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabPrescriptionRequestStatus,
    LabSampleType,
    LabTestCatalog,
)
from modules.laboratory.services.lab_prescription_service import (
    create_investigation_requests_for_prescription,
    get_prescription_investigation_ids,
    update_investigation_requests_for_prescription,
)
from tests.conftest import db_session, doctor, hospital, patient  # noqa: F401

EMERGENCY_APPROVE = {"emergency": {"can_view": True, "can_approve": True}}


# ── helpers ──────────────────────────────────────────────────────────────────


def _staff(db: Session, hospital_id: uuid.UUID, name: str) -> HospitalUser:
    user = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        role_id=uuid.uuid4(),
        name=name,
        phone="555-0142",
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


def _test(db: Session, hospital_id: uuid.UUID, code: str, price: float) -> LabTestCatalog:
    t = LabTestCatalog(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        test_code=code,
        test_name=f"Test {code}",
        department="Biochemistry",
        price=price,
        sample_type=LabSampleType.blood,
        tat_hours=6,
        is_active=True,
    )
    db.add(t)
    db.commit()
    return t


def _pay(db: Session, charge: BillingCharge, amount: float | None = None) -> None:
    amt = float(amount if amount is not None else float(charge.net_amount) - float(charge.amount_paid))
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


def _walk_in(db: Session, hospital_id, patient_id, test_ids, user) -> LabOrder:
    resp = CreateLabOrderAction(db, hospital_id).execute(
        LabOrderCreate(patient_id=patient_id, test_ids=test_ids), user
    )
    return db.get(LabOrder, resp.id)


def _results(mark_completed: bool = True, **extra) -> LabReportSaveRequest:
    return LabReportSaveRequest(
        results=[LabResultInput(parameter_name="Glucose", result_value="92", unit="mg/dL")],
        mark_completed=mark_completed,
        **extra,
    )


def _collect(**extra) -> SampleCollectRequest:
    return SampleCollectRequest(collected_by="Phlebotomist", **extra)


def _active_charges(db: Session, hospital_id, source_ids) -> list[BillingCharge]:
    return (
        db.query(BillingCharge)
        .filter(
            BillingCharge.hospital_id == hospital_id,
            BillingCharge.source_type == BillingSourceType.laboratory,
            BillingCharge.source_id.in_(list(source_ids)),
            BillingCharge.status != BillingChargeStatus.cancelled,
        )
        .all()
    )


@pytest.fixture
def lab_env(db_session: Session, hospital, patient):
    tech = _staff(db_session, hospital.id, "Lab Tech Anil")
    return {
        "db": db_session,
        "h": hospital,
        "p": patient,
        "user": _claims(tech, hospital.id),
        "glu": _test(db_session, hospital.id, "GLU", 200.0),
        "lft": _test(db_session, hospital.id, "LFT", 600.0),
        "tsh": _test(db_session, hospital.id, "TSH", 450.0),
    }


# ── gating ───────────────────────────────────────────────────────────────────


def test_unpaid_lab_execution_steps_return_structured_402(lab_env):
    db, h, p, user = lab_env["db"], lab_env["h"], lab_env["p"], lab_env["user"]
    order = _walk_in(db, h.id, p.id, [lab_env["glu"].id], user)

    with pytest.raises(HTTPException) as exc:
        CollectSampleAction(db, h.id).execute(order.id, _collect(), user)
    assert exc.value.status_code == 402

    # Unpaid results entry is refused before anything else (even on an ordered order).
    with pytest.raises(HTTPException) as exc:
        SaveLabResultsAction(db, h.id).execute(order.id, _results(), user)
    assert exc.value.status_code == 402
    detail = exc.value.detail
    assert detail["code"] == "FINANCIAL_CLEARANCE_REQUIRED"
    assert detail["service_type"] == "laboratory"
    assert detail["source_id"] == str(order.id)
    assert detail["outstanding_amount"] == 200.0
    assert detail["charge_id"]

    # Processing an item is gated too (sample already in the lab, charge unpaid).
    db.rollback()
    order = db.get(LabOrder, order.id)
    order.status = LabOrderStatus.sample_collected
    db.commit()
    with pytest.raises(HTTPException) as exc:
        UpdateItemStatusAction(db, h.id).execute(
            order.id, order.items[0].id, ItemStatusUpdate(status=LabItemStatus.processing), user
        )
    assert exc.value.status_code == 402
    db.rollback()
    assert db.get(LabOrder, order.id).results == []


def test_paid_lab_flow_end_to_end_and_amendment_not_regated(lab_env):
    db, h, p, user = lab_env["db"], lab_env["h"], lab_env["p"], lab_env["user"]
    order = _walk_in(db, h.id, p.id, [lab_env["glu"].id], user)
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, order.id)
    assert charge.source_id == order.id

    # Results before the sample is collected are refused even when paid.
    _pay(db, charge)
    with pytest.raises(HTTPException) as exc:
        SaveLabResultsAction(db, h.id).execute(order.id, _results(mark_completed=False), user)
    assert exc.value.status_code == 409
    db.rollback()

    resp = CollectSampleAction(db, h.id).execute(order.id, _collect(), user)
    assert resp.status == LabOrderStatus.sample_collected
    assert resp.payment_status == "paid"
    assert resp.net_amount == 200.0 and resp.amount_paid == 200.0 and resp.outstanding_amount == 0.0

    order = db.get(LabOrder, order.id)
    resp = UpdateItemStatusAction(db, h.id).execute(
        order.id, order.items[0].id, ItemStatusUpdate(status=LabItemStatus.processing), user
    )
    assert resp.status == LabOrderStatus.in_progress
    resp = SaveLabResultsAction(db, h.id).execute(order.id, _results(), user)
    assert resp.status == LabOrderStatus.completed

    # A later price correction leaves the charge partially paid; amending the already
    # completed (and previously cleared) results must not be blocked by that.
    sync_service_charge_amount(
        db, hospital_id=h.id, patient_id=p.id, source_type=BillingSourceType.laboratory,
        source_id=order.id, charge_amount=260.0, description="Lab re-priced", reason="price correction",
    )
    db.commit()
    resp = SaveLabResultsAction(db, h.id).execute(
        order.id, _results(amendment_reason="Transcription error corrected"), user
    )
    assert resp.is_amended is True
    assert resp.payment_status == "partially_paid"


def test_emergency_override_unlocks_every_step_of_that_order_only(lab_env):
    db, h, p, user = lab_env["db"], lab_env["h"], lab_env["p"], lab_env["user"]
    er_doc = _claims(_staff(db, h.id, "Dr. ER Consultant"), h.id, EMERGENCY_APPROVE)
    order = _walk_in(db, h.id, p.id, [lab_env["glu"].id], user)
    other = _walk_in(db, h.id, p.id, [lab_env["tsh"].id], user)

    # Without authority the override is refused.
    with pytest.raises(HTTPException) as exc:
        CollectSampleAction(db, h.id).execute(
            order.id, _collect(is_emergency_override=True, emergency_override_reason="Sepsis workup"), user
        )
    assert exc.value.status_code == 403
    db.rollback()

    CollectSampleAction(db, h.id).execute(
        order.id, _collect(is_emergency_override=True, emergency_override_reason="Sepsis workup"), er_doc
    )
    exceptions = (
        db.query(FinancialClearanceException)
        .filter(FinancialClearanceException.source_id == order.id)
        .all()
    )
    assert len(exceptions) == 1
    assert exceptions[0].status == FinancialExceptionStatus.consumed.value

    # Later steps of the same order, by a different (non-authorized) user, no override flag.
    order = db.get(LabOrder, order.id)
    UpdateItemStatusAction(db, h.id).execute(
        order.id, order.items[0].id, ItemStatusUpdate(status=LabItemStatus.processing), user
    )
    resp = SaveLabResultsAction(db, h.id).execute(order.id, _results(), user)
    assert resp.status == LabOrderStatus.completed
    # The receivable is preserved: nothing was marked paid.
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, order.id)
    assert charge.status == BillingChargeStatus.pending and float(charge.amount_paid) == 0.0
    # One override, reused — not one per step.
    assert db.query(FinancialClearanceException).filter(
        FinancialClearanceException.source_id == order.id
    ).count() == 1

    # Another order of the same patient is not unlocked.
    with pytest.raises(HTTPException) as exc:
        CollectSampleAction(db, h.id).execute(other.id, _collect(), user)
    assert exc.value.status_code == 402


# ── stable charge linkage ────────────────────────────────────────────────────


def _request(db, h, p, doctor, test_ids, admission_id=None) -> LabPrescriptionRequest:
    lab_req, _ = create_investigation_requests_for_prescription(
        db,
        hospital_id=h.id,
        doctor_id=doctor.id,
        patient_id=p.id,
        appointment_id=None,
        prescription_id=uuid.uuid4(),
        admission_id=admission_id,
        test_ids=test_ids,
    )
    db.commit()
    return lab_req


def _accession(db, h, p, req, user):
    resp = CreateLabOrderAction(db, h.id).execute(
        LabOrderCreate(patient_id=p.id, prescription_request_id=req.id), user
    )
    return db.get(LabOrder, resp.id)


def test_reaccession_after_order_cancel_reuses_the_paid_request_charge(lab_env, doctor):
    db, h, p, user = lab_env["db"], lab_env["h"], lab_env["p"], lab_env["user"]
    req = _request(db, h, p, doctor, [lab_env["glu"].id])
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, req.id)
    _pay(db, charge)

    first = _accession(db, h, p, req, user)
    CancelLabOrderAction(db, h.id).execute(first.id, user)
    db.refresh(req)
    db.refresh(charge)
    assert req.status == LabPrescriptionRequestStatus.pending
    assert charge.status == BillingChargeStatus.paid and charge.source_id == req.id

    # Re-accession needs no new payment and bills nothing twice.
    second = _accession(db, h, p, req, user)
    assert second.id != first.id
    CollectSampleAction(db, h.id).execute(second.id, _collect(), user)
    assert len(_active_charges(db, h.id, [req.id, first.id, second.id])) == 1

    # Cancelling the whole request while its order is live is refused (409).
    from modules.laboratory.actions.laboratory_actions import CancelLabPrescriptionRequestAction

    with pytest.raises(HTTPException) as exc:
        CancelLabPrescriptionRequestAction(db, h.id).execute(req.id, "no longer needed", user)
    assert exc.value.status_code == 409


def test_legacy_repointed_charge_still_resolves_for_request_and_orders(lab_env, doctor):
    db, h, p, user = lab_env["db"], lab_env["h"], lab_env["p"], lab_env["user"]
    req = _request(db, h, p, doctor, [lab_env["glu"].id])
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, req.id)
    _pay(db, charge)
    order = _accession(db, h, p, req, user)

    # Simulate the pre-fix behavior: the paid charge was moved to the order id.
    charge.source_id = order.id
    db.commit()

    assert resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, req.id).id == charge.id
    CancelLabOrderAction(db, h.id).execute(order.id, user)
    db.refresh(charge)
    assert charge.status == BillingChargeStatus.paid

    again = _accession(db, h, p, req, user)
    assert resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, again.id).id == charge.id
    CollectSampleAction(db, h.id).execute(again.id, _collect(), user)
    assert len(_active_charges(db, h.id, [req.id, order.id, again.id])) == 1


def test_prescription_edit_before_accession_reprices_single_charge_with_credit(lab_env, doctor):
    db, h, p = lab_env["db"], lab_env["h"], lab_env["p"]
    glu, lft, tsh = lab_env["glu"], lab_env["lft"], lab_env["tsh"]
    req = _request(db, h, p, doctor, [glu.id, lft.id])
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, req.id)
    assert float(charge.net_amount) == 800.0
    _pay(db, charge)

    # Doctor drops LFT: the paid charge is re-priced down; the excess becomes credit.
    update_investigation_requests_for_prescription(
        db, hospital_id=h.id, doctor_id=doctor.id, patient_id=p.id, appointment_id=None,
        prescription_id=req.prescription_id, test_ids=[glu.id], panel_ids=[],
    )
    db.commit()
    db.refresh(charge)
    assert float(charge.net_amount) == 200.0
    assert float(charge.amount_paid) == 200.0 and charge.status == BillingChargeStatus.paid
    credit = (
        db.query(BillingDeposit)
        .filter(BillingDeposit.patient_id == p.id, BillingDeposit.deposit_type == "cancellation_credit")
        .one()
    )
    assert float(credit.available_amount) == 600.0

    # Doctor adds TSH before accession: same request, same charge, re-priced up.
    update_investigation_requests_for_prescription(
        db, hospital_id=h.id, doctor_id=doctor.id, patient_id=p.id, appointment_id=None,
        prescription_id=req.prescription_id, test_ids=[glu.id, tsh.id], panel_ids=[],
    )
    db.commit()
    db.refresh(charge)
    assert float(charge.net_amount) == 650.0
    assert charge.status == BillingChargeStatus.partially_paid
    assert len(_active_charges(db, h.id, [req.id])) == 1
    assert db.query(LabPrescriptionRequest).filter(
        LabPrescriptionRequest.prescription_id == req.prescription_id
    ).count() == 1


def test_prescription_edit_after_accession_keeps_order_and_bills_addition_separately(lab_env, doctor):
    db, h, p, user = lab_env["db"], lab_env["h"], lab_env["p"], lab_env["user"]
    glu, lft, tsh = lab_env["glu"], lab_env["lft"], lab_env["tsh"]
    req = _request(db, h, p, doctor, [glu.id, lft.id])
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, req.id)
    _pay(db, charge)
    order = _accession(db, h, p, req, user)

    # LFT removed after accession (already in the lab → kept), TSH added.
    update_investigation_requests_for_prescription(
        db, hospital_id=h.id, doctor_id=doctor.id, patient_id=p.id, appointment_id=None,
        prescription_id=req.prescription_id, test_ids=[glu.id, tsh.id], panel_ids=[],
    )
    db.commit()

    reqs = (
        db.query(LabPrescriptionRequest)
        .filter(LabPrescriptionRequest.prescription_id == req.prescription_id)
        .order_by(LabPrescriptionRequest.created_at.asc())
        .all()
    )
    assert len(reqs) == 2
    supplementary = next(r for r in reqs if r.id != req.id)
    assert [i.test_code for i in supplementary.items] == ["TSH"]

    # Exactly one active charge per request; the in-flight paid order keeps clearance.
    db.refresh(charge)
    assert charge.status == BillingChargeStatus.paid and float(charge.net_amount) == 800.0
    for r in reqs:
        assert len(_active_charges(db, h.id, [r.id])) == 1
    supp_charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, supplementary.id)
    assert float(supp_charge.net_amount) == 450.0 and supp_charge.status == BillingChargeStatus.pending
    CollectSampleAction(db, h.id).execute(order.id, _collect(), user)

    # The edit form sees the union of every live request.
    test_ids, _, _ = get_prescription_investigation_ids(db, req.prescription_id)
    assert set(test_ids) == {glu.id, lft.id, tsh.id}

    # Re-submitting the same selection changes nothing.
    update_investigation_requests_for_prescription(
        db, hospital_id=h.id, doctor_id=doctor.id, patient_id=p.id, appointment_id=None,
        prescription_id=req.prescription_id, test_ids=list(test_ids), panel_ids=[],
    )
    db.commit()
    assert db.query(LabPrescriptionRequest).filter(
        LabPrescriptionRequest.prescription_id == req.prescription_id
    ).count() == 2


def test_walk_in_charge_is_idempotent_and_db_rejects_duplicates(lab_env):
    db, h, p, user = lab_env["db"], lab_env["h"], lab_env["p"], lab_env["user"]
    order = _walk_in(db, h.id, p.id, [lab_env["glu"].id], user)
    first = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, order.id)
    again = ensure_service_charge(
        db, hospital_id=h.id, patient_id=p.id, source_type=BillingSourceType.laboratory,
        source_id=order.id, description="dup", charge_amount=200.0,
    )
    assert again.id == first.id

    # A raw second active charge on the same key violates the partial unique index.
    db.add(
        BillingCharge(
            hospital_id=h.id, patient_id=p.id, source_type=BillingSourceType.laboratory,
            source_id=order.id, description="raw duplicate", quantity=1, unit_price=200,
            charge_amount=200, net_amount=200, amount_paid=0, status=BillingChargeStatus.pending,
            created_by_name="test",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    # A cancelled charge does not block a fresh one on the same key.
    first = db.get(BillingCharge, first.id)
    first.status = BillingChargeStatus.cancelled
    db.flush()
    fresh = ensure_charge(
        db, hospital_id=h.id, patient_id=p.id, source_type=BillingSourceType.laboratory,
        source_id=order.id, description="re-billed", charge_amount=200.0,
    )
    assert fresh.id != first.id
    db.rollback()


def test_mark_unavailable_reprices_request_charge(lab_env, doctor):
    db, h, p, user = lab_env["db"], lab_env["h"], lab_env["p"], lab_env["user"]
    from modules.laboratory.actions.laboratory_actions import MarkLabRequestItemUnavailableAction

    req = _request(db, h, p, doctor, [lab_env["glu"].id, lab_env["lft"].id])
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, req.id)
    lft_item = next(i for i in req.items if i.test_code == "LFT")
    resp = MarkLabRequestItemUnavailableAction(db, h.id).execute(req.id, lft_item.id, user)
    assert resp.status == LabPrescriptionRequestStatus.pending
    db.refresh(charge)
    assert float(charge.net_amount) == 200.0 and charge.status == BillingChargeStatus.pending
