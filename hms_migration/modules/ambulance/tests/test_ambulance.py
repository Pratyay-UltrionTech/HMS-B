"""
Unit and integration tests for Ambulance module (Features 10 & 11).

Tests:
1. Ambulance Fleet Management (/ambulance/vehicles)
2. Ambulance Dispatch Lifecycle (/ambulance/dispatches)
3. Multi-tenant isolation
"""

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hospital
from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.ambulance.api.ambulance_api import router as ambulance_router
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital, hospital_b


@pytest.fixture(scope="function")
def ambulance_app(db_session: Session) -> FastAPI:
    test_app = FastAPI(title="Ambulance Test App")
    test_app.include_router(ambulance_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(ambulance_app: FastAPI) -> TestClient:
    return TestClient(ambulance_app)


@pytest.fixture(scope="function")
def staff_auth(hospital: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "dispatcher@hospital.com",
        "name": "Dispatcher Joe",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(uuid4()),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def staff_auth_b(hospital_b: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "dispatcher2@hospitalb.com",
        "name": "Dispatcher B",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_b.id),
        "user_id": str(uuid4()),
    })
    return {"Authorization": f"Bearer {token}"}


def test_ambulance_fleet_crud(client: TestClient, db_session: Session, hospital: Hospital, staff_auth: dict[str, str]):
    """Test Feature 10: Ambulance Fleet Management."""
    Base.metadata.create_all(db_session.bind)

    # 1. Register vehicle
    veh_res = client.post(
        "/api/ambulance/vehicles",
        json={
            "registration_number": "KA-01-AMB-1001",
            "vehicle_type": "als",
            "model": "Force Traveller 3350",
            "operational_status": "available",
            "onboard_equipment": {"ventilator": True, "defibrillator": True, "suction": True},
            "current_driver_name": "Ramesh Kumar",
            "current_paramedic_name": "Suresh Nurse",
        },
        headers=staff_auth,
    )
    assert veh_res.status_code == 201
    veh_data = veh_res.json()
    veh_id = veh_data["id"]
    assert veh_data["registration_number"] == "KA-01-AMB-1001"
    assert veh_data["vehicle_type"] == "als"
    assert veh_data["operational_status"] == "available"

    # 2. Duplicate registration number rejected
    dup_res = client.post(
        "/api/ambulance/vehicles",
        json={
            "registration_number": "KA-01-AMB-1001",
            "vehicle_type": "bls",
            "model": "Tata Winger",
        },
        headers=staff_auth,
    )
    assert dup_res.status_code == 409

    # 3. List vehicles
    list_res = client.get("/api/ambulance/vehicles", headers=staff_auth)
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1

    # 4. Update status to maintenance
    upd_res = client.put(
        f"/api/ambulance/vehicles/{veh_id}",
        json={"operational_status": "maintenance"},
        headers=staff_auth,
    )
    assert upd_res.status_code == 200
    assert upd_res.json()["operational_status"] == "maintenance"


def test_ambulance_dispatch_lifecycle(client: TestClient, db_session: Session, hospital: Hospital, staff_auth: dict[str, str]):
    """Test Feature 11: Ambulance Dispatch & Trip Tracking."""
    Base.metadata.create_all(db_session.bind)

    # Register available vehicle
    veh_id = client.post(
        "/api/ambulance/vehicles",
        json={
            "registration_number": "KA-01-AMB-2002",
            "vehicle_type": "bls",
            "model": "Maruti Eeco Ambulance",
            "operational_status": "available",
        },
        headers=staff_auth,
    ).json()["id"]

    # 1. Dispatch ambulance
    disp_res = client.post(
        "/api/ambulance/dispatches",
        json={
            "ambulance_id": veh_id,
            "caller_name": "John Doe",
            "caller_phone": "9876543210",
            "pickup_location": "MG Road Metro Station",
            "drop_location": "Apollo Emergency Ward",
            "priority": "emergency",
            "driver_name": "Ramesh Driver",
            "paramedic_name": "Anita Paramedic",
            "odometer_start": 12500.0,
            "remarks": "Traffic accident reported, head trauma",
        },
        headers=staff_auth,
    )
    assert disp_res.status_code == 201
    disp_data = disp_res.json()
    disp_id = disp_data["id"]
    assert disp_data["dispatch_code"].startswith("DISP-")
    assert disp_data["status"] == "dispatched"

    # Verify vehicle status is now on_trip
    v_info = client.get("/api/ambulance/vehicles", headers=staff_auth).json()
    assert any(v["id"] == veh_id and v["operational_status"] == "on_trip" for v in v_info)

    # Attempting to dispatch an already on-trip vehicle should fail (409 Conflict)
    busy_res = client.post(
        "/api/ambulance/dispatches",
        json={
            "ambulance_id": veh_id,
            "caller_name": "Jane",
            "caller_phone": "9876543211",
            "pickup_location": "Indiranagar",
            "drop_location": "Apollo",
            "driver_name": "Ramesh",
        },
        headers=staff_auth,
    )
    assert busy_res.status_code == 409

    # 2. Transition status: at_scene
    s1 = client.put(f"/api/ambulance/dispatches/{disp_id}/status", json={"status": "at_scene"}, headers=staff_auth)
    assert s1.status_code == 200
    assert s1.json()["status"] == "at_scene"
    assert s1.json()["arrived_pickup_at"] is not None

    # 3. Transition status: transporting
    s2 = client.put(f"/api/ambulance/dispatches/{disp_id}/status", json={"status": "transporting"}, headers=staff_auth)
    assert s2.status_code == 200
    assert s2.json()["status"] == "transporting"
    assert s2.json()["departed_pickup_at"] is not None

    # 4. Complete trip
    s3 = client.put(
        f"/api/ambulance/dispatches/{disp_id}/status",
        json={"status": "completed", "odometer_end": 12518.5, "remarks": "Patient transferred safely to ED triage"},
        headers=staff_auth,
    )
    assert s3.status_code == 200
    assert s3.json()["status"] == "completed"
    assert s3.json()["completed_at"] is not None

    # Verify vehicle is released back to available
    v_after = client.get("/api/ambulance/vehicles", headers=staff_auth).json()
    assert any(v["id"] == veh_id and v["operational_status"] == "available" for v in v_after)


def test_ambulance_tenant_isolation(client: TestClient, db_session: Session, hospital: Hospital, hospital_b: Hospital, staff_auth: dict[str, str], staff_auth_b: dict[str, str]):
    """Test Tenant Isolation for Ambulance domain."""
    Base.metadata.create_all(db_session.bind)

    client.post(
        "/api/ambulance/vehicles",
        json={"registration_number": "HOSP-A-AMB", "vehicle_type": "bls", "model": "Model A"},
        headers=staff_auth,
    )

    # Hospital B cannot see Hospital A's ambulance
    list_b = client.get("/api/ambulance/vehicles", headers=staff_auth_b).json()
    assert not any(v["registration_number"] == "HOSP-A-AMB" for v in list_b)
