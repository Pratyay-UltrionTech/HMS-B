"""
Module-level tests for the migrated Analytics slice.

Verifies:
1. Repository data access queries against SQLite and PostgreSQL schemas.
2. Action business logic for plan distribution, 6-month rolling growth, and active modules.
3. API controller endpoints, super_admin security enforcement, and response serialization.
"""

from datetime import datetime, timezone
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hospital, HospitalUser, Patient, PlanType
from hms_migration.infrastructure.postgres.session import (
    get_transitional_sync_session as get_db,
)
from hms_migration.modules.analytics.actions.get_platform_analytics_action import (
    GetPlatformAnalyticsAction,
)
from hms_migration.modules.analytics.api.analytics_api import router as analytics_router
from hms_migration.modules.analytics.db.analytics_repository import AnalyticsRepository
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


@pytest.fixture(scope="function")
def staff_auth(hospital: Hospital) -> dict[str, str]:
    """Generate auth headers for hospital_staff."""
    token = create_access_token({
        "sub": "nurse@apollocity.com",
        "name": "Nurse",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# 1. Repository Tests
# ===========================================================================

def test_repository_get_all_hospitals(db_session: Session, hospital: Hospital):
    """Verify repository lists hospital records with correct dict mappings."""
    repo = AnalyticsRepository(db_session)
    hospitals = repo.get_all_hospitals()
    assert len(hospitals) >= 1
    found = next(h for h in hospitals if str(h["id"]) == str(hospital.id))
    assert found["name"] == "Apollo City Hospital"
    assert found["is_active"] is True


def test_repository_counts(
    db_session: Session,
    hospital: Hospital,
    doctor: HospitalUser,
    patient: Patient,
):
    """Verify repository user and patient counts."""
    repo = AnalyticsRepository(db_session)
    staff_count = repo.get_active_staff_users_count()
    patient_count = repo.get_total_patients_count()
    assert staff_count >= 1
    assert patient_count >= 1


def test_repository_recent_hospitals_limit(db_session: Session, hospital: Hospital):
    """Verify repository limits recent hospitals to requested limit."""
    repo = AnalyticsRepository(db_session)
    recent = repo.get_recent_hospitals(limit=5)
    assert len(recent) <= 5
    assert any(str(h["id"]) == str(hospital.id) for h in recent)


# ===========================================================================
# 2. Action Logic Tests
# ===========================================================================

def test_action_execute_aggregations(
    db_session: Session,
    hospital: Hospital,
    doctor: HospitalUser,
    patient: Patient,
):
    """Verify GetPlatformAnalyticsAction aggregates correctly."""
    repo = AnalyticsRepository(db_session)
    action = GetPlatformAnalyticsAction(repo)
    result = action.execute()

    assert result.total_hospitals >= 1
    assert result.active_hospitals >= 1
    assert result.total_patients >= 1
    assert result.platform_users >= 1
    assert len(result.hospital_growth) == 6
    assert len(result.patient_growth) == 6
    assert len(result.plan_distribution) == 3


# ===========================================================================
# 3. Migrated API Endpoint Tests
# ===========================================================================

def test_migrated_api_unauthenticated_forbidden(migrated_analytics_client: TestClient):
    """GET /api/analytics/platform without credentials returns 403 (HTTPBearer default)."""
    resp = migrated_analytics_client.get("/api/analytics/platform")
    assert resp.status_code == 403


def test_migrated_api_staff_forbidden(
    migrated_analytics_client: TestClient,
    staff_auth: dict[str, str],
):
    """GET /api/analytics/platform with hospital_staff returns 403."""
    resp = migrated_analytics_client.get("/api/analytics/platform", headers=staff_auth)
    assert resp.status_code == 403
    assert "Super admin access required" in resp.json().get("detail", "")


def test_migrated_api_success(
    migrated_analytics_client: TestClient,
    super_admin_auth: dict[str, str],
    hospital: Hospital,
):
    """GET /api/analytics/platform with super_admin returns 200 and schema."""
    resp = migrated_analytics_client.get("/api/analytics/platform", headers=super_admin_auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_hospitals"] >= 1
    assert len(data["hospital_growth"]) == 6
    assert len(data["patient_growth"]) == 6
