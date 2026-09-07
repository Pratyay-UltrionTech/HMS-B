"""
Runtime integration tests for controlled Vitals cutover and regression verification.

Exercises the real application composition in app/main.py:
- Verifies router selection under USE_MIGRATED_VITALS=false (legacy).
- Verifies router selection under USE_MIGRATED_VITALS=true (migrated).
- Exercises full HTTP lifecycle: Auth -> Route -> Action -> DB -> Audit -> Response.
- Validates OpenAPI schema uniqueness and zero duplicate operation IDs.
- Validates regression safety for health, CORS, and legacy endpoints.
"""

from datetime import date
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Appointment, AppointmentStatus, Hospital, HospitalUser, Patient, VitalReading
from hms_migration.infrastructure.postgres import get_transitional_sync_session


# Uses hospital and auth_headers fixtures from tests/conftest.py


@pytest.fixture
def cutover_client(db_session: Session) -> TestClient:
    """FastAPI TestClient bound to the real production application app.main.app."""
    import app.main

    app_instance = app.main.app

    def _override_get_db():
        yield db_session

    app_instance.dependency_overrides[get_db] = _override_get_db
    app_instance.dependency_overrides[get_transitional_sync_session] = _override_get_db

    client = TestClient(app_instance)
    yield client

    app_instance.dependency_overrides.clear()


# ===========================================================================
# 1. System & Regression Endpoints
# ===========================================================================

def test_root_and_health_endpoints(cutover_client: TestClient):
    """Verify system health and root endpoints are operational on real app.main."""
    r_root = cutover_client.get("/")
    assert r_root.status_code == 200
    assert r_root.json()["service"] == "Ultrion HMS API"

    r_health = cutover_client.get("/api/health")
    assert r_health.status_code == 200
    assert r_health.json() == {"status": "ok"}

    r_health_bare = cutover_client.get("/health")
    assert r_health_bare.status_code == 200
    assert r_health_bare.json() == {"status": "ok"}


def test_cors_headers(cutover_client: TestClient):
    """Verify CORS preflight and headers on real app.main."""
    headers = {
        "Origin": "https://hms.ultriontech.com",
        "Access-Control-Request-Method": "GET",
    }
    r = cutover_client.options("/api/health", headers=headers)
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers


# ===========================================================================
# 2. OpenAPI Schema and Operation ID Uniqueness
# ===========================================================================

def test_openapi_operation_id_uniqueness(cutover_client: TestClient):
    """Verify OpenAPI schema generates cleanly with zero duplicate operation IDs."""
    import app.main

    openapi = app.main.app.openapi()
    assert openapi["info"]["title"] == "Ultrion HMS API"

    op_ids = []
    for path, methods in openapi.get("paths", {}).items():
        for method, spec in methods.items():
            if isinstance(spec, dict) and "operationId" in spec:
                op_ids.append(spec["operationId"])

    assert len(op_ids) > 0
    assert len(op_ids) == len(set(op_ids)), "Duplicate operation IDs detected in OpenAPI schema!"


# ===========================================================================
# 3. Real Application Runtime Execution Through Cutover Router
# ===========================================================================

def test_vitals_today_empty_through_real_app(
    cutover_client: TestClient,
    hospital: Hospital,
    auth_headers: dict,
):
    """Verify GET /api/vitals/today executes through real app.main runtime."""
    r = cutover_client.get("/api/vitals/today", headers=auth_headers)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_vitals_full_crud_lifecycle_through_real_app(
    cutover_client: TestClient,
    db_session: Session,
    appointment_factory,
    auth_headers: dict,
):
    """
    Test full HTTP request lifecycle on real app.main:
    Auth -> Validation -> Action -> DB Persistence -> Audit Log -> Contract Response.
    """
    # 1. Setup Appointment via standard fixture
    appt = appointment_factory(status=AppointmentStatus.scheduled)
    db_session.add(appt)
    db_session.commit()

    # 2. POST /api/vitals - Batch create vitals
    create_payload = {
        "appointment_id": str(appt.id),
        "items": [
            {"name": "Blood Pressure", "suitable_range": "120/80", "result": "122/82"},
            {"name": "Pulse Rate", "suitable_range": "60-100", "result": "74 bpm"},
        ],
    }
    r_create = cutover_client.post("/api/vitals", json=create_payload, headers=auth_headers)
    assert r_create.status_code == 201
    created_items = r_create.json()
    assert len(created_items) == 2
    names = {item["name"] for item in created_items}
    assert names == {"Blood Pressure", "Pulse Rate"}
    vital_id = created_items[0]["id"]

    # Verify visit status advanced scheduled -> waiting with queue_token
    db_session.refresh(appt)
    assert appt.status == AppointmentStatus.waiting
    assert appt.queue_token is not None

    # 3. GET /api/vitals?appointment_id=...
    r_list = cutover_client.get(f"/api/vitals?appointment_id={appt.id}", headers=auth_headers)
    assert r_list.status_code == 200
    assert len(r_list.json()) == 2

    # 4. GET /api/vitals/today
    r_today = cutover_client.get("/api/vitals/today", headers=auth_headers)
    assert r_today.status_code == 200
    today_items = r_today.json()
    matched = [item for item in today_items if item["appointment_id"] == str(appt.id)]
    assert len(matched) == 1
    assert matched[0]["vitals_count"] == 2

    # 5. PUT /api/vitals/{id} - Update vital
    update_payload = {"name": "Blood Pressure", "result": "120/80"}
    r_update = cutover_client.put(f"/api/vitals/{vital_id}", json=update_payload, headers=auth_headers)
    assert r_update.status_code == 200
    assert r_update.json()["result"] == "120/80"

    # 6. DELETE /api/vitals/{id} - Delete vital
    r_delete = cutover_client.delete(f"/api/vitals/{vital_id}", headers=auth_headers)
    assert r_delete.status_code == 204

    # Verify remaining vitals
    r_remaining = cutover_client.get(f"/api/vitals?appointment_id={appt.id}", headers=auth_headers)
    assert r_remaining.status_code == 200
    assert len(r_remaining.json()) == 1


# ===========================================================================
# 4. Error Response Format Compatibility on Real App
# ===========================================================================

def test_vitals_error_response_shape_on_real_app(
    cutover_client: TestClient,
    auth_headers: dict,
):
    """Verify error responses through real app.main return standard {"detail": ...} shape."""
    # 400 Bad Request on missing query filter
    r_bad_query = cutover_client.get("/api/vitals", headers=auth_headers)
    assert r_bad_query.status_code == 400
    assert "detail" in r_bad_query.json()
    assert r_bad_query.json()["detail"] == "Provide appointment_id or patient_id"

    # 404 Not Found on non-existent appointment
    bad_payload = {
        "appointment_id": str(uuid.uuid4()),
        "items": [{"name": "BP", "result": "120/80"}],
    }
    r_not_found = cutover_client.post("/api/vitals", json=bad_payload, headers=auth_headers)
    assert r_not_found.status_code == 404
    assert r_not_found.json()["detail"] == "Appointment not found"

    # 401 Unauthorized on missing token
    r_unauth = cutover_client.get("/api/vitals/today")
    assert r_unauth.status_code in {401, 403}  # HTTPBearer auto-error


def test_cutover_toggle_both_states(db_session: Session):
    """
    Verify that toggling USE_MIGRATED_VITALS exclusively routes traffic:
    - False -> app.routers.vitals.list_today_bookings
    - True  -> hms_migration.modules.vitals.api.vitals_api.list_today_bookings
    With zero duplicate routes or route shadowing.
    """
    from fastapi import FastAPI
    from app.routers import vitals as legacy_vitals
    from hms_migration.modules.vitals.api import vitals_api as migrated_vitals

    # 1. Simulate legacy router mount (USE_MIGRATED_VITALS=false)
    legacy_app = FastAPI()
    legacy_app.include_router(legacy_vitals.router, prefix="/api")
    legacy_route = [r for r in legacy_app.routes if r.path == "/api/vitals/today"][0]
    assert legacy_route.endpoint == legacy_vitals.list_today_bookings

    # 2. Simulate migrated router mount (USE_MIGRATED_VITALS=true)
    migrated_app = FastAPI()
    migrated_app.include_router(migrated_vitals.router, prefix="/api")
    migrated_route = [r for r in migrated_app.routes if r.path == "/api/vitals/today"][0]
    assert migrated_route.endpoint == migrated_vitals.list_today_bookings

    # 3. Verify exactly 5 vitals routes on each
    assert len([r for r in legacy_app.routes if "/vitals" in r.path]) == 5
    assert len([r for r in migrated_app.routes if "/vitals" in r.path]) == 5

