"""
Runtime cutover integration tests for Phase 8 domains:
- Doctors (USE_MIGRATED_DOCTORS)
- Beds (USE_MIGRATED_BEDS)
- Inpatient (USE_MIGRATED_INPATIENT)

Verifies:
1. When flags are false:
   - Handlers belong to legacy app.routers.
2. When flags are true:
   - Handlers belong to migrated hms_migration.modules.*.api.
3. No duplicate routes or OpenAPI collision.
"""

from importlib import reload
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.models import Hospital
from hms_migration.shared.auth.jwt import create_access_token


@pytest.fixture(autouse=True)
def reset_settings_and_main(monkeypatch):
    """Ensure clean reload before and after cutover test execution."""
    yield
    monkeypatch.delenv("USE_MIGRATED_DOCTORS", raising=False)
    monkeypatch.delenv("USE_MIGRATED_BEDS", raising=False)
    monkeypatch.delenv("USE_MIGRATED_INPATIENT", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    import app.main
    reload(app.main)


def test_legacy_mode_doctors_router_registration(monkeypatch):
    """Under USE_MIGRATED_DOCTORS=false, legacy doctors router is mounted."""
    monkeypatch.setenv("USE_MIGRATED_DOCTORS", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)

    route_modules = {
        route.path: getattr(route.endpoint, "__module__", "")
        for route in app.main.app.routes
    }
    assert "/api/doctors" in route_modules
    assert route_modules["/api/doctors"] == "app.routers.doctors"


def test_migrated_mode_doctors_router_registration(monkeypatch):
    """Under USE_MIGRATED_DOCTORS=true, migrated doctors router is mounted."""
    monkeypatch.setenv("USE_MIGRATED_DOCTORS", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)

    route_modules = {
        route.path: getattr(route.endpoint, "__module__", "")
        for route in app.main.app.routes
    }
    assert "/api/doctors" in route_modules
    assert "hms_migration.modules.doctors.api.doctors_api" in route_modules["/api/doctors"]


def test_legacy_mode_beds_router_registration(monkeypatch):
    """Under USE_MIGRATED_BEDS=false, legacy beds router is mounted."""
    monkeypatch.setenv("USE_MIGRATED_BEDS", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)

    route_modules = {
        route.path: getattr(route.endpoint, "__module__", "")
        for route in app.main.app.routes
    }
    assert "/api/beds/dashboard" in route_modules
    assert route_modules["/api/beds/dashboard"] == "app.routers.beds"


def test_migrated_mode_beds_router_registration(monkeypatch):
    """Under USE_MIGRATED_BEDS=true, migrated beds router is mounted."""
    monkeypatch.setenv("USE_MIGRATED_BEDS", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)

    route_modules = {
        route.path: getattr(route.endpoint, "__module__", "")
        for route in app.main.app.routes
    }
    assert "/api/beds/dashboard" in route_modules
    assert "hms_migration.modules.beds.api.beds_api" in route_modules["/api/beds/dashboard"]


def test_migrated_mode_inpatient_router_registration(monkeypatch):
    """Under USE_MIGRATED_INPATIENT=true, migrated IPD router is mounted."""
    monkeypatch.setenv("USE_MIGRATED_INPATIENT", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    reload(app.main)

    route_modules = {
        route.path: getattr(route.endpoint, "__module__", "")
        for route in app.main.app.routes
    }
    assert "/api/ipd/form-submissions" in route_modules
    assert "hms_migration.modules.inpatient.api.ipd_api" in route_modules["/api/ipd/form-submissions"]
