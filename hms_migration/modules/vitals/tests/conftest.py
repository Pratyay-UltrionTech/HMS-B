"""
Test fixtures for the migrated Vitals module.

Mounts the migrated router (hms_migration.modules.vitals.api.vitals_api.router)
onto an isolated FastAPI application, reusing the defensive SQLite test harness
and Azure isolation guarantees established in Phase 1.
Registers shared exception handlers to guarantee identical error responses.
"""

from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db as legacy_get_db
from hms_migration.infrastructure.postgres import get_transitional_sync_session
from hms_migration.modules.vitals.api.vitals_api import router as migrated_vitals_router
from hms_migration.shared.exceptions import register_exception_handlers

# Re-use isolated SQLite database session and deterministic model fixtures
from tests.conftest import (
    admin_headers,
    appointment_factory,
    auth_headers,
    auth_headers_b,
    db_session,
    doctor,
    doctor_b,
    hospital,
    hospital_b,
    patient,
    patient_b,
    super_admin_headers,
    vital_factory,
)


@pytest.fixture(scope="function")
def app(db_session: Session) -> FastAPI:
    """
    Constructs an isolated FastAPI application mounting the MIGRATED vitals router.
    Registers shared exception handlers and overrides DB session with isolated SQLite session.
    """
    test_app = FastAPI(title="HMS Migrated Vitals Test App")
    register_exception_handlers(test_app)
    test_app.include_router(migrated_vitals_router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[legacy_get_db] = _override_get_db
    test_app.dependency_overrides[get_transitional_sync_session] = _override_get_db
    return test_app


@pytest.fixture(scope="function")
def client(app: FastAPI) -> TestClient:
    """FastAPI TestClient pointing to the migrated vitals router."""
    return TestClient(app)
