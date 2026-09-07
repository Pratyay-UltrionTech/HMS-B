"""
Runtime cutover integration tests for the Analytics module.

Verifies:
1. When USE_MIGRATED_ANALYTICS=false:
   - Exactly one /api/analytics/platform route exists.
   - Route handler belongs to legacy app.routers.analytics.
2. When USE_MIGRATED_ANALYTICS=true:
   - Exactly one /api/analytics/platform route exists.
   - Route handler belongs to migrated hms_migration.modules.analytics.api.analytics_api.
   - Full HTTP request lifecycle completes successfully with identical response shape.
3. Zero duplicate route registrations or conflicting OpenAPI operation IDs.
"""

from typing import Any
import uuid

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
def super_admin_auth() -> dict[str, str]:
    """Generate auth headers for super_admin."""
    token = create_access_token({
        "sub": "superadmin@ultrion.com",
        "name": "Super Admin",
        "role": "super_admin",
    })
    return {"Authorization": f"Bearer {token}"}


def test_legacy_mode_router_registration(monkeypatch):
    """Under USE_MIGRATED_ANALYTICS=false, legacy analytics router is active."""
    monkeypatch.setenv("USE_MIGRATED_ANALYTICS", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    # Create fresh app instance to evaluate router mount branch
    import app.main
    from importlib import reload
    reload(app.main)

    routes = [
        r for r in app.main.app.routes
        if getattr(r, "path", None) == "/api/analytics/platform"
    ]
    assert len(routes) == 1
    assert "app.routers.analytics" in routes[0].endpoint.__module__


def test_migrated_mode_router_registration_and_execution(
    monkeypatch,
    db_session: Session,
    hospital: Hospital,
    super_admin_auth: dict[str, str],
):
    """Under USE_MIGRATED_ANALYTICS=true, migrated analytics router is active and executes successfully."""
    monkeypatch.setenv("USE_MIGRATED_ANALYTICS", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    from importlib import reload
    reload(app.main)

    routes = [
        r for r in app.main.app.routes
        if getattr(r, "path", None) == "/api/analytics/platform"
    ]
    assert len(routes) == 1
    assert "hms_migration.modules.analytics.api.analytics_api" in routes[0].endpoint.__module__

    # Execute against running app
    app.main.app.dependency_overrides[get_db] = lambda: db_session
    app.main.app.dependency_overrides[migrated_get_db] = lambda: db_session

    client = TestClient(app.main.app)
    resp = client.get("/api/analytics/platform", headers=super_admin_auth)
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_hospitals"] >= 1
    assert len(data["hospital_growth"]) == 6
    assert len(data["patient_growth"]) == 6
    assert len(data["plan_distribution"]) == 3

    app.main.app.dependency_overrides.clear()


def test_no_duplicate_operation_ids_in_migrated_mode(monkeypatch):
    """Verify OpenAPI schema has zero duplicate operation IDs when migrated mode is active."""
    monkeypatch.setenv("USE_MIGRATED_ANALYTICS", "true")
    monkeypatch.setenv("USE_MIGRATED_VITALS", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    import app.main
    from importlib import reload
    reload(app.main)

    openapi = app.main.app.openapi()
    operation_ids: list[str] = []
    for path, methods in openapi.get("paths", {}).items():
        for method, spec in methods.items():
            if isinstance(spec, dict) and "operationId" in spec:
                operation_ids.append(spec["operationId"])

    duplicates = [op for op in set(operation_ids) if operation_ids.count(op) > 1]
    assert len(duplicates) == 0, f"Duplicate operation IDs found: {duplicates}"
