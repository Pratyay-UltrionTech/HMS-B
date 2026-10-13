"""Statement presentation checks without database connections."""
from datetime import datetime, date
from uuid import uuid4

import pytest
from modules.billing.actions.billing_actions import statement_html
from modules.billing.contracts.billing_contracts import PatientBillingStatementResponse


def print_fixture(count=1, outstanding=0, optional=True):
    return PatientBillingStatementResponse.model_validate({
        "statement_id": "STMT-20261012-001", "generated_at": datetime(2026, 10, 12, 10, 30),
        "hospital": {"hospital_id": uuid4(), "hospital_code": "TEST", "name": "Ultrion Community Hospital", "address": "21 Hospital Road, Bengaluru", "phone": "080 1234 5678"},
        "patient": {"patient_id": uuid4(), "name": "Example Patient", "uhid": "UHID-000123", "age": 42 if optional else None, "gender": "Female" if optional else None},
        "scope": {"scope": "entire_journey"},
        "charges": [{"id": uuid4(), "charge_date": datetime(2026, 10, 1), "source_type": "laboratory", "description": f"Service {i + 1:03d} — Comprehensive laboratory testing and specialist review with a long description / " + "extended-reference-" * 3,
                     "quantity": 1, "unit_price": 12500, "gross_amount": 12500, "discount_amount": 500, "tax_amount": 250, "net_amount": 12250, "status": "paid", "reference_number": "REF-" + "1234567890" * 3} for i in range(count)],
        "payments": [{"id": uuid4(), "payment_date": date(2026, 10, 2), "amount": 12000, "payment_method": "upi", "receipt_number": "REC-001", "reference_number": "<unsafe>&reference", "received_by_name": "Example Cashier"}],
        "receipts": [], "invoices": [{"id": uuid4(), "hospital_id": uuid4(), "patient_id": uuid4(), "invoice_number": "INV-001", "invoice_date": date(2026,10,1), "subtotal": 12500, "discount_amount": 500, "tax_amount": 250, "grand_total": 12250, "status": "paid", "created_at": datetime(2026,10,1), "lines": [{"id": uuid4(), "source_type": "laboratory", "description": "Invoice snapshot line", "category_label": "Diagnostic services", "quantity": 1, "rate": 12500, "amount": 12250}]}],
        "deposits": [{"id": uuid4(), "deposit_number": "DEP-001", "deposit_date": date(2026,10,1), "deposit_type": "advance", "payment_method": "cash", "original_amount": 5000, "used_amount": 3000, "available_amount": 2000, "status": "active"}],
        "refunds": [{"id": uuid4(), "refund_number": "RF-001", "refund_date": date(2026,10,3), "refund_method": "cash", "amount": 250, "reason": "Excess payment returned", "status": "completed"}],
        "summary": {"gross_charges": 12500 * count, "discount_total": 500 * count, "tax_total": 250 * count, "net_charges": 12250 * count, "total_paid": 12000, "deposits_received": 5000, "deposits_applied": 3000, "deposits_available": 2000, "total_refunds": 250, "outstanding": outstanding, "net_patient_balance": outstanding - 2000},
    })


@pytest.mark.parametrize("count,outstanding,optional", [(1, 0, False), (7, 1250, True), (90, 125000, True)])
def test_statement_print_preserves_records_and_financial_values(count, outstanding, optional):
    statement = print_fixture(count, outstanding, optional)
    before = statement.model_dump()
    html = statement_html(statement, auto_print=False)
    assert statement.model_dump() == before
    assert "size: A4 portrait" in html
    assert "margin: 12mm 14mm 14mm 14mm" in html
    assert "display: table-header-group" in html
    assert "page-break-inside: avoid" in html
    assert "table-layout: fixed" in html
    assert html.count("Status: paid") == count
    assert f"Service {count:03d}" in html
    assert "Advance Deposits" in html and "Refunds Processed" in html
    assert "Invoice INV-001 — Line details" in html
    assert "Invoice snapshot line" in html and "Diagnostic services" in html
    assert "BillingInvoiceStatus" not in html
    assert "Canonical Accounting" not in html and "Net Canonical Charges" not in html
    assert f"₹{outstanding:,.2f}" in html
    assert "&lt;unsafe&gt;&amp;reference" in html
    assert "window.onload" not in html
