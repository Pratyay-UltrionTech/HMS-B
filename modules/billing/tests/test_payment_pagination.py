from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from modules.billing.tests.test_billing import billing_app, billing_client
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b, admin_headers, auth_headers_b
from modules.billing.entities.billing_entities import BillingPayment, BillingPaymentMethod, BillingPaymentAllocation, BillingCharge, BillingSourceType


@pytest.fixture
def payment_rows(db_session, hospital, patient):
    rows = []
    for i in range(37):
        row = BillingPayment(hospital_id=hospital.id, patient_id=patient.id,
            amount=i + 1, payment_date=date(2026, 10, 1) + timedelta(days=i % 10),
            payment_method=BillingPaymentMethod.cash if i % 2 else BillingPaymentMethod.upi,
            reference_number=f'TXN-{i:03}', received_by_name='Alice' if i % 2 else 'Bob',
            notes='large notes ' * 500, created_at=datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=i))
        db_session.add(row)
        rows.append(row)
    db_session.commit()
    return rows


def test_default_page_is_bounded_and_summary_covers_all(billing_client, admin_headers, payment_rows):
    response = billing_client.get('/api/billing/payments', headers=admin_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data['items']) == 15
    assert data['page'] == 1 and data['page_size'] == 15
    assert data['total'] == 37 and data['total_pages'] == 3
    assert data['total_collected'] == sum(range(1, 38))
    assert data['items'][0]['reference_number'] == 'TXN-036'
    assert 'allocations' not in data['items'][0] and 'notes' not in data['items'][0]
    assert len(response.content) < 15000


def test_pages_sorts_filters_and_empty(billing_client, admin_headers, payment_rows, patient):
    def fetch(**params):
        r = billing_client.get('/api/billing/payments', headers=admin_headers, params=params)
        assert r.status_code == 200, r.text
        return r.json()
    first, second, last = fetch(), fetch(page=2), fetch(page=3)
    assert len(last['items']) == 7
    assert not ({p['id'] for p in first['items']} & {p['id'] for p in second['items']})
    assert fetch(page=999)['page'] == 3
    assert fetch(sort='oldest')['items'][0]['reference_number'] == 'TXN-000'
    assert fetch(sort='amount_asc')['items'][0]['amount'] == 1
    assert fetch(sort='amount_desc')['items'][0]['amount'] == 37
    filtered = fetch(payment_method='cash', collected_by='Alice', sort='amount_asc', page_size=5, patient_id=str(patient.id))
    assert filtered['total'] == 18 and filtered['total_collected'] == sum(range(2, 38, 2))
    assert [p['amount'] for p in filtered['items']] == [2, 4, 6, 8, 10]
    assert fetch(search='TXN-036')['total'] == 1
    assert fetch(search=patient.uhid)['total'] == 37
    assert fetch(from_date='2026-10-10', to_date='2026-10-10')['total'] == 3
    empty = fetch(search='nonexistent', page=2)
    assert empty['items'] == [] and empty['total'] == 0 and empty['page'] == 1
    for params in ({'page': 0}, {'page_size': 501}, {'sort': 'invalid'}, {'from_date': '2026-10-12', 'to_date': '2026-10-01'}):
        assert billing_client.get('/api/billing/payments', headers=admin_headers, params=params).status_code == 422


def test_payment_list_has_constant_queries_and_no_allocation_load(billing_client, admin_headers, payment_rows, db_session):
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            queries.append(statement)
    event.listen(db_session.bind, 'before_cursor_execute', capture)
    try:
        response = billing_client.get('/api/billing/payments', headers=admin_headers)
        assert response.status_code == 200
    finally:
        event.remove(db_session.bind, 'before_cursor_execute', capture)
    billing_queries = [sql for sql in queries if 'billing_payments' in sql or 'billing_receipts' in sql]
    assert len(billing_queries) <= 3, billing_queries
    assert any('LIMIT' in sql and 'OFFSET' in sql for sql in billing_queries)
    assert all('billing_payment_allocations' not in sql for sql in billing_queries)
    assert all('patients.phone' not in sql for sql in billing_queries)


def test_payment_list_is_hospital_scoped(billing_client, auth_headers_b, payment_rows, hospital_b, patient_b, db_session):
    db_session.add(BillingPayment(hospital_id=hospital_b.id, patient_id=patient_b.id, amount=123,
        payment_date=date(2026, 10, 1), payment_method=BillingPaymentMethod.cash))
    db_session.commit()
    response = billing_client.get('/api/billing/payments', headers=auth_headers_b)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['total'] == 1 and data['total_collected'] == 123
    assert data['items'][0]['patient_id'] == str(patient_b.id)


def test_receipt_and_invoice_search_do_not_duplicate_payments(billing_client, admin_headers, payment_rows, db_session, hospital, patient):
    from modules.billing.entities.billing_entities import BillingReceipt, BillingReceiptStatus, BillingInvoice
    invoice = BillingInvoice(hospital_id=hospital.id, patient_id=patient.id,
        invoice_number='INV-PAGE-1', invoice_date=date(2026, 10, 1))
    db_session.add(invoice)
    db_session.flush()
    for number, status in [('RCPT-PAGE-1', BillingReceiptStatus.issued), ('RCPT-PAGE-2', BillingReceiptStatus.issued), ('RCPT-CANCELLED', BillingReceiptStatus.cancelled)]:
        db_session.add(BillingReceipt(hospital_id=hospital.id, patient_id=patient.id,
            payment_id=payment_rows[0].id, linked_invoice_id=invoice.id,
            receipt_number=number, payment_date=date(2026, 10, 1), payment_method=BillingPaymentMethod.upi,
            amount=1, status=status))
    db_session.commit()
    for params in ({'search': 'RCPT-PAGE'}, {'search': 'INV-PAGE-1'}, {'invoice_number': 'PAGE-1'}, {'search': 'RCPT-PAGE', 'payment_method': 'upi', 'sort': 'amount_desc'}):
        response = billing_client.get('/api/billing/payments', headers=admin_headers, params=params)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['total'] == 1 and data['total_collected'] == 1 and len(data['items']) == 1
        assert data['items'][0]['receipt_number'] in ('RCPT-PAGE-1', 'RCPT-PAGE-2')
    assert billing_client.get('/api/billing/payments', headers=admin_headers, params={'search': 'RCPT-CANCELLED'}).json()['total'] == 0
    assert billing_client.get('/api/billing/payments', headers=admin_headers, params={'search': '%'}).json()['total'] == 0


def test_create_update_and_last_row_removal_refresh(billing_client, admin_headers, payment_rows, db_session, patient):
    created = billing_client.post('/api/billing/payments', headers=admin_headers,
        json={'patient_id': str(patient.id), 'amount': 99, 'payment_method': 'cash', 'reference_number': 'NEW-PAYMENT'})
    assert created.status_code == 201, created.text
    refreshed = billing_client.get('/api/billing/payments', headers=admin_headers).json()
    assert refreshed['total'] == 38
    assert refreshed['items'][0]['id'] == created.json()['id']
    assert refreshed['items'][0]['receipt_number'] == created.json()['receipt_number']
    payment_rows[-1].reference_number = 'UPDATED-PAYMENT'
    db_session.commit()
    assert billing_client.get('/api/billing/payments', headers=admin_headers, params={'search': 'UPDATED-PAYMENT'}).json()['total'] == 1
    # Remove the last page in the isolated fixture, emulating external changes.
    for row in payment_rows[:8]:
        db_session.delete(row)
    db_session.commit()
    response = billing_client.get('/api/billing/payments', headers=admin_headers, params={'page': 3}).json()
    assert response['total'] == 30 and response['page'] == 2 and len(response['items']) == 15


def test_payment_register_requires_auth(billing_client):
    assert billing_client.get('/api/billing/payments').status_code in (401, 403)


def test_payment_page_avoids_nested_orm_work_at_scale(db_session, hospital, patient):
    import json
    from time import perf_counter
    from modules.billing.actions.billing_actions import ListPaymentsAction, _payment_response_dtos
    from sqlalchemy.orm import joinedload
    hospital_id, patient_id = hospital.id, patient.id
    for i in range(250):
        payment = BillingPayment(hospital_id=hospital_id, patient_id=patient_id, amount=100,
            payment_date=date(2026, 10, 1), payment_method=BillingPaymentMethod.cash,
            reference_number=f'PERF-{i:03}', notes='Routine payment collection',
            created_at=datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=i))
        charge = BillingCharge(hospital_id=hospital_id, patient_id=patient_id,
            source_type=BillingSourceType.other, description='Routine hospital service', charge_amount=100, net_amount=100)
        db_session.add_all([payment, charge])
        db_session.flush()
        db_session.add(BillingPaymentAllocation(hospital_id=hospital_id, payment_id=payment.id,
            charge_id=charge.id, allocated_amount=100))
    db_session.commit()
    db_session.expunge_all()
    queries = []
    def capture(conn, cursor, sql, parameters, context, executemany):
        if sql.lstrip().upper().startswith('SELECT'):
            queries.append(sql)
    event.listen(db_session.bind, 'before_cursor_execute', capture)
    try:
        start = perf_counter()
        # Reproduce the previous 200-row query and serializer for comparison.
        old_rows = db_session.query(BillingPayment).options(joinedload(BillingPayment.patient)).filter(
            BillingPayment.hospital_id == hospital_id
        ).order_by(BillingPayment.created_at.desc()).limit(200).offset(0).all()
        old = _payment_response_dtos(db_session, old_rows)
        old_ms, old_queries = (perf_counter() - start) * 1000, len(queries)
        old_bytes = len(json.dumps([p.model_dump(mode='json') for p in old], separators=(',', ':')).encode())
        db_session.expunge_all()
        queries.clear()
        start = perf_counter()
        new = ListPaymentsAction(db_session, hospital_id).execute()
        new_ms, new_queries = (perf_counter() - start) * 1000, len(queries)
    finally:
        event.remove(db_session.bind, 'before_cursor_execute', capture)
    new_bytes = len(new.model_dump_json().encode())
    assert len(old) == 200 and len(new.items) == 15
    assert new_queries == 3 and old_queries >= 202
    assert new_bytes < old_bytes / 5
    assert all('billing_payment_allocations' not in sql and 'billing_charges' not in sql for sql in queries)
    print(json.dumps({'fixture_payments': 250, 'old_rows': len(old), 'new_rows': len(new.items),
        'old_queries': old_queries, 'new_queries': new_queries, 'old_bytes': old_bytes,
        'new_bytes': new_bytes, 'old_ms': round(old_ms, 2), 'new_ms': round(new_ms, 2)}))
