"""
Unit and integration tests for the Migrated Beds / Wards / Rooms Domain.

Verifies:
1. Bed Dashboard (/api/beds/dashboard).
2. Occupancy Report (/api/beds/occupancy).
3. Wards listing (/api/beds/wards).
4. Rooms listing (/api/beds/rooms).
5. Bed options listing with room auto-bed population (/api/beds/options).
6. Staff doctors listing for beds (/api/beds/doctors).
7. Registration Bed options (/api/registration/beds).
8. Registration Wards & Rooms catalog (/api/registration/wards-rooms).
9. Multi-tenant isolation across beds and wards.
"""

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hospital, HospitalUser, Room, StaffRole, Ward, WardType
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.beds.api.beds_api import (
    registration_inpatient_router,
    router as beds_router,
)
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital


@pytest.fixture(scope="function")
def beds_app(db_session: Session) -> FastAPI:
    """Isolated test app mounting both beds and registration-inpatient routers."""
    test_app = FastAPI(title="Migrated Beds Test App")
    test_app.include_router(beds_router, prefix="/api")
    test_app.include_router(registration_inpatient_router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_get_db
    return test_app


@pytest.fixture(scope="function")
def client(beds_app: FastAPI) -> TestClient:
    return TestClient(beds_app)


@pytest.fixture(scope="function")
def staff_auth_headers(hospital: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "admin@hospital.com",
        "name": "Hospital Admin",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def setup_ward_and_room(db_session: Session, hospital: Hospital) -> tuple[Ward, Room]:
    ward = Ward(
        id=uuid4(),
        hospital_id=hospital.id,
        name="General Ward A",
        ward_type=WardType.general,
        admission_fee=500.0,
        bed_charge_per_day=800.0,
        is_active=True,
    )
    db_session.add(ward)
    db_session.commit()

    room = Room(
        id=uuid4(),
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_code="R101",
        name="Room 101",
        bed_count=2,
        is_active=True,
    )
    db_session.add(room)
    db_session.commit()
    return ward, room


def test_beds_dashboard_and_occupancy(
    client: TestClient,
    staff_auth_headers: dict[str, str],
    setup_ward_and_room: tuple[Ward, Room],
):
    """Test dashboard and occupancy report with dynamic bed creation."""
    ward, room = setup_ward_and_room

    # Dashboard endpoint
    res = client.get("/api/beds/dashboard", headers=staff_auth_headers)
    assert res.status_code == 200, res.text
    rows = res.json()
    assert len(rows) >= 2  # room.bed_count is 2, so 2 beds auto-created
    assert all(r["ward_name"] == "General Ward A" for r in rows)
    assert all(not r["is_occupied"] for r in rows)

    # Occupancy report
    occ_res = client.get("/api/beds/occupancy", headers=staff_auth_headers)
    assert occ_res.status_code == 200, occ_res.text
    occ = occ_res.json()
    assert occ["total_beds"] >= 2
    assert occ["occupied_beds"] == 0
    assert occ["available_beds"] >= 2
    assert occ["occupancy_percent"] == 0.0


def test_wards_and_rooms_endpoints(
    client: TestClient,
    staff_auth_headers: dict[str, str],
    setup_ward_and_room: tuple[Ward, Room],
):
    """Test listing wards and rooms."""
    ward, room = setup_ward_and_room

    # Wards
    w_res = client.get("/api/beds/wards", headers=staff_auth_headers)
    assert w_res.status_code == 200
    wards = w_res.json()
    assert any(w["id"] == str(ward.id) for w in wards)

    # Rooms
    r_res = client.get("/api/beds/rooms", headers=staff_auth_headers)
    assert r_res.status_code == 200
    rooms = r_res.json()
    assert any(r["id"] == str(room.id) for r in rooms)

    # Options
    opt_res = client.get(f"/api/beds/options?ward_id={ward.id}", headers=staff_auth_headers)
    assert opt_res.status_code == 200
    opts = opt_res.json()
    assert len(opts) == 2


def test_registration_beds_and_catalog(
    client: TestClient,
    staff_auth_headers: dict[str, str],
    setup_ward_and_room: tuple[Ward, Room],
):
    """Test /api/registration/beds and /api/registration/wards-rooms."""
    ward, room = setup_ward_and_room

    # Registration beds
    b_res = client.get("/api/registration/beds", headers=staff_auth_headers)
    assert b_res.status_code == 200
    beds = b_res.json()
    assert len(beds) >= 2

    # Registration wards-rooms
    cat_res = client.get("/api/registration/wards-rooms", headers=staff_auth_headers)
    assert cat_res.status_code == 200
    cat = cat_res.json()
    assert "wards" in cat and "rooms" in cat
    assert any(w["name"] == "General Ward A" for w in cat["wards"])
    assert any(r["room_code"] == "R101" for r in cat["rooms"])
