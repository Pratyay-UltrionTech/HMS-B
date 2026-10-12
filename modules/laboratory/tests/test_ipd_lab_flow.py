"""
IPD laboratory flow end to end, through the real admission and billing actions.

Admission request → advance deposit → bed allocation → doctor prescription with lab
tests on the admission → lab request + charge on the admission's IPD account →
deposit explicitly allocated to that charge → accession → specimen collection →
results → completion. Prescription edits and order sets on the same admission bill
the same IPD account.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from modules.beds.entities.bed import Bed, Room, Ward
from modules.billing.actions.billing_actions import CreateDepositAction
from modules.billing.contracts.billing_contracts import BillingDepositCreate, BillingPaymentMethod
from modules.billing.entities.billing_entities import (
    BillingChargeStatus,
    BillingDeposit,
    BillingSourceType,
)
from modules.billing.services.billing_service import draw_down_deposit_for_charges
from modules.billing.services.service_charge_resolver import resolve_active_service_charge
from modules.clinical_decision.actions.clinical_decision_actions import ClinicalDecisionActions
from modules.clinical_decision.contracts.clinical_decision_contracts import (
    ApplyOrderSetRequest,
    ClinicalOrderSetCreate,
    ClinicalOrderSetItemInput,
)
from modules.clinical_decision.entities.clinical_decision_entities import ClinicalOrderType
from modules.clinical_records.actions.clinical_actions import (
    CreatePrescriptionAction,
    UpdatePrescriptionAction,
)
from modules.clinical_records.contracts.clinical_contracts import PrescriptionCreate, PrescriptionUpdate
from modules.doctors.actions.doctor_appointment_actions import DoctorRequestIpdAdmissionAction
from modules.doctors.contracts.doctor_contracts import DoctorIpdAdmissionRequest
from modules.inpatient.actions.admission_lifecycle_actions import AcceptAdmissionAction
from modules.inpatient.entities.admission import AdmissionStatus
from modules.laboratory.actions.laboratory_actions import (
    CollectSampleAction,
    CreateLabOrderAction,
    SaveLabResultsAction,
)
from modules.laboratory.contracts.lab_contracts import (
    LabOrderCreate,
    LabReportSaveRequest,
    LabResultInput,
    SampleCollectRequest,
)
from modules.laboratory.entities.lab_entities import (
    LabOrder,
    LabOrderStatus,
    LabPrescriptionRequest,
    LabSampleType,
    LabSpecimen,
    LabTestCatalog,
)
from modules.radiology.entities.radiology_entities import RadiologyScanCatalog
from tests.conftest import db_session, doctor, hospital, patient  # noqa: F401


def _lab_test(db: Session, hospital_id, code: str, price: float) -> LabTestCatalog:
    t = LabTestCatalog(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        test_code=code,
        test_name=f"Test {code}",
        department="Biochemistry",
        price=price,
        sample_type=LabSampleType.blood,
        tat_hours=4,
        is_active=True,
    )
    db.add(t)
    return t


@pytest.fixture
def ipd(db_session: Session, hospital, doctor, patient):
    db = db_session
    ward = Ward(
        id=uuid.uuid4(), hospital_id=hospital.id, name="Medical Ward", ward_type="general",
        admission_fee=500.0, bed_charge_per_day=1500.0, is_active=True,
    )
    room = Room(id=uuid.uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_code="MW-1", is_active=True)
    bed = Bed(
        id=uuid.uuid4(), hospital_id=hospital.id, ward_id=ward.id, room_id=room.id,
        bed_code="MW-1-A", is_occupied=False, is_active=True,
    )
    db.add_all([ward, room, bed])
    cbc = _lab_test(db, hospital.id, "CBC", 350.0)
    crp = _lab_test(db, hospital.id, "CRP", 650.0)
    lft = _lab_test(db, hospital.id, "LFT", 800.0)
    db.commit()

    doctor_ctx = {"user_id": str(doctor.id), "doctor_id": str(doctor.id), "name": "Dr. Sarah Admitting"}
    req = DoctorIpdAdmissionRequest(clinical_reason="Community-acquired pneumonia")
    adm = DoctorRequestIpdAdmissionAction(db).execute(hospital.id, doctor.id, patient.id, req, doctor_ctx)

    cashier = {"user_id": str(uuid.uuid4()), "name": "Cashier Rahul"}
    dep = CreateDepositAction(db, hospital.id).execute(
        BillingDepositCreate(
            patient_id=patient.id, admission_id=adm.admission_id, amount=5000.0,
            payment_method=BillingPaymentMethod.cash, deposit_type="ipd_advance",
        ),
        cashier,
    )
    accepted = AcceptAdmissionAction(db).execute(
        hospital_id=hospital.id,
        actor={"user_id": str(uuid.uuid4()), "name": "Nurse Priya"},
        admission_id=adm.admission_id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
    )
    assert accepted.status == AdmissionStatus.admitted
    return {
        "db": db, "h": hospital, "doc": doctor, "p": patient, "doctor_ctx": doctor_ctx,
        "admission_id": adm.admission_id, "account_id": adm.financial_account_id,
        "deposit_id": dep.id, "cbc": cbc, "crp": crp, "lft": lft,
    }


def _prescribe(env, test_ids) -> uuid.UUID:
    rx = CreatePrescriptionAction(env["db"]).execute(
        env["h"].id,
        env["doc"].id,
        PrescriptionCreate(
            patient_id=env["p"].id,
            admission_id=env["admission_id"],
            symptoms="Fever, productive cough",
            diagnosis="Community-acquired pneumonia",
            medicines="Ceftriaxone",
            dosage="1 g IV OD",
            test_ids=test_ids,
        ),
        env["doctor_ctx"],
    )
    return rx.id


def test_ipd_prescription_lab_flow_bills_ipd_account_and_completes(ipd):
    db, h, p = ipd["db"], ipd["h"], ipd["p"]
    lab_user = {"user_id": str(uuid.uuid4()), "name": "Lab Tech Anil", "role": "hospital_admin"}

    rx_id = _prescribe(ipd, [ipd["cbc"].id, ipd["crp"].id])
    req = db.query(LabPrescriptionRequest).filter(LabPrescriptionRequest.prescription_id == rx_id).one()
    assert req.admission_id == ipd["admission_id"]

    charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, req.id)
    assert charge.source_id == req.id
    assert str(charge.account_id) == str(ipd["account_id"])
    assert float(charge.net_amount) == 1000.0 and charge.status == BillingChargeStatus.pending

    # The admission's advance is explicitly allocated to the lab charge.
    applied = draw_down_deposit_for_charges(
        db, hospital_id=h.id, deposit_id=ipd["deposit_id"],
        allocations_plan=[{"charge_id": charge.id, "amount": 1000.0}], created_by_name="Cashier Rahul",
    )
    db.commit()
    db.refresh(charge)
    assert applied == 1000.0
    assert charge.status == BillingChargeStatus.paid

    # Accession (this used to 500 with NameError: Admission) → collection → results.
    resp = CreateLabOrderAction(db, h.id).execute(
        LabOrderCreate(patient_id=p.id, prescription_request_id=req.id), lab_user
    )
    order = db.get(LabOrder, resp.id)
    assert order.admission_id == ipd["admission_id"]
    assert db.query(LabSpecimen).filter(LabSpecimen.order_id == order.id).count() >= 1

    CollectSampleAction(db, h.id).execute(order.id, SampleCollectRequest(collected_by="Ward Nurse"), lab_user)
    done = SaveLabResultsAction(db, h.id).execute(
        order.id,
        LabReportSaveRequest(
            results=[
                LabResultInput(parameter_name="Hemoglobin", result_value="12.8", unit="g/dL"),
                LabResultInput(parameter_name="CRP", result_value="48", unit="mg/L"),
            ],
            mark_completed=True,
        ),
        lab_user,
    )
    assert done.status == LabOrderStatus.completed
    assert done.payment_status == "paid"
    db.refresh(req)
    assert req.status.value == "completed"


def test_ipd_prescription_edit_and_order_set_bill_ipd_account(ipd):
    db, h, p = ipd["db"], ipd["h"], ipd["p"]

    rx_id = _prescribe(ipd, [ipd["cbc"].id])
    UpdatePrescriptionAction(db).execute(
        h.id, ipd["doc"].id, rx_id, PrescriptionUpdate(test_ids=[ipd["cbc"].id, ipd["crp"].id]),
        ipd["doctor_ctx"],
    )
    reqs = db.query(LabPrescriptionRequest).filter(LabPrescriptionRequest.prescription_id == rx_id).all()
    assert len(reqs) == 1
    charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, reqs[0].id)
    assert float(charge.net_amount) == 1000.0
    assert str(charge.account_id) == str(ipd["account_id"])

    # Order set applied on the admission: lab order with specimens + radiology order,
    # both billed on the IPD account.
    scan = RadiologyScanCatalog(
        id=uuid.uuid4(), hospital_id=h.id, scan_code="CXR", scan_name="Chest X-Ray", category="X-Ray",
        department="Radiology", price=500.0, duration_minutes=10, is_active=True,
    )
    db.add(scan)
    db.commit()
    actions = ClinicalDecisionActions(db, h.id, ipd["doctor_ctx"])
    order_set = actions.create_order_set(
        ClinicalOrderSetCreate(
            code="CAP-BUNDLE",
            name="Pneumonia bundle",
            items=[
                ClinicalOrderSetItemInput(
                    order_type=ClinicalOrderType.lab, lab_test_catalog_id=ipd["lft"].id, item_name="LFT"
                ),
                ClinicalOrderSetItemInput(
                    order_type=ClinicalOrderType.radiology, radiology_scan_catalog_id=scan.id, item_name="CXR"
                ),
            ],
        )
    )
    result = actions.apply_order_set(
        order_set.id, ApplyOrderSetRequest(patient_id=p.id, admission_id=ipd["admission_id"])
    )
    lab_order = db.get(LabOrder, result.lab_order_id)
    assert lab_order.admission_id == ipd["admission_id"]
    assert db.query(LabSpecimen).filter(LabSpecimen.order_id == lab_order.id).count() == 1
    lab_charge = resolve_active_service_charge(db, h.id, BillingSourceType.laboratory, lab_order.id)
    assert lab_charge.source_id == lab_order.id
    assert float(lab_charge.net_amount) == 800.0
    assert str(lab_charge.account_id) == str(ipd["account_id"])

    assert len(result.radiology_order_ids) == 1
    rad_charge = resolve_active_service_charge(db, h.id, BillingSourceType.radiology, result.radiology_order_ids[0])
    assert float(rad_charge.net_amount) == 500.0
    assert str(rad_charge.account_id) == str(ipd["account_id"])

    # The advance is still intact: nothing was silently drawn.
    deposit = db.get(BillingDeposit, ipd["deposit_id"])
    assert float(deposit.available_amount) > 0
