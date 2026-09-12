"""
Tests for the Procurement Lifecycle domain (Features 49-55).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
# Imported so its FK target ("admissions") is registered on Base.metadata before
# create_all — masters_api transitively imports the Appointment entity, which
# has a FK to Admission, but does not import Admission itself.
from hms_migration.modules.inpatient.entities.admission import Admission  # noqa: F401
from hms_migration.modules.inventory.api.inventory_api import router as inventory_router
from hms_migration.modules.masters.api.masters_api import router as masters_router
from hms_migration.modules.procurement.api.procurement_api import router as procurement_router
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_admin, require_hospital_user


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def _make_client(db, hospital_id=None):
    app = FastAPI()
    app.include_router(procurement_router, prefix="/api")
    app.include_router(inventory_router, prefix="/api")
    app.include_router(masters_router, prefix="/api")

    hospital_id = hospital_id or uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id=f"HOSP{hospital_id.hex[:6].upper()}",
        name="Procurement Test Hospital",
        address="123 Health St",
        phone="9876543210",
        email=f"admin_{hospital_id.hex[:6]}@prochosp.org",
        password_hash="hash",
        is_active=True,
    )
    db.add(hospital)
    db.commit()

    current_user = {
        "id": str(uuid4()),
        "sub": "proc_admin@example.com",
        "email": "proc_admin@example.com",
        "role": "admin",
        "name": "Procurement Admin",
        "hospital_id": str(hospital_id),
    }

    def override_session():
        yield db

    def override_auth():
        return current_user

    def override_hospital():
        return hospital_id

    app.dependency_overrides[get_transitional_sync_session] = override_session
    app.dependency_overrides[require_hospital_user] = override_auth
    app.dependency_overrides[require_hospital_admin] = override_auth
    app.dependency_overrides[get_hospital_context] = override_hospital

    return TestClient(app), hospital_id, current_user


@pytest.fixture
def client(db):
    return _make_client(db)


def _create_item(client, code="ITM001", reorder_level=10):
    res = client.post(
        "/api/inventory/items",
        json={
            "item_code": code,
            "item_name": "Surgical Gloves",
            "category": "surgical_disposable",
            "unit_of_issue": "box",
            "reorder_level": reorder_level,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def _create_supplier(client, name="Acme Medical Supplies"):
    res = client.post(
        "/api/masters/suppliers",
        json={"name": name, "contact_person": "Jane Doe", "phone": "1234567890", "email": "jane@acme.com"},
    )
    assert res.status_code == 201, res.text
    return res.json()


def _create_po(client, supplier_id, item_id, qty=100, rate=5.0):
    res = client.post(
        "/api/procurement/purchase-orders",
        json={
            "supplier_id": supplier_id,
            "items": [{"consumable_item_id": item_id, "quantity_ordered": qty, "negotiated_rate": rate}],
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def _po_to_sent(client, po_id):
    client.post(f"/api/procurement/purchase-orders/{po_id}/submit")
    client.post(f"/api/procurement/purchase-orders/{po_id}/approve", json={})
    return client.post(f"/api/procurement/purchase-orders/{po_id}/send").json()


# ── Feature 49: Material Requisition ─────────────────────────────────────────


def test_create_and_approve_requisition(client):
    c, _, _ = client
    item = _create_item(c)
    res = c.post(
        "/api/procurement/requisitions",
        json={"department": "ICU", "consumable_item_id": item["id"], "quantity_requested": 20},
    )
    assert res.status_code == 201, res.text
    req = res.json()
    assert req["status"] == "pending"
    assert req["item_name"] == "Surgical Gloves"

    approved = c.post(f"/api/procurement/requisitions/{req['id']}/approve", json={"approval_notes": "ok"}).json()
    assert approved["status"] == "approved"

    fulfilled = c.post(f"/api/procurement/requisitions/{req['id']}/fulfill").json()
    assert fulfilled["status"] == "fulfilled"


def test_reject_requisition(client):
    c, _, _ = client
    item = _create_item(c)
    req = c.post(
        "/api/procurement/requisitions",
        json={"department": "ER", "consumable_item_id": item["id"], "quantity_requested": 5},
    ).json()
    rejected = c.post(f"/api/procurement/requisitions/{req['id']}/reject", json={"approval_notes": "not needed"}).json()
    assert rejected["status"] == "rejected"

    # Cannot approve an already-rejected requisition
    res = c.post(f"/api/procurement/requisitions/{req['id']}/approve", json={})
    assert res.status_code == 400


# ── Feature 50: Purchase Order ────────────────────────────────────────────────


def test_create_po_with_items(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    item = _create_item(c)
    po = _create_po(c, supplier["id"], item["id"], qty=100, rate=5.0)
    assert po["status"] == "draft"
    assert len(po["items"]) == 1
    assert po["items"][0]["quantity_ordered"] == 100


def test_po_lifecycle_transitions(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    item = _create_item(c)
    po = _create_po(c, supplier["id"], item["id"])

    submitted = c.post(f"/api/procurement/purchase-orders/{po['id']}/submit").json()
    assert submitted["status"] == "pending_approval"

    # cannot send before approval
    res = c.post(f"/api/procurement/purchase-orders/{po['id']}/send")
    assert res.status_code == 400

    approved = c.post(f"/api/procurement/purchase-orders/{po['id']}/approve", json={}).json()
    assert approved["status"] == "approved"

    sent = c.post(f"/api/procurement/purchase-orders/{po['id']}/send").json()
    assert sent["status"] == "sent"


# ── Feature 51: GRN ────────────────────────────────────────────────────────────


def test_grn_calls_inventory_and_updates_po_status_partial_then_complete(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    item = _create_item(c)
    po = _create_po(c, supplier["id"], item["id"], qty=100, rate=5.0)
    po = _po_to_sent(c, po["id"])
    po_item_id = po["items"][0]["id"]

    # Partial receipt of 60
    grn1 = c.post(
        "/api/procurement/grns",
        json={
            "purchase_order_id": po["id"],
            "items": [
                {
                    "purchase_order_item_id": po_item_id,
                    "accepted_quantity": 60,
                    "rejected_quantity": 0,
                    "batch_number": "B001",
                }
            ],
        },
    )
    assert grn1.status_code == 201, grn1.text

    po_after_partial = c.get(f"/api/procurement/purchase-orders/{po['id']}").json()
    assert po_after_partial["status"] == "partially_received"
    assert po_after_partial["items"][0]["quantity_received"] == 60

    # Inventory actually received the stock (calls InventoryActions.receive_stock)
    central = c.get("/api/inventory/central-stock").json()
    assert any(row["total_quantity"] == 60 for row in central)

    # Complete the remaining 40
    grn2 = c.post(
        "/api/procurement/grns",
        json={
            "purchase_order_id": po["id"],
            "items": [
                {
                    "purchase_order_item_id": po_item_id,
                    "accepted_quantity": 40,
                    "rejected_quantity": 0,
                    "batch_number": "B002",
                }
            ],
        },
    )
    assert grn2.status_code == 201, grn2.text

    po_after_complete = c.get(f"/api/procurement/purchase-orders/{po['id']}").json()
    assert po_after_complete["status"] == "completed"
    assert po_after_complete["items"][0]["quantity_received"] == 100

    central_after = c.get("/api/inventory/central-stock").json()
    assert any(row["total_quantity"] == 100 for row in central_after)


def test_grn_over_receipt_blocked(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    item = _create_item(c)
    po = _create_po(c, supplier["id"], item["id"], qty=50, rate=5.0)
    po = _po_to_sent(c, po["id"])
    po_item_id = po["items"][0]["id"]

    res = c.post(
        "/api/procurement/grns",
        json={
            "purchase_order_id": po["id"],
            "items": [
                {
                    "purchase_order_item_id": po_item_id,
                    "accepted_quantity": 999,
                    "rejected_quantity": 0,
                    "batch_number": "B001",
                }
            ],
        },
    )
    assert res.status_code == 400


def test_grn_blocked_on_draft_po(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    item = _create_item(c)
    po = _create_po(c, supplier["id"], item["id"])
    po_item_id = po["items"][0]["id"]

    res = c.post(
        "/api/procurement/grns",
        json={
            "purchase_order_id": po["id"],
            "items": [
                {"purchase_order_item_id": po_item_id, "accepted_quantity": 1, "rejected_quantity": 0, "batch_number": "B001"}
            ],
        },
    )
    assert res.status_code == 400


def test_grn_with_rejected_quantity(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    item = _create_item(c)
    po = _create_po(c, supplier["id"], item["id"], qty=100)
    po = _po_to_sent(c, po["id"])
    po_item_id = po["items"][0]["id"]

    grn = c.post(
        "/api/procurement/grns",
        json={
            "purchase_order_id": po["id"],
            "items": [
                {
                    "purchase_order_item_id": po_item_id,
                    "accepted_quantity": 80,
                    "rejected_quantity": 20,
                    "rejection_reason": "damaged in transit",
                    "batch_number": "B001",
                }
            ],
        },
    ).json()
    assert grn["items"][0]["rejected_quantity"] == 20

    # accepted+rejected == quantity_ordered so PO must be completed
    po_after = c.get(f"/api/procurement/purchase-orders/{po['id']}").json()
    assert po_after["status"] == "completed"


# ── Feature 54: Supplier Performance ─────────────────────────────────────────


def test_supplier_performance_zero_when_no_data(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    perf = c.get(f"/api/procurement/suppliers/{supplier['id']}/performance").json()
    assert perf["total_purchase_orders"] == 0
    assert perf["total_grns"] == 0
    assert perf["on_time_delivery_rate"] is None
    assert perf["rejection_rate"] is None
    assert perf["total_accepted_quantity"] == 0
    assert perf["total_rejected_quantity"] == 0


def test_supplier_performance_computed_from_real_data(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    item = _create_item(c)
    po = _create_po(c, supplier["id"], item["id"], qty=100)
    po = _po_to_sent(c, po["id"])
    po_item_id = po["items"][0]["id"]

    c.post(
        "/api/procurement/grns",
        json={
            "purchase_order_id": po["id"],
            "items": [
                {
                    "purchase_order_item_id": po_item_id,
                    "accepted_quantity": 90,
                    "rejected_quantity": 10,
                    "batch_number": "B001",
                }
            ],
        },
    )

    perf = c.get(f"/api/procurement/suppliers/{supplier['id']}/performance").json()
    assert perf["total_purchase_orders"] == 1
    assert perf["total_grns"] == 1
    assert perf["total_accepted_quantity"] == 90
    assert perf["total_rejected_quantity"] == 10
    assert perf["rejection_rate"] == pytest.approx(0.1)


# ── Feature 55: Analytics ──────────────────────────────────────────────────────


def test_analytics_returns_real_aggregates(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    item = _create_item(c, reorder_level=1000)  # force a reorder alert once received stock is low
    po = _create_po(c, supplier["id"], item["id"], qty=100, rate=5.0)
    po = _po_to_sent(c, po["id"])
    po_item_id = po["items"][0]["id"]

    c.post(
        "/api/procurement/grns",
        json={
            "purchase_order_id": po["id"],
            "items": [
                {"purchase_order_item_id": po_item_id, "accepted_quantity": 100, "rejected_quantity": 0, "batch_number": "B001"}
            ],
        },
    )

    analytics = c.get("/api/procurement/analytics").json()
    assert len(analytics["spend_by_supplier"]) == 1
    assert analytics["spend_by_supplier"][0]["total_spend"] == 500.0
    assert analytics["pending_orders_count"] == 0  # PO is completed
    # Reorder alert fires because reorder_level(1000) > current_quantity(100)
    assert any(a["item_id"] == item["id"] for a in analytics["reorder_alerts"])


def test_analytics_pending_orders_count(client):
    c, _, _ = client
    supplier = _create_supplier(c)
    item = _create_item(c)
    _create_po(c, supplier["id"], item["id"])  # left in draft

    analytics = c.get("/api/procurement/analytics").json()
    assert analytics["pending_orders_count"] == 1


# ── Tenant isolation ─────────────────────────────────────────────────────────


def test_tenant_isolation_on_purchase_orders(db):
    client_a, hospital_a, _ = _make_client(db)
    supplier_a = _create_supplier(client_a)
    item_a = _create_item(client_a, code="TEN-A")
    po_a = _create_po(client_a, supplier_a["id"], item_a["id"])

    client_b, hospital_b, _ = _make_client(db)
    res = client_b.get(f"/api/procurement/purchase-orders/{po_a['id']}")
    assert res.status_code == 404

    list_b = client_b.get("/api/procurement/purchase-orders").json()
    assert all(po["id"] != po_a["id"] for po in list_b)
