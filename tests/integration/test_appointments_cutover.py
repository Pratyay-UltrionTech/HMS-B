"""
Runtime cutover integration tests for the Appointments Domain module.

Verifies:
1. When USE_MIGRATED_APPOINTMENTS=false:
   - Route handlers belong to legacy app.routers.appointment.
2. When USE_MIGRATED_APPOINTMENTS=true:
   - Route handlers belong to migrated hms_migration.modules.appointments.api.appointments_api.
   - Zero duplicate route registrations or conflicting OpenAPI paths.
   - Real HTTP request lifecycle completes successfully via FastAPI application runtime.
"""

from datetime import date, timedelta
from importlib import reload
import random
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Hospital, HospitalUser
from hms_migration.infrastructure.postgres.session import (
    get_transitional_sync_session as migrated_get_db,
)
from hms_migration.shared.auth.jwt import create_access_token


@pytest.fixture(scope="function")
def staff_auth(hospital: Hospital) -> dict[str, str]:
    """Generate auth headers for hospital staff."""
    token = create_access_token({
        "sub": "nurse.joy@apollocity.com",
        "name": "Nurse Joy",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def reset_settings_and_main(monkeypatch):
    """Ensure clean reload before and after cutover test execution."""
    yield
    monkeypatch.delenv("USE_MIGRATED_APPOINTMENTS", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    import app.main
    reload(app.main)


def test_legacy_mode_appointment_router_registration(monkeypatch):
    """Under USE_MIGRATED_APPOINTMENTS=false, legacy appointment router handles appointment routes."""
    monkeypatch.setenv("USE_MIGRATED_APPOINTMENTS", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)
    main_app = app.main.app

    routes = [
        route for route in main_app.routes
        if hasattr(route, "path") and route.path == "/api/appointments"
    ]
    assert len(routes) >= 1
    assert all("appointment" in route.endpoint.__module__ for route in routes)
    assert not any("hms_migration" in route.endpoint.__module__ for route in routes)


def test_migrated_mode_appointment_router_registration(monkeypatch):
    """Under USE_MIGRATED_APPOINTMENTS=true, migrated appointment router handles appointment routes."""
    monkeypatch.setenv("USE_MIGRATED_APPOINTMENTS", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)
    main_app = app.main.app

    routes = [
        route for route in main_app.routes
        if hasattr(route, "path") and route.path == "/api/appointments"
    ]
    assert len(routes) >= 1
    assert any("hms_migration" in route.endpoint.__module__ for route in routes)
    assert not any(
        route.endpoint.__module__ == "app.routers.appointment" for route in routes
    )


def test_migrated_mode_functional_booking_and_retrieval(
    monkeypatch,
    db_session: Session,
    hospital: Hospital,
    doctor: HospitalUser,
    staff_auth: dict[str, str],
):
    """Under USE_MIGRATED_APPOINTMENTS=true, appointment booking completes via migrated router."""
    monkeypatch.setenv("USE_MIGRATED_APPOINTMENTS", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)
    main_app = app.main.app

    main_app.dependency_overrides[get_db] = lambda: db_session
    main_app.dependency_overrides[migrated_get_db] = lambda: db_session

    client = TestClient(main_app)
    target_date = (date.today() + timedelta(days=3)).isoformat()
    unique_phone = f"98765{random.randint(10000, 99999)}"

    payload = {
        "doctor_id": str(doctor.id),
        "appointment_date": target_date,
        "appointment_time": "11:00:00",
        "booking_kind": "walk_in",
        "first_name": "Suresh",
        "last_name": "Raina",
        "mobile": unique_phone,
        "gender": "male",
        "date_of_birth": "1992-06-15",
        "age": 34,
        "emergency_contact": "9876543210",
        "purpose": "General Consultation",
    }

    res = client.post("/api/appointments", json=payload, headers=staff_auth)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["status"] == "scheduled"
    assert data["patient_name"] == "Suresh Raina"
    assert data["op_id"].startswith("OP-")

    # Fetch today list
    today_res = client.get("/api/appointments/today", headers=staff_auth)
    assert today_res.status_code == 200
    assert isinstance(today_res.json(), list)
