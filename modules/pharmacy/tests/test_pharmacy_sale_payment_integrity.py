"""P0.12: pharmacy sales never fabricate payments.

A sale without an explicitly tendered ``amount_paid`` creates no BillingPayment;
it is dispensed only when the same account's deposits cover it (oldest first),
otherwise the financial gate refuses it with 402 and no stock moves.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import tests.conftest  # noqa: F401  (registers every ORM model on Base.metadata)
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingDeposit,
    BillingPayment,
    BillingPaymentAllocation,
    FinancialAccountType,
)
from modules.billing.services.billing_service import create_deposit, get_or_create_financial_account
from modules.patients.entities.patient import Patient
from modules.pharmacy.entities.pharmacy_entities import PharmacySale
from modules.pharmacy.tests.test_pharmacy import pharm_client, pharm_db  # noqa: F401  (fixtures)


def _stock_medicine(client, *, price: float = 10.0, qty: float = 100.0) -> str:
    supp = client.post("/api/pharmacy/suppliers", json={"company_name": f"Supplier {uuid4().hex[:6]}"}).json()
    med = client.post(
        "/api/pharmacy/medicines",
        json={
            "medicine_name": f"Paracetamol {uuid4().hex[:4]}",
            "brand_name": "Crocin",
            "manufacturer": "GSK",
            "gst_percent": 0.0,
            "selling_price": price,
            "purchase_price": price / 2,
            "mrp": price,
        },
    ).json()
    res = client.post(
        "/api/pharmacy/purchases",
        json={
            "supplier_id": supp["id"],
            "invoice_number": f"PUR-{uuid4().hex[:6]}",
            "invoice_date": str(date.today()),
            "received_date": str(date.today()),
            "items": [
                {
                    "medicine_id": med["id"],
                    "batch_number": f"B-{uuid4().hex[:4]}",
                    "expiry_date": str(date.today() + timedelta(days=365)),
                    "purchase_price": price / 2,
                    "mrp": price,
                    "selling_price": price,
                    "quantity": qty,
                }
            ],
        },
    )
    assert res.status_code == 201, res.text
    return med["id"]


def _available_stock(client, medicine_id: str) -> float:
    rows = client.get("/api/pharmacy/inventory").json()
    return next(r["available_stock"] for r in rows if r["medicine_id"] == medicine_id)


def _patient(db, hospital_id: UUID) -> Patient:
    p = Patient(
        id=uuid4(),
        hospital_id=hospital_id,
        uhid=f"UHID-{uuid4().hex[:6]}",
        first_name="Meera",
        last_name="Iyer",
        name="Meera Iyer",
        mobile="9000000001",
        age=40,
        gender="female",
        created_at=datetime.now(timezone.utc),
    )
    db.add(p)
    db.commit()
    return p


def test_walk_in_sale_without_amount_paid_is_refused_and_stock_untouched(pharm_client, pharm_db):
    client, _hospital_id, _ = pharm_client
    med_id = _stock_medicine(client)

    res = client.post(
        "/api/pharmacy/sales",
        json={
            "customer_name": "Walk-in",
            "customer_phone": "9876500000",
            "items": [{"medicine_id": med_id, "quantity": 5.0}],
        },
    )
    assert res.status_code == 402
    assert pharm_db.query(BillingPayment).count() == 0
    assert _available_stock(client, med_id) == 100.0


def test_patient_sale_without_amount_paid_creates_no_payment_and_402_when_unfunded(pharm_client, pharm_db):
    client, hospital_id, _ = pharm_client
    med_id = _stock_medicine(client)
    patient = _patient(pharm_db, hospital_id)

    res = client.post(
        "/api/pharmacy/sales",
        json={
            "customer_name": patient.name,
            "customer_phone": "9000000001",
            "patient_id": str(patient.id),
            "items": [{"medicine_id": med_id, "quantity": 5.0}],
        },
    )
    assert res.status_code == 402, res.text
    assert pharm_db.query(BillingPayment).count() == 0
    assert pharm_db.query(PharmacySale).count() == 0  # whole sale rolled back
    assert _available_stock(client, med_id) == 100.0


def test_patient_sale_with_tendered_cash_records_real_payment(pharm_client, pharm_db):
    client, hospital_id, _ = pharm_client
    med_id = _stock_medicine(client)
    patient = _patient(pharm_db, hospital_id)

    res = client.post(
        "/api/pharmacy/sales",
        json={
            "customer_name": patient.name,
            "customer_phone": "9000000001",
            "patient_id": str(patient.id),
            "amount_paid": 50.0,
            "items": [{"medicine_id": med_id, "quantity": 5.0}],
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["payment_status"] == "paid"
    pays = pharm_db.query(BillingPayment).all()
    assert len(pays) == 1 and float(pays[0].amount) == 50.0
    assert _available_stock(client, med_id) == 95.0


def test_ipd_sale_covered_by_multiple_same_account_deposits_oldest_first(pharm_client, pharm_db):
    client, hospital_id, _ = pharm_client
    med_id = _stock_medicine(client, price=10.0)
    patient = _patient(pharm_db, hospital_id)

    admission_id = uuid4()
    acc = get_or_create_financial_account(
        pharm_db,
        hospital_id=hospital_id,
        patient_id=patient.id,
        account_type=FinancialAccountType.ipd,
        admission_id=admission_id,
    )
    other_acc = get_or_create_financial_account(
        pharm_db,
        hospital_id=hospital_id,
        patient_id=patient.id,
        account_type=FinancialAccountType.ipd,
        admission_id=uuid4(),
    )
    old_dep = create_deposit(
        pharm_db, hospital_id=hospital_id, patient_id=patient.id, amount=30.0,
        deposit_date=date.today(), account_id=acc.id,
    )
    pharm_db.flush()
    new_dep = create_deposit(
        pharm_db, hospital_id=hospital_id, patient_id=patient.id, amount=500.0,
        deposit_date=date.today(), account_id=acc.id,
    )
    foreign_dep = create_deposit(
        pharm_db, hospital_id=hospital_id, patient_id=patient.id, amount=1000.0,
        deposit_date=date.today(), account_id=other_acc.id,
    )
    pharm_db.commit()

    res = client.post(
        "/api/pharmacy/sales",
        json={
            "customer_name": patient.name,
            "customer_phone": "9000000001",
            "patient_id": str(patient.id),
            "admission_id": str(admission_id),
            "items": [{"medicine_id": med_id, "quantity": 10.0}],
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["net_amount"] == 100.0
    assert body["payment_status"] == "paid"
    assert body["amount_paid"] == 100.0

    assert pharm_db.query(BillingPayment).count() == 0
    pharm_db.expire_all()
    charge = pharm_db.query(BillingCharge).filter(BillingCharge.id == UUID(body["billing_charge_id"])).one() \
        if body.get("billing_charge_id") else pharm_db.query(BillingCharge).one()
    assert charge.status == BillingChargeStatus.paid
    assert charge.account_id == acc.id

    allocs = {
        a.deposit_id: float(a.allocated_amount)
        for a in pharm_db.query(BillingPaymentAllocation).filter(BillingPaymentAllocation.charge_id == charge.id)
    }
    assert allocs == {old_dep.id: 30.0, new_dep.id: 70.0}
    assert float(pharm_db.get(BillingDeposit, old_dep.id).available_amount) == 0.0
    assert float(pharm_db.get(BillingDeposit, new_dep.id).available_amount) == 430.0
    assert float(pharm_db.get(BillingDeposit, foreign_dep.id).available_amount) == 1000.0
    assert _available_stock(client, med_id) == 90.0


def test_ipd_sale_not_funded_by_another_accounts_deposit(pharm_client, pharm_db):
    client, hospital_id, _ = pharm_client
    med_id = _stock_medicine(client, price=10.0)
    patient = _patient(pharm_db, hospital_id)

    admission_id = uuid4()
    get_or_create_financial_account(
        pharm_db, hospital_id=hospital_id, patient_id=patient.id,
        account_type=FinancialAccountType.ipd, admission_id=admission_id,
    )
    other_acc = get_or_create_financial_account(
        pharm_db, hospital_id=hospital_id, patient_id=patient.id,
        account_type=FinancialAccountType.ipd, admission_id=uuid4(),
    )
    create_deposit(
        pharm_db, hospital_id=hospital_id, patient_id=patient.id, amount=1000.0,
        deposit_date=date.today(), account_id=other_acc.id,
    )
    pharm_db.commit()

    res = client.post(
        "/api/pharmacy/sales",
        json={
            "customer_name": patient.name,
            "customer_phone": "9000000001",
            "patient_id": str(patient.id),
            "admission_id": str(admission_id),
            "items": [{"medicine_id": med_id, "quantity": 10.0}],
        },
    )
    assert res.status_code == 402, res.text
    assert _available_stock(client, med_id) == 100.0
