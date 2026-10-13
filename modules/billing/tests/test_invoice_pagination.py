from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from modules.billing.tests.test_billing import billing_app, billing_client
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b, admin_headers, auth_headers_b
from modules.billing.entities.billing_entities import BillingInvoice, BillingInvoiceLine, BillingInvoiceStatus


@pytest.fixture
def invoice_rows(db_session, hospital, patient):
    rows = []
    for i in range(37):
        row = BillingInvoice(
            hospital_id=hospital.id,
            patient_id=patient.id,
            invoice_number=f"INV-{i:03}",
            invoice_date=date(2026, 10, 1) + timedelta(days=i % 10),
            subtotal=float(i + 1),
            discount_amount=0.0,
            taxable_amount=float(i + 1),
            tax_amount=0.0,
            grand_total=float(i + 1),
            status=BillingInvoiceStatus.paid if i % 2 == 0 else BillingInvoiceStatus.generated,
            notes="large invoice notes " * 500,
            created_by_name="Alice" if i % 2 == 0 else "Bob",
            created_at=datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=i),
        )
        db_session.add(row)
        db_session.flush()
        # Add 3 lines to each invoice to verify line items are NOT loaded in list query
        for ln_idx in range(3):
            line = BillingInvoiceLine(
                hospital_id=hospital.id,
                invoice_id=row.id,
                source_type="consultation",
                description=f"Line item {ln_idx} for INV-{i:03}",
                quantity=1.0,
                rate=float(i + 1) / 3.0,
                amount=float(i + 1) / 3.0,
                sort_order=ln_idx,
            )
            db_session.add(line)
        rows.append(row)
    db_session.commit()
    return rows


def test_default_invoice_page_is_bounded_and_summary_covers_all(billing_client, admin_headers, invoice_rows):
    response = billing_client.get("/api/billing/invoices", headers=admin_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["items"]) == 15
    assert data["page"] == 1 and data["page_size"] == 15
    assert data["total"] == 37 and data["total_pages"] == 3
    assert data["total_amount"] == pytest.approx(sum(range(1, 38)))
    assert data["items"][0]["invoice_number"] == "INV-036"
    assert "lines" not in data["items"][0]
    assert "notes" not in data["items"][0]
    assert len(response.content) < 15000


def test_invoice_pages_sorts_filters_and_empty(billing_client, admin_headers, invoice_rows, patient):
    def fetch(**params):
        r = billing_client.get("/api/billing/invoices", headers=admin_headers, params=params)
        assert r.status_code == 200, r.text
        return r.json()

    first, second, last = fetch(), fetch(page=2), fetch(page=3)
    assert len(last["items"]) == 7
    assert not ({inv["id"] for inv in first["items"]} & {inv["id"] for inv in second["items"]})
    assert fetch(page=999)["page"] == 3
    assert fetch(sort="oldest")["items"][0]["invoice_number"] == "INV-000"
    assert fetch(sort="amount_asc")["items"][0]["grand_total"] == 1.0
    assert fetch(sort="amount_desc")["items"][0]["grand_total"] == 37.0
    assert fetch(sort="date_asc")["items"][0]["invoice_date"] == "2026-10-01"

    # Status filter
    paid_res = fetch(status="paid", sort="amount_asc", page_size=5, patient_id=str(patient.id))
    # Even indices (0, 2, 4, ... 36) -> 19 items
    assert paid_res["total"] == 19
    assert paid_res["total_amount"] == pytest.approx(sum(range(1, 38, 2)))
    assert [p["grand_total"] for p in paid_res["items"]] == [1.0, 3.0, 5.0, 7.0, 9.0]

    # Search
    assert fetch(search="INV-036")["total"] == 1
    assert fetch(search=patient.uhid)["total"] == 37
    assert fetch(search=patient.name)["total"] == 37

    # Date range
    assert fetch(from_date="2026-10-10", to_date="2026-10-10")["total"] == 3

    # Empty search
    empty = fetch(search="nonexistent", page=2)
    assert empty["items"] == [] and empty["total"] == 0 and empty["page"] == 1

    # Validation errors: 422
    for params in (
        {"page": 0},
        {"page_size": 501},
        {"sort": "invalid"},
        {"from_date": "2026-10-12", "to_date": "2026-10-01"},
    ):
        assert billing_client.get("/api/billing/invoices", headers=admin_headers, params=params).status_code == 422


def test_invoice_list_has_constant_queries_and_no_line_item_load(billing_client, admin_headers, invoice_rows, db_session):
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        response = billing_client.get("/api/billing/invoices", headers=admin_headers)
        assert response.status_code == 200
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)

    billing_queries = [sql for sql in queries if "billing_invoices" in sql]
    assert len(billing_queries) <= 3, billing_queries
    assert any("LIMIT" in sql and "OFFSET" in sql for sql in billing_queries)
    assert all("billing_invoice_lines" not in sql for sql in billing_queries)


@pytest.fixture
def admin_headers_b(hospital_b):
    from shared.auth.jwt import create_access_token
    token = create_access_token({
        "sub": "admin@fortisreg.com",
        "name": "Admin B",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital_b.id),
    })
    return {"Authorization": f"Bearer {token}"}


def test_invoice_list_is_hospital_scoped(billing_client, admin_headers_b, invoice_rows, hospital_b, patient_b, db_session):
    db_session.add(
        BillingInvoice(
            hospital_id=hospital_b.id,
            patient_id=patient_b.id,
            invoice_number="INV-HOSP-B",
            invoice_date=date(2026, 10, 1),
            grand_total=999.0,
            status=BillingInvoiceStatus.generated,
        )
    )
    db_session.commit()
    response = billing_client.get("/api/billing/invoices", headers=admin_headers_b)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1 and data["total_amount"] == 999.0
    assert data["items"][0]["patient_id"] == str(patient_b.id)
    assert data["items"][0]["invoice_number"] == "INV-HOSP-B"
