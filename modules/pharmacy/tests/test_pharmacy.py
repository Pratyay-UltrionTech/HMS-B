"""
Unit and integration tests for Pharmacy domain.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.conftest
from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
from modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from modules.appointments.entities.appointment_type import AppointmentType
from modules.clinical_records.entities.clinical_record import Prescription
from modules.doctors.entities.doctor import HospitalUser, StaffRole
from modules.inpatient.entities.admission import Admission
from modules.patients.entities.patient import Patient
from modules.pharmacy.actions.pharmacy_actions import PharmacyActions
from modules.pharmacy.api.pharmacy_api import router as pharmacy_router
from modules.pharmacy.contracts.pharmacy_contracts import (
    PurchaseCreate,
    PurchaseItemCreate,
)
from modules.pharmacy.entities.pharmacy_entities import (
    Medicine,
    MedicineBatch,
    MedicineCategory,
    MedicineInventory,
    MedicineType,
    MedicineUnit,
    PharmacyPaymentStatus,
    PharmacyPurchase,
    PharmacyPurchaseItem,
    PharmacyReturn,
    PharmacySale,
    PharmacySaleItem,
    PharmacySaleStatus,
    PharmacySaleType,
    PharmacySupplier,
    PurchasePaymentStatus,
    StockAdjustment,
    StockAdjustmentType,
    StockTransaction,
    StockTransactionType,
)
from modules.tenancy.entities.hospital import Hospital
from shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def pharm_db():
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


@pytest.fixture
def pharm_client(pharm_db):
    app = FastAPI()
    app.include_router(pharmacy_router, prefix="/api")

    hospital_id = uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id=f"HOSP{hospital_id.hex[:6].upper()}",
        name="Pharmacy Test Hospital",
        address="789 Pharma Road",
        phone="9112233445",
        email=f"admin_{hospital_id.hex[:6]}@pharmhosp.org",
        password_hash="hash",
        is_active=True,
    )
    pharm_db.add(hospital)
    pharm_db.commit()

    current_user = {
        "id": str(uuid4()),
        "user_id": str(uuid4()),
        "sub": "pharm_admin@example.com",
        "email": "pharm_admin@example.com",
        "role": "hospital_admin",
        "name": "Pharmacist Admin",
        "hospital_uuid": str(hospital_id),
        "hospital_id": str(hospital_id),
    }

    def override_session():
        yield pharm_db

    def override_auth():
        return current_user

    def override_hospital():
        return hospital_id

    app.dependency_overrides[get_transitional_sync_session] = override_session
    app.dependency_overrides[require_hospital_user] = override_auth
    app.dependency_overrides[get_hospital_context] = override_hospital

    return TestClient(app), hospital_id, current_user


def test_pharmacy_categories_and_suppliers(pharm_client):
    client, hospital_id, _ = pharm_client

    # 1. Category CRUD
    c_res = client.post(
        "/api/pharmacy/categories",
        json={"name": "Antibiotics", "description": "Broad-spectrum antibiotics", "is_active": True},
    )
    assert c_res.status_code == 201
    cat = c_res.json()
    assert cat["name"] == "Antibiotics"
    cat_id = cat["id"]

    # Duplicate category conflict
    dup_c = client.post("/api/pharmacy/categories", json={"name": "Antibiotics"})
    assert dup_c.status_code == 409

    # Update category
    up_c = client.patch(f"/api/pharmacy/categories/{cat_id}", json={"description": "Updated description"})
    assert up_c.status_code == 200
    assert up_c.json()["description"] == "Updated description"

    # List categories
    cats = client.get("/api/pharmacy/categories").json()
    assert len(cats) >= 1

    # 2. Supplier CRUD
    s_res = client.post(
        "/api/pharmacy/suppliers",
        json={
            "company_name": "Apollo Pharma Distributors",
            "gstin": "07AAAAA0000A1Z5",
            "phone": "9876543210",
            "email": "sales@apollo.com",
            "contact_person": "Rajesh Kumar",
        },
    )
    assert s_res.status_code == 201
    supp = s_res.json()
    assert supp["company_name"] == "Apollo Pharma Distributors"
    supp_id = supp["id"]

    # Duplicate supplier conflict
    dup_s = client.post("/api/pharmacy/suppliers", json={"company_name": "Apollo Pharma Distributors"})
    assert dup_s.status_code == 409

    # Update supplier
    up_s = client.patch(f"/api/pharmacy/suppliers/{supp_id}", json={"city": "New Delhi"})
    assert up_s.status_code == 200
    assert up_s.json()["city"] == "New Delhi"

    # List suppliers
    supps = client.get("/api/pharmacy/suppliers").json()
    assert len(supps) >= 1


def test_pharmacy_medicines_crud_and_batches(pharm_client):
    client, hospital_id, _ = pharm_client

    # Create category first
    cat = client.post("/api/pharmacy/categories", json={"name": "Analgesics"}).json()

    # 1. Create medicine
    payload = {
        "medicine_name": "Paracetamol 500mg",
        "generic_name": "Paracetamol",
        "brand_name": "Crocin",
        "manufacturer": "GSK",
        "category_id": cat["id"],
        "medicine_type": "tablet",
        "strength": "500mg",
        "unit": "tablet",
        "selling_price": 2.50,
        "purchase_price": 1.50,
        "mrp": 3.00,
        "minimum_stock": 50.0,
        "maximum_stock": 1000.0,
        "reorder_level": 100.0,
        "requires_prescription": False,
    }
    m_res = client.post("/api/pharmacy/medicines", json=payload)
    assert m_res.status_code == 201
    med = m_res.json()
    assert med["medicine_name"] == "Paracetamol 500mg"
    assert med["brand_name"] == "Crocin"
    assert med["category_name"] == "Analgesics"
    med_id = med["id"]

    # 2. Duplicate medicine conflict
    dup_m = client.post("/api/pharmacy/medicines", json=payload)
    assert dup_m.status_code == 409

    # 3. Get medicine
    get_m = client.get(f"/api/pharmacy/medicines/{med_id}")
    assert get_m.status_code == 200
    assert get_m.json()["id"] == med_id

    # 4. Update medicine
    up_m = client.patch(f"/api/pharmacy/medicines/{med_id}", json={"selling_price": 2.80})
    assert up_m.status_code == 200
    assert up_m.json()["selling_price"] == 2.80

    # 5. List medicines
    list_m = client.get("/api/pharmacy/medicines", params={"search": "Paracetamol"}).json()
    assert len(list_m) == 1

    # 6. List batches for medicine (should be empty initially)
    batches = client.get(f"/api/pharmacy/medicines/{med_id}/batches").json()
    assert len(batches) == 0


def test_pharmacy_purchases_and_inventory_stock(pharm_client):
    client, hospital_id, _ = pharm_client

    cat = client.post("/api/pharmacy/categories", json={"name": "Antibiotics"}).json()
    supp = client.post("/api/pharmacy/suppliers", json={"company_name": "Cipla Direct"}).json()

    med = client.post(
        "/api/pharmacy/medicines",
        json={
            "medicine_name": "Amoxicillin 500mg",
            "brand_name": "Novamox",
            "manufacturer": "Cipla",
            "category_id": cat["id"],
            "strength": "500mg",
            "selling_price": 10.0,
            "purchase_price": 6.0,
            "mrp": 12.0,
        },
    ).json()

    # Create purchase with 2 batches
    today = date.today()
    purchase_payload = {
        "supplier_id": supp["id"],
        "invoice_number": "INV-CIPLA-001",
        "invoice_date": str(today),
        "received_date": str(today),
        "payment_status": "paid",
        "notes": "Bulk monthly stock",
        "items": [
            {
                "medicine_id": med["id"],
                "batch_number": "BAT-AMX-01",
                "expiry_date": str(today + timedelta(days=365)),
                "purchase_price": 6.0,
                "mrp": 12.0,
                "selling_price": 10.0,
                "quantity": 100.0,
                "free_quantity": 10.0,
                "gst_percent": 12.0,
                "discount_percent": 5.0,
            },
            {
                "medicine_id": med["id"],
                "batch_number": "BAT-AMX-02",
                "expiry_date": str(today + timedelta(days=180)),
                "purchase_price": 6.0,
                "mrp": 12.0,
                "selling_price": 10.0,
                "quantity": 50.0,
                "free_quantity": 0.0,
                "gst_percent": 12.0,
                "discount_percent": 0.0,
            },
        ],
    }
    p_res = client.post("/api/pharmacy/purchases", json=purchase_payload)
    assert p_res.status_code == 201
    purchase = p_res.json()
    assert purchase["invoice_number"] == "INV-CIPLA-001"
    assert len(purchase["items"]) == 2

    # Verify inventory was updated
    inv = client.get("/api/pharmacy/inventory", params={"search": "Amoxicillin"}).json()
    assert len(inv) == 1
    assert inv[0]["current_stock"] == 160.0  # (100+10) + 50
    assert inv[0]["available_stock"] == 160.0
    assert inv[0]["batch_count"] == 2
    assert inv[0]["status"] == "ok"

    # List batches for medicine
    batches = client.get(f"/api/pharmacy/medicines/{med['id']}/batches").json()
    assert len(batches) == 2


def test_pharmacy_sales_fifo_and_cancel(pharm_client, pharm_db):
    client, hospital_id, _ = pharm_client

    cat = client.post("/api/pharmacy/categories", json={"name": "Vitamins"}).json()
    supp = client.post("/api/pharmacy/suppliers", json={"company_name": "Abbott India"}).json()

    med = client.post(
        "/api/pharmacy/medicines",
        json={
            "medicine_name": "Vitamin C 500mg",
            "brand_name": "Limcee",
            "manufacturer": "Abbott",
            "category_id": cat["id"],
            "strength": "500mg",
            "selling_price": 5.0,
            "purchase_price": 2.5,
            "mrp": 6.0,
            "gst_percent": 12.0,
        },
    ).json()

    today = date.today()
    # Batch 1 expires in 60 days (earlier expiry)
    # Batch 2 expires in 300 days
    client.post(
        "/api/pharmacy/purchases",
        json={
            "supplier_id": supp["id"],
            "invoice_number": "PUR-LIM-01",
            "invoice_date": str(today),
            "received_date": str(today),
            "items": [
                {
                    "medicine_id": med["id"],
                    "batch_number": "LIM-EARLY",
                    "expiry_date": str(today + timedelta(days=60)),
                    "purchase_price": 2.5,
                    "mrp": 6.0,
                    "selling_price": 5.0,
                    "quantity": 20.0,
                },
                {
                    "medicine_id": med["id"],
                    "batch_number": "LIM-LATER",
                    "expiry_date": str(today + timedelta(days=300)),
                    "purchase_price": 2.5,
                    "mrp": 6.0,
                    "selling_price": 5.0,
                    "quantity": 50.0,
                },
            ],
        },
    )

    # 1. Counter sale requesting 25 units (FIFO should take 20 from LIM-EARLY, 5 from LIM-LATER)
    sale_payload = {
        "customer_name": "Rahul Sharma",
        "customer_phone": "9876543210",
        "sale_type": "walk_in",
        "payment_method": "cash",
        # Walk-in counter sales must tender cash explicitly (no implied payment).
        "amount_paid": 1000.0,
        "items": [
            {
                "medicine_id": med["id"],
                "quantity": 25.0,
            }
        ],
    }
    s_res = client.post("/api/pharmacy/sales", json=sale_payload)
    assert s_res.status_code == 201
    sale = s_res.json()
    assert sale["status"] == "completed"
    assert len(sale["items"]) == 2  # split across 2 batches
    sale_id = sale["id"]

    batch_map = {it["batch_number"]: it["quantity"] for it in sale["items"]}
    assert batch_map["LIM-EARLY"] == 20.0
    assert batch_map["LIM-LATER"] == 5.0

    # Stock should now be 70 - 25 = 45
    inv = client.get("/api/pharmacy/inventory", params={"search": "Vitamin C"}).json()
    assert inv[0]["available_stock"] == 45.0

    # 2. Cancel sale
    cancel_res = client.post(f"/api/pharmacy/sales/{sale_id}/cancel")
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"

    # Stock should be restored to 70
    inv_after = client.get("/api/pharmacy/inventory", params={"search": "Vitamin C"}).json()
    assert inv_after[0]["available_stock"] == 70.0


def test_pharmacy_returns_and_adjustments(pharm_client):
    client, hospital_id, _ = pharm_client

    cat = client.post("/api/pharmacy/categories", json={"name": "Cardio"}).json()
    supp = client.post("/api/pharmacy/suppliers", json={"company_name": "Torrent"}).json()

    med = client.post(
        "/api/pharmacy/medicines",
        json={
            "medicine_name": "Amlodipine 5mg",
            "brand_name": "Amlong",
            "manufacturer": "Micro Labs",
            "category_id": cat["id"],
            "strength": "5mg",
            "selling_price": 4.0,
            "purchase_price": 2.0,
            "mrp": 5.0,
        },
    ).json()

    today = date.today()
    # Purchase
    p_res = client.post(
        "/api/pharmacy/purchases",
        json={
            "supplier_id": supp["id"],
            "invoice_number": "PUR-AML-01",
            "invoice_date": str(today),
            "received_date": str(today),
            "items": [
                {
                    "medicine_id": med["id"],
                    "batch_number": "AML-001",
                    "expiry_date": str(today + timedelta(days=200)),
                    "purchase_price": 2.0,
                    "mrp": 5.0,
                    "selling_price": 4.0,
                    "quantity": 100.0,
                }
            ],
        },
    ).json()
    batch_id = p_res["items"][0]["batch_id"]
    purchase_item_id = p_res["items"][0]["id"]

    # 1. Customer sale
    sale = client.post(
        "/api/pharmacy/sales",
        json={
            "customer_name": "Sunita Verma",
            "customer_phone": "9811223344",
            "amount_paid": 1000.0,
            "items": [{"medicine_id": med["id"], "batch_id": batch_id, "quantity": 10.0}],
        },
    ).json()
    sale_item_id = sale["items"][0]["id"]

    # 2. Customer return (return 4 units)
    ret_res = client.post(
        "/api/pharmacy/returns/customer",
        json={
            "sale_id": sale["id"],
            "sale_item_id": sale_item_id,
            "quantity": 4.0,
            "reason": "Doctor changed prescription",
        },
    )
    assert ret_res.status_code == 201
    ret = ret_res.json()
    assert ret["return_type"] == "customer"
    assert ret["quantity"] == 4.0
    assert ret["refund_amount"] > 0

    # Verify stock restored: 100 - 10 + 4 = 94
    inv = client.get("/api/pharmacy/inventory", params={"search": "Amlodipine"}).json()
    assert inv[0]["available_stock"] == 94.0

    # 3. Purchase return (return 10 units to supplier)
    p_ret = client.post(
        "/api/pharmacy/returns/purchase",
        json={
            "purchase_id": p_res["id"],
            "purchase_item_id": purchase_item_id,
            "quantity": 10.0,
            "reason": "Damaged packaging on arrival",
        },
    )
    assert p_ret.status_code == 201
    assert p_ret.json()["return_type"] == "purchase"

    # Stock now 94 - 10 = 84
    inv = client.get("/api/pharmacy/inventory", params={"search": "Amlodipine"}).json()
    assert inv[0]["available_stock"] == 84.0

    # 4. Stock adjustment (damage 2 units)
    adj_res = client.post(
        "/api/pharmacy/adjustments",
        json={
            "medicine_id": med["id"],
            "batch_id": batch_id,
            "adjustment_type": "damage",
            "quantity": 2.0,
            "reason": "Broken during shelf reorganization",
        },
    )
    assert adj_res.status_code == 201
    assert adj_res.json()["quantity"] == 2.0

    # Stock now 84 - 2 = 82
    inv = client.get("/api/pharmacy/inventory", params={"search": "Amlodipine"}).json()
    assert inv[0]["available_stock"] == 82.0


def test_pharmacy_dashboard_and_reports(pharm_client):
    client, hospital_id, _ = pharm_client

    # 1. Dashboard
    dash_res = client.get("/api/pharmacy/dashboard")
    assert dash_res.status_code == 200
    dash = dash_res.json()
    assert "today_sales_amount" in dash
    assert "low_stock_count" in dash

    # 2. Reports
    r_stock = client.get("/api/pharmacy/reports", params={"report": "current_stock"})
    assert r_stock.status_code == 200
    assert r_stock.json()["report"] == "current_stock"

    r_sales = client.get("/api/pharmacy/reports", params={"report": "sales"})
    assert r_sales.status_code == 200

    r_profit = client.get("/api/pharmacy/reports", params={"report": "profit"})
    assert r_profit.status_code == 200
    assert "profit" in r_profit.json()

    # 3. Search
    search_res = client.get("/api/pharmacy/search", params={"q": "Amlodipine"})
    assert search_res.status_code == 200


def test_pharmacy_same_batch_additive_purchases_and_row_locking(pharm_client):
    """Test 11 & 12: Same-batch increments are strictly additive and inventory summary remains accurate."""
    client, hospital_id, _ = pharm_client

    cat = client.post("/api/pharmacy/categories", json={"name": "Vitamins"}).json()
    supp = client.post("/api/pharmacy/suppliers", json={"company_name": "HealthCorp Pharma"}).json()

    med = client.post(
        "/api/pharmacy/medicines",
        json={
            "medicine_name": "Vitamin C 500mg",
            "brand_name": "Limcee",
            "manufacturer": "Abbott",
            "category_id": cat["id"],
            "strength": "500mg",
            "selling_price": 5.0,
            "purchase_price": 3.0,
            "mrp": 6.0,
        },
    ).json()
    med_id = med["id"]
    batch_num = "BATCH-ADDITIVE-01"
    today = date.today()
    exp = today + timedelta(days=365)

    # 1. Purchase 1: receives 100 units
    p1 = client.post(
        "/api/pharmacy/purchases",
        json={
            "supplier_id": supp["id"],
            "invoice_number": "INV-P1-100",
            "invoice_date": today.isoformat(),
            "received_date": today.isoformat(),
            "payment_status": "paid",
            "items": [
                {
                    "medicine_id": med_id,
                    "batch_number": batch_num,
                    "expiry_date": exp.isoformat(),
                    "quantity": 100.0,
                    "purchase_price": 3.0,
                    "mrp": 6.0,
                    "selling_price": 5.0,
                }
            ],
        },
    )
    assert p1.status_code == 201

    batches_1 = client.get(f"/api/pharmacy/medicines/{med_id}/batches").json()
    assert len(batches_1) == 1
    assert batches_1[0]["available_quantity"] == 100.0

    # 2. Purchase 2: receives +50 units into SAME batch
    p2 = client.post(
        "/api/pharmacy/purchases",
        json={
            "supplier_id": supp["id"],
            "invoice_number": "INV-P2-50",
            "invoice_date": today.isoformat(),
            "received_date": today.isoformat(),
            "payment_status": "paid",
            "items": [
                {
                    "medicine_id": med_id,
                    "batch_number": batch_num,
                    "expiry_date": exp.isoformat(),
                    "quantity": 50.0,
                    "purchase_price": 3.0,
                    "mrp": 6.0,
                    "selling_price": 5.0,
                }
            ],
        },
    )
    assert p2.status_code == 201

    batches_2 = client.get(f"/api/pharmacy/medicines/{med_id}/batches").json()
    assert len(batches_2) == 1
    assert batches_2[0]["available_quantity"] == 150.0

    # 3. Purchase 3: receives +25 units into SAME batch
    p3 = client.post(
        "/api/pharmacy/purchases",
        json={
            "supplier_id": supp["id"],
            "invoice_number": "INV-P3-25",
            "invoice_date": today.isoformat(),
            "received_date": today.isoformat(),
            "payment_status": "paid",
            "items": [
                {
                    "medicine_id": med_id,
                    "batch_number": batch_num,
                    "expiry_date": exp.isoformat(),
                    "quantity": 25.0,
                    "purchase_price": 3.0,
                    "mrp": 6.0,
                    "selling_price": 5.0,
                }
            ],
        },
    )
    assert p3.status_code == 201

    batches_3 = client.get(f"/api/pharmacy/medicines/{med_id}/batches").json()
    assert len(batches_3) == 1
    assert batches_3[0]["available_quantity"] == 175.0

    # Inventory summary matches batch sum exactly
    inv = client.get("/api/pharmacy/inventory", params={"search": "Vitamin C"}).json()
    assert len(inv) == 1
    assert inv[0]["available_stock"] == 175.0


def test_concurrent_creation_of_same_new_pharmacy_batch(pharm_client, pharm_db):
    """Test concurrent creation of the same previously nonexistent batch is handled safely without unhandled IntegrityError."""
    from modules.pharmacy.actions.pharmacy_actions import PharmacyActions
    from modules.pharmacy.contracts.pharmacy_contracts import PurchaseCreate, PurchaseItemCreate, PurchasePaymentStatus
    from modules.pharmacy.entities.pharmacy_entities import MedicineBatch

    client, hosp_id, current_user = pharm_client

    cat_res = client.post("/api/pharmacy/categories", json={"name": "Antibiotics"})
    assert cat_res.status_code == 201
    cat = cat_res.json()

    supp_res = client.post(
        "/api/pharmacy/suppliers",
        json={"company_name": "Pharma Corp", "gstin": "07AAAAA0000A1Z5", "phone": "9876543210"},
    )
    assert supp_res.status_code == 201
    supp = supp_res.json()

    med_res = client.post(
        "/api/pharmacy/medicines",
        json={
            "medicine_name": "Amoxicillin 500mg",
            "generic_name": "Amoxicillin",
            "brand_name": "Amoxil",
            "manufacturer": "GSK",
            "category_id": cat["id"],
            "medicine_type": "capsule",
            "unit": "strip",
            "purchase_price": 5.0,
            "selling_price": 10.0,
            "mrp": 12.0,
        },
    )
    assert med_res.status_code == 201
    med = med_res.json()

    med_id = UUID(med["id"])
    supp_id = UUID(supp["id"])
    batch_num = "AMX-BATCH-CONCURRENT-01"
    today = date.today()
    exp = today + timedelta(days=365)

    # Verify batch does not exist initially
    existing_initial = pharm_db.query(MedicineBatch).filter_by(
        hospital_id=hosp_id, medicine_id=med_id, batch_number=batch_num
    ).first()
    assert existing_initial is None

    # Hook begin_nested on pharm_db so that a concurrent worker (Session 2)
    # inserts and commits the conflicting batch row AFTER Session 1's initial lookup
    # returned empty, but BEFORE Session 1's new-batch INSERT flush occurs.
    session_factory_concurrent = sessionmaker(bind=pharm_db.bind, autoflush=False, autocommit=False)
    original_begin_nested = pharm_db.begin_nested
    collision_triggered = False

    def hooked_begin_nested(*args, **kwargs):
        nonlocal collision_triggered
        sp = original_begin_nested(*args, **kwargs)
        if not collision_triggered:
            collision_triggered = True
            session2 = session_factory_concurrent()
            try:
                concurrent_batch = MedicineBatch(
                    hospital_id=hosp_id,
                    medicine_id=med_id,
                    supplier_id=supp_id,
                    batch_number=batch_num,
                    manufacture_date=today,
                    expiry_date=exp,
                    purchase_price=5.0,
                    selling_price=10.0,
                    mrp=12.0,
                    initial_quantity=40.0,
                    available_quantity=40.0,
                    is_active=True,
                )
                session2.add(concurrent_batch)
                session2.commit()
            finally:
                session2.close()
        return sp

    pharm_db.begin_nested = hooked_begin_nested

    try:
        actions = PharmacyActions(
            pharm_db,
            hospital_id=hosp_id,
            user={"user_id": str(uuid4()), "name": "Pharmacist", "role": "pharmacist"},
        )
        purchase_payload = PurchaseCreate(
            supplier_id=supp_id,
            invoice_number="INV-CONCURRENT-RACE-01",
            invoice_date=today,
            received_date=today,
            payment_status=PurchasePaymentStatus.paid,
            items=[
                PurchaseItemCreate(
                    medicine_id=med_id,
                    batch_number=batch_num,
                    manufacture_date=today,
                    expiry_date=exp,
                    quantity=60.0,
                    purchase_price=5.0,
                    selling_price=10.0,
                    mrp=12.0,
                )
            ],
        )

        resp = actions.create_purchase(purchase_payload)
        pharm_db.commit()
    finally:
        pharm_db.begin_nested = original_begin_nested

    assert collision_triggered is True, "Test must genuinely exercise the collision/recovery branch"
    assert resp.invoice_number == "INV-CONCURRENT-RACE-01"

    # Batch quantity must reflect the sum of both (40 + 60 = 100)
    batch_rows = pharm_db.query(MedicineBatch).filter_by(
        hospital_id=hosp_id, medicine_id=med_id, batch_number=batch_num
    ).all()
    assert len(batch_rows) == 1
    assert batch_rows[0].available_quantity == 100.0


def test_mixed_collision_and_non_collision_new_batches(pharm_client, pharm_db):
    """
    Required Test 2:
    Purchase request:
      Batch A (+60)
      Batch B (+30)
    Initial lookup: A missing, B missing.
    Concurrent transaction creates Batch A (+40).
    Current transaction collides on A, recovers A (+60 -> 100), and MUST still create B (+30).
    Final state:
      Batch A: 1 row, available_quantity = 100
      Batch B: 1 row, available_quantity = 30
      Zero missing batches, zero duplicate rows.
    """
    client, hosp_id, current_user = pharm_client

    supp_res = client.post(
        "/api/pharmacy/suppliers",
        json={"company_name": "Supplier Mixed", "contact_person": "Pooja", "phone": "9811122233"},
    )
    assert supp_res.status_code == 201
    supp_id = UUID(supp_res.json()["id"])

    med_a_res = client.post(
        "/api/pharmacy/medicines",
        json={"medicine_name": "Medicine Alpha", "brand_name": "AlphaBrand", "manufacturer": "Pharma", "medicine_type": "tablet", "unit": "strip"},
    )
    med_b_res = client.post(
        "/api/pharmacy/medicines",
        json={"medicine_name": "Medicine Beta", "brand_name": "BetaBrand", "manufacturer": "Pharma", "medicine_type": "tablet", "unit": "strip"},
    )
    assert med_a_res.status_code == 201 and med_b_res.status_code == 201
    med_a_id = UUID(med_a_res.json()["id"])
    med_b_id = UUID(med_b_res.json()["id"])

    today = date.today()
    exp = today + timedelta(days=365)
    batch_a_num = "BATCH-A-COLLIDING"
    batch_b_num = "BATCH-B-SURVIVING"

    session_factory_concurrent = sessionmaker(bind=pharm_db.bind, autoflush=False, autocommit=False)
    original_begin_nested = pharm_db.begin_nested
    collision_triggered = False

    def hooked_begin_nested(*args, **kwargs):
        nonlocal collision_triggered
        sp = original_begin_nested(*args, **kwargs)
        if not collision_triggered:
            collision_triggered = True
            session2 = session_factory_concurrent()
            try:
                # Concurrent worker inserts ONLY Batch A (+40)
                b_a = MedicineBatch(
                    hospital_id=hosp_id,
                    medicine_id=med_a_id,
                    supplier_id=supp_id,
                    batch_number=batch_a_num,
                    manufacture_date=today,
                    expiry_date=exp,
                    purchase_price=10.0,
                    selling_price=15.0,
                    mrp=18.0,
                    initial_quantity=40.0,
                    available_quantity=40.0,
                    is_active=True,
                )
                session2.add(b_a)
                session2.commit()
            finally:
                session2.close()
        return sp

    pharm_db.begin_nested = hooked_begin_nested

    try:
        actions = PharmacyActions(
            pharm_db,
            hospital_id=hosp_id,
            user={"user_id": str(uuid4()), "name": "Pharmacist", "role": "pharmacist"},
        )
        payload = PurchaseCreate(
            supplier_id=supp_id,
            invoice_number="INV-MIXED-001",
            invoice_date=today,
            received_date=today,
            payment_status=PurchasePaymentStatus.paid,
            items=[
                PurchaseItemCreate(
                    medicine_id=med_a_id,
                    batch_number=batch_a_num,
                    manufacture_date=today,
                    expiry_date=exp,
                    quantity=60.0,
                    purchase_price=10.0,
                    selling_price=15.0,
                    mrp=18.0,
                ),
                PurchaseItemCreate(
                    medicine_id=med_b_id,
                    batch_number=batch_b_num,
                    manufacture_date=today,
                    expiry_date=exp,
                    quantity=30.0,
                    purchase_price=20.0,
                    selling_price=25.0,
                    mrp=30.0,
                ),
            ],
        )
        resp = actions.create_purchase(payload)
        pharm_db.commit()
    finally:
        pharm_db.begin_nested = original_begin_nested

    assert collision_triggered is True
    assert resp.invoice_number == "INV-MIXED-001"
    assert len(resp.items) == 2

    # Assert Batch A: recovered and additive (40 + 60 = 100)
    rows_a = pharm_db.query(MedicineBatch).filter_by(
        hospital_id=hosp_id, medicine_id=med_a_id, batch_number=batch_a_num
    ).all()
    assert len(rows_a) == 1, "Exactly one row for Batch A must exist"
    assert rows_a[0].available_quantity == 100.0

    # Assert Batch B: created successfully despite collision on Batch A (30)
    rows_b = pharm_db.query(MedicineBatch).filter_by(
        hospital_id=hosp_id, medicine_id=med_b_id, batch_number=batch_b_num
    ).all()
    assert len(rows_b) == 1, "Batch B must not disappear due to Batch A collision"
    assert rows_b[0].available_quantity == 30.0


def test_three_new_batches_middle_collides_all_survive(pharm_client, pharm_db):
    """
    Required Test 3:
    Three new batches in purchase:
      Batch A (+20) -> new
      Batch B (+50) -> collides with concurrent insert (+30)
      Batch C (+70) -> new
    Expected:
      Batch A created with 20.
      Batch B recovered with 30 + 50 = 80.
      Batch C created with 70.
      All three survive; non-conflicting batches A and C are both preserved.
    """
    client, hosp_id, current_user = pharm_client

    supp_res = client.post(
        "/api/pharmacy/suppliers",
        json={"company_name": "Supplier Tri", "contact_person": "Sunil", "phone": "9811122244"},
    )
    supp_id = UUID(supp_res.json()["id"])

    med_a = UUID(client.post("/api/pharmacy/medicines", json={"medicine_name": "Med 1", "brand_name": "B1", "manufacturer": "M"}).json()["id"])
    med_b = UUID(client.post("/api/pharmacy/medicines", json={"medicine_name": "Med 2", "brand_name": "B2", "manufacturer": "M"}).json()["id"])
    med_c = UUID(client.post("/api/pharmacy/medicines", json={"medicine_name": "Med 3", "brand_name": "B3", "manufacturer": "M"}).json()["id"])

    today = date.today()
    exp = today + timedelta(days=365)
    batch_a_num = "BAT-A-NEW"
    batch_b_num = "BAT-B-MIDDLE-COLLIDE"
    batch_c_num = "BAT-C-NEW"

    session_factory_concurrent = sessionmaker(bind=pharm_db.bind, autoflush=False, autocommit=False)
    original_begin_nested = pharm_db.begin_nested
    collision_triggered = False

    def hooked_begin_nested(*args, **kwargs):
        nonlocal collision_triggered
        sp = original_begin_nested(*args, **kwargs)
        if not collision_triggered:
            collision_triggered = True
            session2 = session_factory_concurrent()
            try:
                # Concurrent worker inserts ONLY Batch B (+30)
                b_b = MedicineBatch(
                    hospital_id=hosp_id,
                    medicine_id=med_b,
                    supplier_id=supp_id,
                    batch_number=batch_b_num,
                    manufacture_date=today,
                    expiry_date=exp,
                    purchase_price=10.0,
                    selling_price=15.0,
                    mrp=18.0,
                    initial_quantity=30.0,
                    available_quantity=30.0,
                    is_active=True,
                )
                session2.add(b_b)
                session2.commit()
            finally:
                session2.close()
        return sp

    pharm_db.begin_nested = hooked_begin_nested

    try:
        actions = PharmacyActions(
            pharm_db,
            hospital_id=hosp_id,
            user={"user_id": str(uuid4()), "name": "Pharmacist", "role": "pharmacist"},
        )
        payload = PurchaseCreate(
            supplier_id=supp_id,
            invoice_number="INV-TRI-001",
            invoice_date=today,
            received_date=today,
            payment_status=PurchasePaymentStatus.paid,
            items=[
                PurchaseItemCreate(medicine_id=med_a, batch_number=batch_a_num, expiry_date=exp, quantity=20.0, purchase_price=10.0, selling_price=15.0, mrp=18.0),
                PurchaseItemCreate(medicine_id=med_b, batch_number=batch_b_num, expiry_date=exp, quantity=50.0, purchase_price=10.0, selling_price=15.0, mrp=18.0),
                PurchaseItemCreate(medicine_id=med_c, batch_number=batch_c_num, expiry_date=exp, quantity=70.0, purchase_price=10.0, selling_price=15.0, mrp=18.0),
            ],
        )
        resp = actions.create_purchase(payload)
        pharm_db.commit()
    finally:
        pharm_db.begin_nested = original_begin_nested

    assert collision_triggered is True
    assert len(resp.items) == 3

    # Check A
    rows_a = pharm_db.query(MedicineBatch).filter_by(hospital_id=hosp_id, medicine_id=med_a, batch_number=batch_a_num).all()
    assert len(rows_a) == 1
    assert rows_a[0].available_quantity == 20.0

    # Check B
    rows_b = pharm_db.query(MedicineBatch).filter_by(hospital_id=hosp_id, medicine_id=med_b, batch_number=batch_b_num).all()
    assert len(rows_b) == 1
    assert rows_b[0].available_quantity == 80.0

    # Check C
    rows_c = pharm_db.query(MedicineBatch).filter_by(hospital_id=hosp_id, medicine_id=med_c, batch_number=batch_c_num).all()
    assert len(rows_c) == 1
    assert rows_c[0].available_quantity == 70.0


def test_purchase_request_local_duplicate_batch_key_is_additive(pharm_client, pharm_db):
    """
    Required Test 4:
    Purchase contains two lines for the exact same batch key:
      Line 1: Batch X +10
      Line 2: Batch X +15
    Expected:
      ONE physical MedicineBatch row in DB.
      Total available_quantity = 25.
      Both purchase items link to the same batch.
    """
    client, hosp_id, current_user = pharm_client

    supp_res = client.post(
        "/api/pharmacy/suppliers",
        json={"company_name": "Supplier Dup", "contact_person": "Ravi", "phone": "9811122255"},
    )
    supp_id = UUID(supp_res.json()["id"])

    med_res = client.post(
        "/api/pharmacy/medicines",
        json={"medicine_name": "Paracetamol 500mg", "brand_name": "Calpol", "manufacturer": "GSK", "medicine_type": "tablet", "unit": "strip"},
    )
    med_id = UUID(med_res.json()["id"])

    today = date.today()
    exp = today + timedelta(days=365)
    batch_num = "PARA-BATCH-DUP-01"

    actions = PharmacyActions(
        pharm_db,
        hospital_id=hosp_id,
        user={"user_id": str(uuid4()), "name": "Pharmacist", "role": "pharmacist"},
    )
    payload = PurchaseCreate(
        supplier_id=supp_id,
        invoice_number="INV-DUP-LINES-01",
        invoice_date=today,
        received_date=today,
        payment_status=PurchasePaymentStatus.paid,
        items=[
            PurchaseItemCreate(
                medicine_id=med_id,
                batch_number=batch_num,
                manufacture_date=today,
                expiry_date=exp,
                quantity=10.0,
                purchase_price=2.0,
                selling_price=4.0,
                mrp=5.0,
            ),
            PurchaseItemCreate(
                medicine_id=med_id,
                batch_number=batch_num,
                manufacture_date=today,
                expiry_date=exp,
                quantity=15.0,
                purchase_price=2.0,
                selling_price=4.0,
                mrp=5.0,
            ),
        ],
    )
    resp = actions.create_purchase(payload)
    pharm_db.commit()

    assert resp.invoice_number == "INV-DUP-LINES-01"
    assert len(resp.items) == 2

    # Exactly ONE physical MedicineBatch row in DB
    batch_rows = pharm_db.query(MedicineBatch).filter_by(
        hospital_id=hosp_id, medicine_id=med_id, batch_number=batch_num
    ).all()
    assert len(batch_rows) == 1, "Must NOT create duplicate MedicineBatch rows for identical batch key"
    assert batch_rows[0].available_quantity == 25.0, "Quantity must be additive: 10 + 15 = 25"

    # Both purchase items point to this exact batch row
    assert resp.items[0].batch_id == batch_rows[0].id
    assert resp.items[1].batch_id == batch_rows[0].id



