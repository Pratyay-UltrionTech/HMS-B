"""
Unit and integration tests for Equipment domain.
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
from hms_migration.modules.equipment.api.equipment_api import router as equipment_router
from hms_migration.modules.equipment.entities.equipment_entities import (
    EquipmentAssignTarget,
    EquipmentAssignment,
    EquipmentCategory,
    EquipmentItem,
    EquipmentMaintenance,
    EquipmentRequest,
    EquipmentRequestStatus,
    EquipmentServiceLog,
    EquipmentStatus,
    MaintenanceStatus,
)
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def equip_db():
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
def equip_client(equip_db):
    app = FastAPI()
    app.include_router(equipment_router, prefix="/api")

    hospital_id = uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id=f"HOSP{hospital_id.hex[:6].upper()}",
        name="Equipment Test Hospital",
        address="123 Health St",
        phone="9876543210",
        email=f"admin_{hospital_id.hex[:6]}@equiphosp.org",
        password_hash="hash",
        is_active=True,
    )
    equip_db.add(hospital)
    equip_db.commit()

    current_user = {
        "id": str(uuid4()),
        "sub": "equip_admin@example.com",
        "email": "equip_admin@example.com",
        "role": "admin",
        "name": "Equipment Admin",
        "hospital_id": str(hospital_id),
    }

    def override_session():
        yield equip_db

    def override_auth():
        return current_user

    def override_hospital():
        return hospital_id

    app.dependency_overrides[get_transitional_sync_session] = override_session
    app.dependency_overrides[require_hospital_user] = override_auth
    app.dependency_overrides[get_hospital_context] = override_hospital

    return TestClient(app), hospital_id, current_user


def test_equipment_categories_crud(equip_client):
    client, hospital_id, _ = equip_client

    # 1. List default categories
    res = client.get("/api/equipment/categories")
    assert res.status_code == 200
    cats = res.json()
    assert len(cats) >= 8
    cat_names = [c["name"] for c in cats]
    assert "Diagnostic" in cat_names
    assert "Surgical" in cat_names

    # 2. Create new category
    create_res = client.post(
        "/api/equipment/categories",
        json={"name": "Endoscopy", "description": "Endoscopic equipment", "is_active": True},
    )
    assert create_res.status_code == 201
    cat_data = create_res.json()
    assert cat_data["name"] == "Endoscopy"
    cat_id = cat_data["id"]

    # 3. Duplicate conflict
    dup_res = client.post("/api/equipment/categories", json={"name": "Endoscopy"})
    assert dup_res.status_code == 409

    # 4. Update category
    up_res = client.put(
        f"/api/equipment/categories/{cat_id}",
        json={"name": "Advanced Endoscopy", "description": "Updated description"},
    )
    assert up_res.status_code == 200
    assert up_res.json()["name"] == "Advanced Endoscopy"

    # 5. Delete category
    del_res = client.delete(f"/api/equipment/categories/{cat_id}")
    assert del_res.status_code == 204

    # Verify deleted
    list_res = client.get("/api/equipment/categories")
    assert not any(c["id"] == cat_id for c in list_res.json())


def test_equipment_items_crud_and_amc(equip_client):
    client, hospital_id, _ = equip_client

    # First get a category
    cats = client.get("/api/equipment/categories").json()
    diag_cat = next(c for c in cats if c["name"] == "Diagnostic")

    # 1. Create equipment item
    payload = {
        "name": "Ultrasound Scanner Pro",
        "category_id": diag_cat["id"],
        "manufacturer": "GE Healthcare",
        "model": "Logic E9",
        "serial_number": "SN-GE-998811",
        "purchase_date": str(date.today()),
        "purchase_cost": 45000.0,
        "department": "Radiology",
        "current_location": "Scan Room 1",
        "status": "available",
        "vendor": "GE India Pvt Ltd",
    }
    create_res = client.post("/api/equipment/items", json=payload)
    assert create_res.status_code == 201
    item = create_res.json()
    assert item["name"] == "Ultrasound Scanner Pro"
    assert item["asset_id"] == "EQ001"
    assert item["category_name"] == "Diagnostic"
    item_id = item["id"]

    # 2. List items
    list_res = client.get("/api/equipment/items", params={"search": "Ultrasound"})
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    # 3. Update AMC
    amc_res = client.put(
        f"/api/equipment/items/{item_id}/amc",
        json={
            "vendor": "GE Healthcare Support",
            "warranty_start": str(date.today()),
            "warranty_end": str(date.today() + timedelta(days=365)),
            "amc_start": str(date.today() + timedelta(days=366)),
            "amc_end": str(date.today() + timedelta(days=730)),
        },
    )
    assert amc_res.status_code == 200
    assert amc_res.json()["vendor"] == "GE Healthcare Support"

    # 4. Dashboard
    dash_res = client.get("/api/equipment/dashboard")
    assert dash_res.status_code == 200
    dash = dash_res.json()
    assert dash["total"] == 1
    assert dash["available"] == 1
    assert dash["in_use"] == 0

    # 5. Update item
    up_res = client.put(f"/api/equipment/items/{item_id}", json={"department": "OPD Diagnostic"})
    assert up_res.status_code == 200
    assert up_res.json()["department"] == "OPD Diagnostic"

    # 6. Delete item
    del_res = client.delete(f"/api/equipment/items/{item_id}")
    assert del_res.status_code == 204


def test_equipment_assignments_and_return(equip_client):
    client, hospital_id, _ = equip_client

    # Create an item
    create_res = client.post(
        "/api/equipment/items",
        json={
            "name": "Infusion Pump",
            "department": "ICU",
            "status": "available",
        },
    )
    assert create_res.status_code == 201
    item = create_res.json()
    item_id = item["id"]

    # 1. Assign to ICU Bed 4
    assign_res = client.post(
        "/api/equipment/assignments",
        json={
            "equipment_id": item_id,
            "target_type": "room",
            "target_name": "ICU Bed 4",
            "remarks": "Patient emergency infusion",
        },
    )
    assert assign_res.status_code == 201
    assign_data = assign_res.json()
    assert assign_data["is_active"] is True
    assert assign_data["target_name"] == "ICU Bed 4"
    assignment_id = assign_data["id"]

    # Item should now be in_use
    item_check = client.get("/api/equipment/items").json()[0]
    assert item_check["status"] == "in_use"
    assert "room: ICU Bed 4" in item_check["active_assignment"]

    # 2. List assignments
    list_res = client.get("/api/equipment/assignments", params={"active_only": True})
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    # 3. Return assignment
    ret_res = client.post(f"/api/equipment/assignments/{assignment_id}/return")
    assert ret_res.status_code == 200
    assert ret_res.json()["is_active"] is False

    # Item should now be available again
    item_check = client.get("/api/equipment/items").json()[0]
    assert item_check["status"] == "available"


def test_equipment_maintenance_and_service_logs(equip_client):
    client, hospital_id, _ = equip_client

    # Create an item
    item = client.post(
        "/api/equipment/items",
        json={
            "name": "Defibrillator",
            "department": "Emergency",
            "status": "available",
        },
    ).json()
    item_id = item["id"]

    # 1. Schedule maintenance
    maint_res = client.post(
        "/api/equipment/maintenance",
        json={
            "equipment_id": item_id,
            "next_service_date": str(date.today() + timedelta(days=30)),
            "remarks": "Quarterly calibration check",
        },
    )
    assert maint_res.status_code == 201
    maint = maint_res.json()
    assert maint["status"] in ("scheduled", "ok")
    maint_id = maint["id"]

    # 2. List maintenance
    list_res = client.get("/api/equipment/maintenance")
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1

    # 3. Complete maintenance
    comp_res = client.post(
        f"/api/equipment/maintenance/{maint_id}/complete",
        json={
            "work_done": "Battery replaced and pads tested",
            "engineer": "John Engineer",
            "cost": 1500.0,
            "remarks": "All parameters normal",
            "next_service_date": str(date.today() + timedelta(days=90)),
        },
    )
    assert comp_res.status_code == 200
    assert comp_res.json()["status"] == "completed"

    # 4. Check service logs (auto-created from maintenance completion)
    logs_res = client.get("/api/equipment/service-logs", params={"equipment_id": item_id})
    assert logs_res.status_code == 200
    logs = logs_res.json()
    assert len(logs) == 1
    assert logs[0]["engineer"] == "John Engineer"
    assert logs[0]["cost"] == 1500.0

    # 5. Create direct service log
    direct_log_res = client.post(
        "/api/equipment/service-logs",
        json={
            "equipment_id": item_id,
            "service_date": str(date.today()),
            "work_done": "Firmware update to v2.4",
            "engineer": "Tech Specialist",
            "cost": 500.0,
        },
    )
    assert direct_log_res.status_code == 201
    assert direct_log_res.json()["work_done"] == "Firmware update to v2.4"


def test_equipment_requests_workflow(equip_client):
    client, hospital_id, _ = equip_client

    # 1. Create request
    req_res = client.post(
        "/api/equipment/requests",
        json={
            "department": "Cardiology",
            "equipment_name": "ECG Machine 12-Lead",
            "quantity": 1,
            "remarks": "Urgent requirement for OPD room 3",
        },
    )
    assert req_res.status_code == 201
    req = req_res.json()
    assert req["request_no"] == "ER0001"
    assert req["status"] == "pending"
    req_id = req["id"]

    # 2. List requests
    list_res = client.get("/api/equipment/requests", params={"status": "pending"})
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    # 3. Approve request
    appr_res = client.post(
        f"/api/equipment/requests/{req_id}/approve",
        json={"admin_remarks": "Approved by HOD"},
    )
    assert appr_res.status_code == 200
    assert appr_res.json()["status"] == "approved"

    # 4. Create available equipment to fulfill the request
    item = client.post(
        "/api/equipment/items",
        json={
            "name": "ECG Machine 12-Lead",
            "department": "Storage",
            "status": "available",
        },
    ).json()

    # 5. Assign equipment to fulfill request
    assign_res = client.post(
        f"/api/equipment/requests/{req_id}/assign",
        json={
            "equipment_id": item["id"],
            "admin_remarks": "Assigned from Central Store",
        },
    )
    assert assign_res.status_code == 200
    resolved_req = assign_res.json()
    assert resolved_req["status"] == "assigned"
    assert resolved_req["assigned_equipment_id"] == item["id"]

    # Verify item status is now in_use and department is Cardiology
    item_check = client.get("/api/equipment/items", params={"search": "ECG"}).json()[0]
    assert item_check["status"] == "in_use"
    assert item_check["department"] == "Cardiology"
