"""
Unit and integration tests for the central + departmental non-drug inventory
domain (Features 44-48).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.inventory.api.inventory_api import router as inventory_router
from modules.tenancy.entities.hospital import Hospital
from shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def inventory_db():
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
    app.include_router(inventory_router, prefix="/api")

    hospital_id = hospital_id or uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id=f"HOSP{hospital_id.hex[:6].upper()}",
        name="Inventory Test Hospital",
        address="123 Health St",
        phone="9876543210",
        email=f"admin_{hospital_id.hex[:6]}@invhosp.org",
        password_hash="hash",
        is_active=True,
    )
    db.add(hospital)
    db.commit()

    current_user = {
        "id": str(uuid4()),
        "sub": "inv_admin@example.com",
        "email": "inv_admin@example.com",
        "role": "admin",
        "name": "Inventory Admin",
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
    app.dependency_overrides[get_hospital_context] = override_hospital

    return TestClient(app), hospital_id, current_user


@pytest.fixture
def inv_client(inventory_db):
    return _make_client(inventory_db)


def _create_item(client, code="ITM001", name="Surgical Gloves (Box of 100)"):
    res = client.post(
        "/api/inventory/items",
        json={
            "item_code": code,
            "item_name": name,
            "category": "surgical_disposable",
            "unit_of_issue": "box",
            "reorder_level": 10,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def _receive(client, item_id, quantity=100, batch_number="B001", location="Main Store"):
    res = client.post(
        "/api/inventory/central-stock/receive",
        json={
            "item_id": item_id,
            "batch_number": batch_number,
            "quantity": quantity,
            "location": location,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


# ── Feature 45: Consumable Item Master ──────────────────────────────────────


def test_create_consumable_item(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    assert item["item_code"] == "ITM001"
    assert item["category"] == "surgical_disposable"
    assert item["is_active"] is True


def test_duplicate_item_code_rejected(inv_client):
    client, _, _ = inv_client
    _create_item(client, code="DUP001")
    res = client.post(
        "/api/inventory/items",
        json={
            "item_code": "DUP001",
            "item_name": "Another Item",
            "category": "ward_supply",
            "unit_of_issue": "piece",
        },
    )
    assert res.status_code == 409


# ── Feature 44/46: Batch receipt increments central stock + ledger ─────────


def test_receive_stock_creates_batch_and_central_stock_and_ledger(inv_client):
    client, hospital_id, _ = inv_client
    item = _create_item(client)
    central = _receive(client, item["id"], quantity=100, batch_number="B001")
    assert central["total_quantity"] == 100
    assert central["location"] == "Main Store"

    batches = client.get(f"/api/inventory/items/{item['id']}/batches")
    assert batches.status_code == 200
    batch_list = batches.json()
    assert len(batch_list) == 1
    assert batch_list[0]["batch_number"] == "B001"
    assert batch_list[0]["remaining_quantity"] == 100


def test_receiving_same_batch_number_again_increments_existing_batch(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=50, batch_number="B001")
    central = _receive(client, item["id"], quantity=25, batch_number="B001")
    assert central["total_quantity"] == 75

    batches = client.get(f"/api/inventory/items/{item['id']}/batches").json()
    assert len(batches) == 1
    assert batches[0]["remaining_quantity"] == 75


# ── Feature 47: Departmental consumption ────────────────────────────────────


def test_consume_departmental_stock_decrements_and_blocks_overconsumption(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=100)

    # Move stock into a department via a completed transfer first.
    transfer = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "ICU", "quantity": 40},
    ).json()
    tid = transfer["id"]
    client.post(f"/api/inventory/transfers/{tid}/approve", json={})
    client.post(f"/api/inventory/transfers/{tid}/dispatch")
    complete_res = client.post(f"/api/inventory/transfers/{tid}/complete")
    assert complete_res.status_code == 200

    dept_stock = client.get("/api/inventory/departmental-stock", params={"department": "ICU"}).json()
    assert dept_stock[0]["quantity"] == 40

    consume_res = client.post(
        "/api/inventory/departmental-stock/consume",
        json={"item_id": item["id"], "department": "ICU", "quantity": 15},
    )
    assert consume_res.status_code == 200
    assert consume_res.json()["quantity"] == 25

    # Over-consumption blocked
    over_res = client.post(
        "/api/inventory/departmental-stock/consume",
        json={"item_id": item["id"], "department": "ICU", "quantity": 1000},
    )
    assert over_res.status_code == 400


def test_consume_with_no_stock_blocked(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    res = client.post(
        "/api/inventory/departmental-stock/consume",
        json={"item_id": item["id"], "department": "Ward-A", "quantity": 5},
    )
    assert res.status_code == 400


# ── Feature 48: Transfer lifecycle ──────────────────────────────────────────


def test_full_transfer_lifecycle_moves_stock_and_creates_two_ledger_entries_only_at_completion(inv_client):
    client, hospital_id, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=200)

    transfer = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "OT-1", "quantity": 60},
    ).json()
    tid = transfer["id"]
    assert transfer["status"] == "requested"

    # Approve
    approved = client.post(f"/api/inventory/transfers/{tid}/approve", json={}).json()
    assert approved["status"] == "approved"

    # Central stock must be unchanged at approved stage
    central_after_approve = client.get("/api/inventory/central-stock").json()
    assert central_after_approve[0]["total_quantity"] == 200
    assert central_after_approve[0]["in_transit_quantity"] == 0

    # Dispatch -> FLAW-019: source available decremented into in-transit holding
    dispatched = client.post(f"/api/inventory/transfers/{tid}/dispatch").json()
    assert dispatched["status"] == "dispatched"

    central_after_dispatch = client.get("/api/inventory/central-stock").json()
    assert central_after_dispatch[0]["total_quantity"] == 140
    assert central_after_dispatch[0]["in_transit_quantity"] == 60

    # Complete -> stock lands at destination, in-transit cleared
    completed = client.post(f"/api/inventory/transfers/{tid}/complete").json()
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None

    central_after_complete = client.get("/api/inventory/central-stock").json()
    assert central_after_complete[0]["total_quantity"] == 140
    assert central_after_complete[0]["in_transit_quantity"] == 0

    dept_stock = client.get("/api/inventory/departmental-stock", params={"department": "OT-1"}).json()
    assert dept_stock[0]["quantity"] == 60
    assert dept_stock[0]["in_transit_quantity"] == 0


def test_transfer_ledger_has_exactly_two_entries_only_after_completion(inv_client, inventory_db):
    client, hospital_id, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=200)

    transfer = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "Lab-1", "quantity": 30},
    ).json()
    tid = transfer["id"]

    from modules.inventory.entities.inventory_entities import InventoryStockTransaction

    # No transfer-scoped ledger rows exist yet (the earlier receipt txn has reference_type=None).
    rows_before = (
        inventory_db.query(InventoryStockTransaction)
        .filter(InventoryStockTransaction.reference_type == "inventory_transfer")
        .all()
    )
    assert len(rows_before) == 0

    client.post(f"/api/inventory/transfers/{tid}/approve", json={})
    client.post(f"/api/inventory/transfers/{tid}/dispatch")

    # FLAW-019: dispatch already writes the transfer_out ledger entry (in transit).
    rows_after_dispatch = (
        inventory_db.query(InventoryStockTransaction)
        .filter(InventoryStockTransaction.reference_type == "inventory_transfer")
        .all()
    )
    assert len(rows_after_dispatch) == 1
    assert rows_after_dispatch[0].transaction_type.value == "transfer_out"

    client.post(f"/api/inventory/transfers/{tid}/complete")

    rows_after = (
        inventory_db.query(InventoryStockTransaction)
        .filter(InventoryStockTransaction.reference_type == "inventory_transfer")
        .all()
    )
    assert len(rows_after) == 2
    types = sorted(r.transaction_type.value for r in rows_after)
    assert types == ["transfer_in", "transfer_out"]
    for r in rows_after:
        assert str(r.reference_id) == tid


def test_transfer_with_insufficient_stock_blocked(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=10)

    transfer = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "ER", "quantity": 500},
    ).json()
    tid = transfer["id"]
    client.post(f"/api/inventory/transfers/{tid}/approve", json={})
    # FLAW-019: insufficient stock is now rejected at DISPATCH (source must have
    # the stock to move it in transit), not at completion.
    res = client.post(f"/api/inventory/transfers/{tid}/dispatch")
    assert res.status_code == 400

    # Central stock unaffected by the blocked dispatch
    central = client.get("/api/inventory/central-stock").json()
    assert central[0]["total_quantity"] == 10
    assert central[0]["in_transit_quantity"] == 0


def test_rejected_transfer_does_not_move_stock(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=80)

    transfer = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "Radiology", "quantity": 20},
    ).json()
    tid = transfer["id"]

    rejected = client.post(f"/api/inventory/transfers/{tid}/reject", json={"rejection_reason": "not needed"}).json()
    assert rejected["status"] == "rejected"

    central = client.get("/api/inventory/central-stock").json()
    assert central[0]["total_quantity"] == 80

    dept_stock = client.get("/api/inventory/departmental-stock", params={"department": "Radiology"}).json()
    assert dept_stock == []


def test_cannot_skip_transfer_states(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=50)

    transfer = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "ICU", "quantity": 10},
    ).json()
    tid = transfer["id"]

    # Cannot dispatch before approve
    res = client.post(f"/api/inventory/transfers/{tid}/dispatch")
    assert res.status_code == 400

    # Cannot complete before dispatch
    res2 = client.post(f"/api/inventory/transfers/{tid}/complete")
    assert res2.status_code == 400


# ── FLAW-019: in-transit transfer holding ────────────────────────────────────


def test_dispatch_decrements_source_and_tracks_in_transit(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=100)

    transfer = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "ICU", "quantity": 40},
    ).json()
    tid = transfer["id"]
    client.post(f"/api/inventory/transfers/{tid}/approve", json={})
    client.post(f"/api/inventory/transfers/{tid}/dispatch")

    central = client.get("/api/inventory/central-stock").json()[0]
    # Available dropped by 40, in transit = 40
    assert central["total_quantity"] == 60
    assert central["in_transit_quantity"] == 40


def test_in_transit_stock_cannot_be_over_consumed_by_second_request(inv_client):
    """The FLAW-019 scenario: after dispatching 10, central available is 0, so a
    second department requesting the same 10 cannot dispatch (no phantom stock)."""
    client, _, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=10)

    # OT requests and dispatches all 10
    t1 = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "OT", "quantity": 10},
    ).json()
    client.post(f"/api/inventory/transfers/{t1['id']}/approve", json={})
    client.post(f"/api/inventory/transfers/{t1['id']}/dispatch")

    # ER tries to request the same 10 -> dispatch blocked, no phantom stock
    t2 = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "ER", "quantity": 10},
    ).json()
    client.post(f"/api/inventory/transfers/{t2['id']}/approve", json={})
    res = client.post(f"/api/inventory/transfers/{t2['id']}/dispatch")
    assert res.status_code == 400


def test_reject_after_dispatch_returns_in_transit_stock(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=80)

    transfer = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "Radiology", "quantity": 20},
    ).json()
    tid = transfer["id"]
    client.post(f"/api/inventory/transfers/{tid}/approve", json={})
    client.post(f"/api/inventory/transfers/{tid}/dispatch")

    central_before = client.get("/api/inventory/central-stock").json()[0]
    assert central_before["total_quantity"] == 60
    assert central_before["in_transit_quantity"] == 20

    # Reject AFTER dispatch -> stock returned to source
    rejected = client.post(
        f"/api/inventory/transfers/{tid}/reject", json={"rejection_reason": "dispatch cancelled"}
    ).json()
    assert rejected["status"] == "rejected"

    central_after = client.get("/api/inventory/central-stock").json()[0]
    assert central_after["total_quantity"] == 80
    assert central_after["in_transit_quantity"] == 0


def test_department_to_department_in_transit_holding(inv_client):
    client, _, _ = inv_client
    item = _create_item(client)
    _receive(client, item["id"], quantity=100)

    # Move 50 into dept "Ward-A"
    t1 = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Central", "to_department": "Ward-A", "quantity": 50},
    ).json()
    client.post(f"/api/inventory/transfers/{t1['id']}/approve", json={})
    client.post(f"/api/inventory/transfers/{t1['id']}/dispatch")
    client.post(f"/api/inventory/transfers/{t1['id']}/complete")

    # Dispatch 30 out of Ward-A to Ward-B
    t2 = client.post(
        "/api/inventory/transfers",
        json={"item_id": item["id"], "from_location": "Ward-A", "to_department": "Ward-B", "quantity": 30},
    ).json()
    client.post(f"/api/inventory/transfers/{t2['id']}/approve", json={})
    client.post(f"/api/inventory/transfers/{t2['id']}/dispatch")

    ward_a = client.get("/api/inventory/departmental-stock", params={"department": "Ward-A"}).json()[0]
    assert ward_a["quantity"] == 20
    assert ward_a["in_transit_quantity"] == 30

    client.post(f"/api/inventory/transfers/{t2['id']}/complete")
    ward_a = client.get("/api/inventory/departmental-stock", params={"department": "Ward-A"}).json()[0]
    ward_b = client.get("/api/inventory/departmental-stock", params={"department": "Ward-B"}).json()[0]
    assert ward_a["in_transit_quantity"] == 0
    assert ward_b["quantity"] == 30


# ── Tenant isolation ─────────────────────────────────────────────────────────


def test_tenant_isolation_on_consumable_items(inventory_db):
    client_a, hospital_a, _ = _make_client(inventory_db)
    item_a = _create_item(client_a, code="TENANT-A-001")

    client_b, hospital_b, _ = _make_client(inventory_db)
    # Same item_code is allowed in a different hospital (unique constraint is per-hospital)
    item_b = _create_item(client_b, code="TENANT-A-001")
    assert item_a["hospital_id"] != item_b["hospital_id"]

    # Hospital B cannot see/get hospital A's item
    res = client_b.get(f"/api/inventory/items/{item_a['id']}")
    assert res.status_code == 404

    # Hospital A's item list does not include hospital B's item
    list_a = client_a.get("/api/inventory/items").json()
    ids_a = {i["id"] for i in list_a}
    assert item_b["id"] not in ids_a
    assert item_a["id"] in ids_a


def test_ledger_lists_transactions_and_can_filter_by_item(inv_client):
    client, _, _ = inv_client
    item_a = _create_item(client, code="LEDGER-A")
    item_b = _create_item(client, code="LEDGER-B", name="Ward Gauze Rolls")
    _receive(client, item_a["id"], quantity=50, batch_number="LB-A1")
    _receive(client, item_b["id"], quantity=30, batch_number="LB-B1")

    all_txns = client.get("/api/inventory/transactions").json()
    assert len(all_txns) == 2
    assert {t["item_id"] for t in all_txns} == {item_a["id"], item_b["id"]}
    assert all(t["transaction_type"] == "receipt" for t in all_txns)

    filtered = client.get(f"/api/inventory/transactions?item_id={item_a['id']}").json()
    assert len(filtered) == 1
    assert filtered[0]["item_id"] == item_a["id"]


def test_ledger_unknown_item_filter_returns_404(inv_client):
    client, _, _ = inv_client
    res = client.get(f"/api/inventory/transactions?item_id={uuid4()}")
    assert res.status_code == 404
