from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from modules.billing.tests.test_billing import billing_app, billing_client
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b, admin_headers, auth_headers_b
from modules.billing.entities.billing_entities import (
    BillingDeposit,
    BillingPaymentMethod,
    DepositStatus,
    BillingCharge,
    BillingSourceType,
    BillingChargeStatus,
)


@pytest.fixture
def deposit_rows(db_session, hospital, patient):
    rows = []
    for i in range(35):
        orig = (i + 1) * 100.0
        avail = orig - (50.0 if i % 2 == 0 else 0.0)
        status = DepositStatus.partially_allocated if i % 2 == 0 else DepositStatus.available
        row = BillingDeposit(
            hospital_id=hospital.id,
            patient_id=patient.id,
            deposit_number=f"DEP-{i:03d}",
            deposit_date=date(2026, 10, 1) + timedelta(days=i % 10),
            deposit_type="ipd_advance" if i % 2 else "general_deposit",
            payment_method=BillingPaymentMethod.cash if i % 2 else BillingPaymentMethod.upi,
            original_amount=orig,
            available_amount=avail,
            status=status,
            reference_number=f"REF-{i:03d}",
            received_by_name="Alice" if i % 2 else "Bob",
            notes="advance deposit note " * 20,
            created_at=datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=i),
        )
        db_session.add(row)
        rows.append(row)
    db_session.commit()
    return rows


def test_default_deposit_page_is_bounded_and_summary_covers_all(
    billing_client, admin_headers, deposit_rows
):
    response = billing_client.get("/api/billing/deposits?page=1&page_size=15", headers=admin_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["items"]) == 15
    assert data["page"] == 1 and data["page_size"] == 15
    assert data["total"] == 35 and data["total_pages"] == 3
    expected_deposited = sum((i + 1) * 100.0 for i in range(35))
    expected_available = sum((i + 1) * 100.0 - (50.0 if i % 2 == 0 else 0.0) for i in range(35))
    assert data["total_deposited"] == expected_deposited
    assert data["total_available"] == expected_available
    # Authoritative net allocations: 0.0 because no payment allocations were attached in fixture
    assert data["total_allocated"] == 0.0
    assert data["total_refunded"] == 0.0
    assert data["items"][0]["deposit_number"] == "DEP-034"


def test_deposit_legacy_list_and_pagination(
    billing_client, admin_headers, deposit_rows, patient
):
    # Without page parameter: returns list for backward compatibility
    legacy_res = billing_client.get(f"/api/billing/deposits?patient_id={patient.id}", headers=admin_headers)
    assert legacy_res.status_code == 200
    legacy_data = legacy_res.json()
    assert isinstance(legacy_data, list)
    assert len(legacy_data) == 35

    # With page parameter: returns paginated page object
    p2 = billing_client.get(f"/api/billing/deposits?patient_id={patient.id}&page=2&page_size=15", headers=admin_headers)
    assert p2.status_code == 200
    p2_data = p2.json()
    assert isinstance(p2_data, dict)
    assert p2_data["page"] == 2
    assert len(p2_data["items"]) == 15


def test_allocate_deposit_authoritative_response(
    billing_client, admin_headers, db_session, hospital, patient
):
    # Create charge for 3000
    chg = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        source_type=BillingSourceType.other,
        description="General ward consultation",
        quantity=1.0,
        unit_price=3000.0,
        charge_amount=3000.0,
        net_amount=3000.0,
        amount_paid=0.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add(chg)

    # Create deposit with 5000
    dep = BillingDeposit(
        hospital_id=hospital.id,
        patient_id=patient.id,
        deposit_number="DEP-AUTH-001",
        deposit_date=date(2026, 10, 1),
        deposit_type="ipd_advance",
        payment_method=BillingPaymentMethod.cash,
        original_amount=5000.0,
        available_amount=5000.0,
        status=DepositStatus.available,
    )
    db_session.add(dep)
    db_session.commit()

    # Request 4500 allocation against 3000 charge
    alloc_res = billing_client.post(
        f"/api/billing/deposits/{dep.id}/allocate",
        headers=admin_headers,
        json={"allocations": [{"charge_id": str(chg.id), "amount": 4500.0}]},
    )
    assert alloc_res.status_code == 200
    data = alloc_res.json()
    # Backend authoritatively caps at charge due (3000)
    assert data["allocated_amount"] == 3000.0
    assert data["available_amount"] == 2000.0
    assert data["status"] == "partially_allocated"


def test_deposit_kpi_accounting_with_refunds_and_reversals(
    billing_client, admin_headers, db_session, hospital, patient, patient_b
):
    """Authoritative equation: total_deposited = total_available + total_allocated + total_refunded.
    Refunds reduce available without falsely inflating allocated. Normal payment refunds are excluded.
    """
    from modules.billing.services.billing_service import create_deposit, draw_down_deposit_for_charges, process_refund, create_payment
    from modules.billing.entities.billing_entities import BillingPaymentAllocation

    # 1. Deposit 10,000
    dep = create_deposit(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=10000.0,
        deposit_date=date(2026, 10, 1),
        deposit_type="general",
    )
    db_session.commit()

    # 2. Charge of 4,000 and allocate 4,000
    chg = BillingCharge(
        hospital_id=hospital.id,
        patient_id=patient.id,
        account_id=None,
        source_type=BillingSourceType.other,
        description="General Clinic Care",
        charge_amount=4000.0,
        net_amount=4000.0,
        amount_paid=0.0,
        status=BillingChargeStatus.pending,
    )
    db_session.add(chg)
    db_session.commit()

    allocated = draw_down_deposit_for_charges(
        db_session,
        hospital_id=hospital.id,
        deposit_id=dep.id,
        allocations_plan=[{"charge_id": chg.id, "amount": 4000.0}],
    )
    assert allocated == 4000.0
    db_session.commit()

    # 3. Refund 2,000 of the remaining available balance (6,000 -> 4,000 available)
    ref = process_refund(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        amount=2000.0,
        deposit_id=dep.id,
        reason="Unused advance balance refunded",
        refund_date=date(2026, 10, 2),
    )
    db_session.commit()

    # 4. Create an unrelated normal payment refund on patient_b to ensure it does not bleed into deposit total_refunded
    pay_b = create_payment(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient_b.id,
        amount=5000.0,
        payment_date=date(2026, 10, 2),
        payment_method=BillingPaymentMethod.cash,
        notes="Unrelated payment",
        received_by_name="Cashier",
    )
    db_session.commit()
    ref_b = process_refund(
        db_session,
        hospital_id=hospital.id,
        patient_id=patient_b.id,
        amount=1500.0,
        payment_id=pay_b.id,
        reason="Unrelated normal payment refund",
        refund_date=date(2026, 10, 2),
    )
    db_session.commit()

    # Query deposit pagination for patient
    res = billing_client.get(
        f"/api/billing/deposits?patient_id={patient.id}&page=1&page_size=15",
        headers=admin_headers,
    )
    assert res.status_code == 200
    data = res.json()

    assert data["total_deposited"] == 10000.0
    assert data["total_available"] == 4000.0
    assert data["total_allocated"] == 4000.0
    assert data["total_refunded"] == 2000.0
    # Equation invariant check
    assert data["total_deposited"] == round(data["total_available"] + data["total_allocated"] + data["total_refunded"], 2)

    # 5. Reverse 1,000 of the deposit-funded allocation (allocated becomes 3,000, available becomes 5,000)
    orig_alloc = db_session.query(BillingPaymentAllocation).filter(
        BillingPaymentAllocation.deposit_id == dep.id,
        BillingPaymentAllocation.allocated_amount > 0,
    ).first()
    rev_alloc = BillingPaymentAllocation(
        hospital_id=hospital.id,
        deposit_id=dep.id,
        charge_id=chg.id,
        allocated_amount=-1000.0,
        reversal_of_id=orig_alloc.id,
        reversal_reason="Partial dispute reversal back to deposit",
        created_by_name="Admin",
    )
    dep.available_amount = float(dep.available_amount) + 1000.0
    chg.amount_paid = float(chg.amount_paid) - 1000.0
    db_session.add(rev_alloc)
    db_session.commit()

    res2 = billing_client.get(
        f"/api/billing/deposits?patient_id={patient.id}&page=1&page_size=15",
        headers=admin_headers,
    )
    data2 = res2.json()
    assert data2["total_deposited"] == 10000.0
    assert data2["total_available"] == 5000.0
    assert data2["total_allocated"] == 3000.0
    assert data2["total_refunded"] == 2000.0
    assert data2["total_deposited"] == round(data2["total_available"] + data2["total_allocated"] + data2["total_refunded"], 2)

