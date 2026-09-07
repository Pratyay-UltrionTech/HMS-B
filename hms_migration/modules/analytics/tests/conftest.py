"""
Test fixtures for the migrated Analytics module.

Reuses the defensive SQLite test harness and deterministic fixtures
established in Phase 1 without creating any external network calls.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db as legacy_get_db
from hms_migration.infrastructure.postgres.session import (
    get_transitional_sync_session as migrated_get_db,
)
from hms_migration.modules.analytics.api.analytics_api import router as analytics_router
from hms_migration.shared.exceptions import register_exception_handlers
from tests.conftest import (
    db_session,
    doctor,
    hospital,
    patient,
    super_admin_headers,
)


@pytest.fixture(scope="function")
def migrated_analytics_client(db_session: Session) -> TestClient:
    """FastAPI TestClient mounting the migrated analytics router."""
    test_app = FastAPI(title="Migrated Analytics Test App")
    register_exception_handlers(test_app)
    test_app.include_router(analytics_router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[legacy_get_db] = _override_get_db
    test_app.dependency_overrides[migrated_get_db] = _override_get_db
    return TestClient(test_app)
