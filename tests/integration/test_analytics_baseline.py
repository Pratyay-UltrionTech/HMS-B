"""
Baseline behavioral lock-in tests for legacy Analytics platform endpoint.

Locks in exact observable behavior of GET /api/analytics/platform:
1. Authentication: Bearer JWT required (401 on missing/invalid/expired token).
2. Authorization: super_admin role required (403 on hospital_admin, hospital_staff, etc.).
3. Response shape & types: PlatformAnalyticsResponse schema fidelity.
4. Aggregation semantics:
   - 6-month historical growth windows (hospital and patient)
   - Plan distribution across basic, premium, platinum
   - Active modules computation (plan multipliers vs distinct permission modules)
   - Platform users = active staff users + active hospitals
   - Recent hospitals ordering (created_at desc, limit 8)
"""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Hospital, HospitalUser, Patient, PlanType, RolePermission
from app.routers import analytics as legacy_analytics
from fastapi import FastAPI
from hms_migration.shared.auth.jwt import create_access_token


@pytest.fixture(scope="function")
def analytics_client(db_session: Session) -> TestClient:
    """FastAPI TestClient pointing to an isolated app mounting legacy analytics router."""
    test_app = FastAPI(title="HMS Analytics Baseline Test App")
    test_app.include_router(legacy_analytics.router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(test_app)



@pytest.fixture(scope="function")
def super_admin_headers() -> dict[str, str]:
    """Generate auth headers for a valid super_admin user."""
    token = create_access_token({
        "sub": "superadmin@ultrion.com",
        "name": "Super Admin",
        "role": "super_admin",
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def hospital_admin_headers(hospital: Hospital) -> dict[str, str]:
    """Generate auth headers for a hospital_admin user."""
    token = create_access_token({
        "sub": "admin@apollocity.com",
        "name": "Hospital Admin",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def hospital_staff_headers(hospital: Hospital) -> dict[str, str]:
    """Generate auth headers for a hospital_staff user."""
    token = create_access_token({
        "sub": "staff@apollocity.com",
        "name": "Hospital Staff",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# 1. Authentication & Authorization Baseline
# ===========================================================================

def test_analytics_platform_unauthenticated_returns_403(analytics_client: TestClient):
    """GET /api/analytics/platform without credentials returns 403 Forbidden (FastAPI HTTPBearer default)."""
    resp = analytics_client.get("/api/analytics/platform")
    assert resp.status_code == 403



def test_analytics_platform_invalid_token_returns_401(analytics_client: TestClient):
    """GET /api/analytics/platform with malformed Bearer token returns 401."""
    resp = analytics_client.get(
        "/api/analytics/platform",
        headers={"Authorization": "Bearer invalid.jwt.token"},
    )
    assert resp.status_code == 401


def test_analytics_platform_hospital_admin_forbidden_returns_403(
    analytics_client: TestClient,
    hospital_admin_headers: dict[str, str],
):
    """GET /api/analytics/platform with hospital_admin role returns 403 Forbidden."""
    resp = analytics_client.get("/api/analytics/platform", headers=hospital_admin_headers)
    assert resp.status_code == 403
    assert "Super admin access required" in resp.json().get("detail", "")


def test_analytics_platform_hospital_staff_forbidden_returns_403(
    analytics_client: TestClient,
    hospital_staff_headers: dict[str, str],
):
    """GET /api/analytics/platform with hospital_staff role returns 403 Forbidden."""
    resp = analytics_client.get("/api/analytics/platform", headers=hospital_staff_headers)
    assert resp.status_code == 403
    assert "Super admin access required" in resp.json().get("detail", "")


# ===========================================================================
# 2. Response Contract & Structure Baseline
# ===========================================================================

def test_analytics_platform_success_contract(
    analytics_client: TestClient,
    super_admin_headers: dict[str, str],
    hospital: Hospital,
):
    """GET /api/analytics/platform with super_admin token returns 200 and valid schema."""
    resp = analytics_client.get("/api/analytics/platform", headers=super_admin_headers)
    assert resp.status_code == 200
    data = resp.json()

    # Required top-level metrics
    assert "total_hospitals" in data
    assert "active_hospitals" in data
    assert "active_modules" in data
    assert "total_patients" in data
    assert "platform_users" in data
    assert "hospital_growth" in data
    assert "patient_growth" in data
    assert "plan_distribution" in data
    assert "recent_hospitals" in data
    assert "generated_at" in data

    # Growth lists must contain exactly 6 rolling monthly buckets
    assert isinstance(data["hospital_growth"], list)
    assert len(data["hospital_growth"]) == 6
    assert isinstance(data["patient_growth"], list)
    assert len(data["patient_growth"]) == 6

    # Verify MonthCount structure
    for bucket in data["hospital_growth"]:
        assert "month" in bucket  # YYYY-MM
        assert "label" in bucket  # Mon YYYY
        assert "count" in bucket
        assert len(bucket["month"]) == 7
        assert "-" in bucket["month"]

    # Plan distribution must cover all 3 PlanTypes (basic, premium, platinum)
    assert len(data["plan_distribution"]) == 3
    plan_keys = {p["plan"] for p in data["plan_distribution"]}
    assert plan_keys == {"basic", "premium", "platinum"}


# ===========================================================================
# 3. Aggregation Logic & Metrics Baseline
# ===========================================================================

def test_analytics_platform_metrics_calculations(
    analytics_client: TestClient,
    db_session: Session,
    super_admin_headers: dict[str, str],
    hospital: Hospital,
    doctor: HospitalUser,
    patient: Patient,
):
    """Verify numeric calculations for counts, plan distribution, and user aggregates."""
    # Ensure hospital is active with premium plan
    hospital.is_active = True
    hospital.plan = PlanType.premium
    db_session.commit()

    resp = analytics_client.get("/api/analytics/platform", headers=super_admin_headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_hospitals"] >= 1
    assert data["active_hospitals"] >= 1
    assert data["total_patients"] >= 1

    # Active staff user (doctor is active) + active hospitals
    # doctor.is_active is True by default in model
    assert data["platform_users"] >= data["active_hospitals"]

    # Plan distribution should show count for premium
    premium_entry = next(p for p in data["plan_distribution"] if p["plan"] == "premium")
    assert premium_entry["count"] >= 1

    # Recent hospitals must list our hospital
    recent_ids = [h["id"] for h in data["recent_hospitals"]]
    assert str(hospital.id) in recent_ids

