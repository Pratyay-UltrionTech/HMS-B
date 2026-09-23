"""
Test fixtures for the migrated Patient module.

Mounts the migrated router (modules.patients.api.patients_api.router)
onto an isolated FastAPI application, reusing the defensive SQLite test harness
and Azure isolation guarantees established in Phase 1.
Registers shared exception handlers to guarantee identical error responses.
"""

from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from infrastructure.postgres.session import (
    get_transitional_sync_session,
    get_transitional_sync_session as get_db,
)

from modules.patients.api.patients_api import router as migrated_patients_router
from modules.tenancy.entities.hospital import Hospital
from shared.exceptions import register_exception_handlers



# Re-use isolated SQLite database session and deterministic model fixtures
from tests.conftest import (
    admin_headers,
    auth_headers,
    auth_headers_b,
    db_session,
    doctor,
    doctor_b,
    hospital,
    hospital_b,
)

