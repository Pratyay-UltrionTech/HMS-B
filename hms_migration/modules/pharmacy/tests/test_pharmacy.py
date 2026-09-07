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

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.billing.entities.billing_entities import (
    BillingCharge,
    BillingChargeStatus,
    BillingSourceType,
)
from hms_migration.modules.appointments.entities.appointment_type import AppointmentType
from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
from hms_migration.modules.doctors.entities.doctor import HospitalUser, StaffRole
from hms_migration.modules.patients.entities.patient import Patient
from hms_migration.modules.pharmacy.api.pharmacy_api import router as pharmacy_router
from hms_migration.modules.pharmacy.entities.pharmacy_entities import (
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
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_user


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
        "sub": "pharm_admin@example.com",
        "email": "pharm_admin@example.com",
        "role": "admin",
        "name": "Pharmacist Admin",
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
