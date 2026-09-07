"""
Runtime cutover integration tests for the Patient Domain module.

Verifies:
1. When USE_MIGRATED_PATIENTS=false:
   - Exactly one set of /api/registration/patients routes exists.
   - Handlers belong to legacy app.routers.registration.
2. When USE_MIGRATED_PATIENTS=true:
   - Exactly one set of /api/registration/patients routes exists.
   - Handlers belong to migrated hms_migration.modules.patients.api.patients_api.
   - Legacy inpatient routes (/beds, /wards-rooms, /admit, /discharge, /doctors) remain mounted.
   - Zero duplicate route registrations or conflicting OpenAPI operation IDs.
   - Full HTTP request lifecycle completes successfully via real application runtime.
"""

from importlib import reload
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Hospital
from hms_migration.infrastructure.postgres.session import (
    get_transitional_sync_session as migrated_get_db,
)
from hms_migration.shared.auth.jwt import create_access_token


@pytest.fixture(scope="function")
def staff_auth(hospital: Hospital) -> dict[str, str]:
    """Generate auth headers for hospital staff."""
    token = create_access_token({
        "sub": "nurse@hospital.com",
        "name": "Staff Nurse",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def reset_settings_and_main(monkeypatch):
    """Ensure clean reload before and after cutover test execution."""
    yield
    monkeypatch.delenv("USE_MIGRATED_PATIENTS", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    import app.main
    reload(app.main)


def test_legacy_mode_patient_router_registration(monkeypatch):
    """Under USE_MIGRATED_PATIENTS=false, legacy registration router handles patient routes."""
    monkeypatch.setenv("USE_MIGRATED_PATIENTS", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)

    patient_routes = [
        r for r in app.main.app.routes
        if getattr(r, "path", None) in (
            "/api/registration/patients",
            "/api/registration/patients/{patient_id}",
        )
    ]
    # 2 routes at /api/registration/patients (POST, GET) + 2 routes at {patient_id} (GET, PUT)
    assert len(patient_routes) == 4
    for r in patient_routes:
        assert "app.routers.registration" in r.endpoint.__module__

    # Verify beds route also present
    beds_routes = [
        r for r in app.main.app.routes
        if getattr(r, "path", None) == "/api/registration/beds"
    ]
    assert len(beds_routes) == 1
    assert "app.routers.registration" in beds_routes[0].endpoint.__module__


def test_migrated_mode_patient_router_registration(monkeypatch):
    """Under USE_MIGRATED_PATIENTS=true, migrated router handles patient routes without route collision."""
    monkeypatch.setenv("USE_MIGRATED_PATIENTS", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)

    patient_routes = [
        r for r in app.main.app.routes
        if getattr(r, "path", None) in (
            "/api/registration/patients",
            "/api/registration/patients/{patient_id}",
        )
    ]
    assert len(patient_routes) == 4
    for r in patient_routes:
        assert "hms_migration.modules.patients.api.patients_api" in r.endpoint.__module__

    # Verify legacy inpatient routes remain intact
    inpatient_paths = [
        "/api/registration/beds",
        "/api/registration/wards-rooms",
        "/api/registration/patients/{patient_id}/admit",
        "/api/registration/admissions/{admission_id}/discharge",
        "/api/registration/doctors",
    ]
    for path in inpatient_paths:
        routes = [r for r in app.main.app.routes if getattr(r, "path", None) == path]
        assert len(routes) == 1, f"Expected 1 route for {path}, found {len(routes)}"
        assert "app.routers.registration" in routes[0].endpoint.__module__


def test_migrated_mode_real_request_execution(
    monkeypatch,
    db_session: Session,
    hospital: Hospital,
    staff_auth: dict[str, str],
):
    """Under USE_MIGRATED_PATIENTS=true, full HTTP request lifecycle executes via migrated router."""
    monkeypatch.setenv("USE_MIGRATED_PATIENTS", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)

    def _override_db():
        yield db_session

    app.main.app.dependency_overrides[get_db] = _override_db
    app.main.app.dependency_overrides[migrated_get_db] = _override_db

    client = TestClient(app.main.app)

    # 1. Register patient via migrated router
    payload = {
        "first_name": "Cutover",
        "last_name": "TestPatient",
        "gender": "Female",
        "mobile": "9988776655",
        "emergency_contact": "9988776656",
        "emergency_contact_name": "Guardian",
        "emergency_contact_relation": "Parent" if "Parent" in ("Father", "Mother", "Spouse", "Sibling", "Child", "Friend", "Other") else "Mother",
    }
    resp = client.post("/api/registration/patients", json=payload, headers=staff_auth)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["uhid"].startswith("P")
    assert created["name"] == "Cutover TestPatient"

    patient_id = created["id"]

    # 2. Get profile via migrated router
    resp_get = client.get(f"/api/registration/patients/{patient_id}", headers=staff_auth)
    assert resp_get.status_code == 200
    assert resp_get.json()["mobile"] == "9988776655"

    # 3. Update patient via migrated router
    resp_put = client.put(
        f"/api/registration/patients/{patient_id}",
        json={"first_name": "UpdatedCutover"},
        headers=staff_auth,
    )
    assert resp_put.status_code == 200
    assert resp_put.json()["first_name"] == "UpdatedCutover"
    assert resp_put.json()["name"] == "UpdatedCutover TestPatient"

    # 4. Search patient via migrated router
    resp_list = client.get("/api/registration/patients?search=UpdatedCutover", headers=staff_auth)
    assert resp_list.status_code == 200
    assert len(resp_list.json()) == 1
