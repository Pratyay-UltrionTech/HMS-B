"""
Unit and integration tests for the CSSD (Central Sterile Services Department) domain.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.cssd.api.cssd_api import router as cssd_router
from hms_migration.modules.tenancy.entities.hospital import Hospital
from hms_migration.shared.auth import get_hospital_context, require_hospital_user


@pytest.fixture
def cssd_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def _make_client(db, hospital_id=None):
    app = FastAPI()
    app.include_router(cssd_router, prefix="/api")

    hospital_id = hospital_id or uuid4()
    hospital = Hospital(
        id=hospital_id,
        hospital_id=f"HOSP{hospital_id.hex[:6].upper()}",
        name="CSSD Test Hospital",
        address="123 Health St",
        phone="9876543210",
        email=f"admin_{hospital_id.hex[:6]}@cssdhosp.org",
        password_hash="hash",
        is_active=True,
    )
    db.add(hospital)
    db.commit()

    current_user = {
        "id": str(uuid4()),
        "sub": "cssd_admin@example.com",
        "email": "cssd_admin@example.com",
        "role": "admin",
        "name": "CSSD Admin",
        "hospital_id": str(hospital_id),
    }

    def override_session():
        yield db

    def override_auth():
        return current_user

    def override_hospital():
        return hospital_id

    app.dependency_overrides[get_transitional_sync_session] = override_session
    app.dependency_overrides[require_hospital_user] = override_auth
    app.dependency_overrides[get_hospital_context] = override_hospital

    return TestClient(app), hospital_id, current_user


@pytest.fixture
def cssd_client(cssd_db):
    return _make_client(cssd_db)


def _create_set(client, code="SET001"):
    res = client.post(
        "/api/cssd/instrument-sets",
        json={
            "set_code": code,
            "set_name": "General Surgery Major Set",
            "owning_department": "General Surgery",
            "description": "Major set for open abdominal surgery",
            "items": [
                {"instrument_name": "Scalpel Handle #4", "quantity": 2},
                {"instrument_name": "Kelly Forceps", "quantity": 4},
            ],
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def _create_batch(client, set_id, method="steam"):
    res = client.post(
        "/api/cssd/sterilization-batches",
        json={
            "sterilization_method": method,
            "machine_id": "AUTOCLAVE-01",
            "operator_staff_id": "STF001",
            "instrument_set_ids": [set_id],
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


# ── Feature 39 ────────────────────────────────────────────────────────────────


def test_instrument_set_creation_with_items(cssd_client):
    client, hospital_id, _ = cssd_client
    iset = _create_set(client)
    assert iset["set_code"] == "SET001"
    assert iset["instrument_count"] == 6
    assert len(iset["items"]) == 2

    # Duplicate code conflicts
    dup = client.post(
        "/api/cssd/instrument-sets",
        json={"set_code": "SET001", "set_name": "Dup"},
    )
    assert dup.status_code == 409

    # Update
    up = client.put(
        f"/api/cssd/instrument-sets/{iset['id']}",
        json={"set_name": "General Surgery Major Set v2"},
    )
    assert up.status_code == 200
    assert up.json()["set_name"] == "General Surgery Major Set v2"


def test_tenant_isolation_for_instrument_sets(cssd_db):
    client_a, hospital_a, _ = _make_client(cssd_db)
    client_b, hospital_b, _ = _make_client(cssd_db, hospital_id=uuid4())

    _create_set(client_a, code="TENANT-SET")

    # Hospital B should not see hospital A's instrument set
    list_b = client_b.get("/api/cssd/instrument-sets")
    assert list_b.status_code == 200
    assert list_b.json() == []

    # Hospital B can reuse the same set_code since uniqueness is scoped per-hospital
    res_b = client_b.post(
        "/api/cssd/instrument-sets",
        json={"set_code": "TENANT-SET", "set_name": "Hospital B Set"},
    )
    assert res_b.status_code == 201

    list_a = client_a.get("/api/cssd/instrument-sets")
    assert len(list_a.json()) == 1
    assert list_a.json()[0]["set_name"] == "General Surgery Major Set"


# ── Feature 40 + 41: happy path completed -> QC pass -> released ────────────


def test_sterilization_batch_happy_path_qc_pass_releases_batch(cssd_client):
    client, hospital_id, _ = cssd_client
    iset = _create_set(client)
    batch = _create_batch(client, iset["id"])
    assert batch["status"] == "in_progress"
    batch_id = batch["id"]

    # Cannot QC a batch that isn't completed yet
    qc_early = client.post(
        f"/api/cssd/sterilization-batches/{batch_id}/qc",
        json={"chemical_indicator_result": "pass", "biological_indicator_result": "pass"},
    )
    assert qc_early.status_code == 400

    complete_res = client.post(
        f"/api/cssd/sterilization-batches/{batch_id}/complete",
        json={"temperature_c": 134.0, "pressure_kpa": 210.0},
    )
    assert complete_res.status_code == 200
    assert complete_res.json()["status"] == "completed"

    qc_res = client.post(
        f"/api/cssd/sterilization-batches/{batch_id}/qc",
        json={
            "chemical_indicator_result": "pass",
            "biological_indicator_result": "pass",
            "bowie_dick_test_result": "pass",
            "checked_by_staff_id": "STF002",
        },
    )
    assert qc_res.status_code == 201
    assert qc_res.json()["overall_result"] == "pass"

    batch_after = client.get(f"/api/cssd/sterilization-batches/{batch_id}").json()
    assert batch_after["status"] == "released"


def test_qc_fail_quarantines_batch_and_blocks_issuing(cssd_client):
    client, hospital_id, _ = cssd_client
    iset = _create_set(client, code="SET-FAIL")
    batch = _create_batch(client, iset["id"])
    batch_id = batch["id"]

    client.post(f"/api/cssd/sterilization-batches/{batch_id}/complete", json={})

    qc_res = client.post(
        f"/api/cssd/sterilization-batches/{batch_id}/qc",
        json={"chemical_indicator_result": "fail", "biological_indicator_result": "fail"},
    )
    assert qc_res.status_code == 201
    assert qc_res.json()["overall_result"] == "fail"

    batch_after = client.get(f"/api/cssd/sterilization-batches/{batch_id}").json()
    assert batch_after["status"] == "quarantined"

    # Issuing from a quarantined batch must be rejected
    issue_res = client.post(
        "/api/cssd/issues",
        json={
            "instrument_set_id": iset["id"],
            "batch_id": batch_id,
            "issued_to_department": "OT-1",
        },
    )
    assert issue_res.status_code == 400


def test_batch_in_progress_to_failed_transition(cssd_client):
    client, hospital_id, _ = cssd_client
    iset = _create_set(client, code="SET-BAD-CYCLE")
    batch = _create_batch(client, iset["id"])
    batch_id = batch["id"]

    fail_res = client.post(f"/api/cssd/sterilization-batches/{batch_id}/fail", json={"remarks": "Power outage"})
    assert fail_res.status_code == 200
    assert fail_res.json()["status"] == "failed"

    # Cannot complete an already-failed batch
    complete_res = client.post(f"/api/cssd/sterilization-batches/{batch_id}/complete", json={})
    assert complete_res.status_code == 400


# ── Feature 42: Issue requires released batch ────────────────────────────────


def test_issue_requires_released_batch(cssd_client):
    client, hospital_id, _ = cssd_client
    iset = _create_set(client, code="SET-ISSUE")
    batch = _create_batch(client, iset["id"])
    batch_id = batch["id"]

    # Not yet completed/released -> reject
    issue_res = client.post(
        "/api/cssd/issues",
        json={"instrument_set_id": iset["id"], "batch_id": batch_id, "issued_to_department": "OT-2"},
    )
    assert issue_res.status_code == 400

    client.post(f"/api/cssd/sterilization-batches/{batch_id}/complete", json={})
    client.post(
        f"/api/cssd/sterilization-batches/{batch_id}/qc",
        json={"chemical_indicator_result": "pass", "biological_indicator_result": "pass"},
    )

    issue_res_ok = client.post(
        "/api/cssd/issues",
        json={
            "instrument_set_id": iset["id"],
            "batch_id": batch_id,
            "issued_to_department": "OT-2",
            "issued_to_staff_id": "NURSE01",
            "expected_return_time": str(datetime.now(timezone.utc) + timedelta(hours=6)),
        },
    )
    assert issue_res_ok.status_code == 201
    issue = issue_res_ok.json()
    assert issue["status"] == "issued"


# ── Feature 42 + 43: return with damage auto-creates discrepancy report ─────


def test_return_with_damage_auto_creates_discrepancy_report(cssd_client):
    client, hospital_id, _ = cssd_client
    iset = _create_set(client, code="SET-DAMAGE")
    batch = _create_batch(client, iset["id"])
    batch_id = batch["id"]
    client.post(f"/api/cssd/sterilization-batches/{batch_id}/complete", json={})
    client.post(
        f"/api/cssd/sterilization-batches/{batch_id}/qc",
        json={"chemical_indicator_result": "pass", "biological_indicator_result": "pass"},
    )
    issue = client.post(
        "/api/cssd/issues",
        json={"instrument_set_id": iset["id"], "batch_id": batch_id, "issued_to_department": "OT-3"},
    ).json()

    return_res = client.post(
        f"/api/cssd/issues/{issue['id']}/return",
        json={
            "returned_by_staff_id": "NURSE02",
            "return_condition": "damaged",
            "discrepancy_instrument_name": "Kelly Forceps",
            "discrepancy_quantity_affected": 1,
            "discrepancy_remarks": "Tip bent during procedure",
        },
    )
    assert return_res.status_code == 200
    ret_data = return_res.json()
    assert ret_data["status"] == "returned"
    assert ret_data["return_condition"] == "damaged"

    reports = client.get("/api/cssd/discrepancy-reports").json()
    assert len(reports) == 1
    assert reports[0]["issue_id"] == issue["id"]
    assert reports[0]["discrepancy_type"] == "damaged"
    assert reports[0]["investigation_status"] == "open"

    # Return with intact condition should not create a report
    iset2 = _create_set(client, code="SET-INTACT")
    batch2 = _create_batch(client, iset2["id"])
    client.post(f"/api/cssd/sterilization-batches/{batch2['id']}/complete", json={})
    client.post(
        f"/api/cssd/sterilization-batches/{batch2['id']}/qc",
        json={"chemical_indicator_result": "pass", "biological_indicator_result": "pass"},
    )
    issue2 = client.post(
        "/api/cssd/issues",
        json={"instrument_set_id": iset2["id"], "batch_id": batch2["id"], "issued_to_department": "OT-4"},
    ).json()
    client.post(f"/api/cssd/issues/{issue2['id']}/return", json={"return_condition": "intact"})
    reports_after = client.get("/api/cssd/discrepancy-reports").json()
    assert len(reports_after) == 1  # unchanged


# ── Feature 43: discrepancy status transition enforcement ───────────────────


def test_discrepancy_status_transition_enforced(cssd_client):
    client, hospital_id, _ = cssd_client
    iset = _create_set(client, code="SET-DISC")
    batch = _create_batch(client, iset["id"])
    batch_id = batch["id"]
    client.post(f"/api/cssd/sterilization-batches/{batch_id}/complete", json={})
    client.post(
        f"/api/cssd/sterilization-batches/{batch_id}/qc",
        json={"chemical_indicator_result": "pass", "biological_indicator_result": "pass"},
    )
    issue = client.post(
        "/api/cssd/issues",
        json={"instrument_set_id": iset["id"], "batch_id": batch_id, "issued_to_department": "OT-5"},
    ).json()

    report_res = client.post(
        "/api/cssd/discrepancy-reports",
        json={
            "issue_id": issue["id"],
            "discrepancy_type": "missing",
            "instrument_name": "Scalpel Handle #4",
            "quantity_affected": 1,
        },
    )
    assert report_res.status_code == 201
    report = report_res.json()
    assert report["investigation_status"] == "open"

    # Cannot skip open -> resolved directly
    bad_skip = client.post(
        f"/api/cssd/discrepancy-reports/{report['id']}/transition",
        json={"investigation_status": "resolved"},
    )
    assert bad_skip.status_code == 400

    # open -> investigating is allowed
    to_investigating = client.post(
        f"/api/cssd/discrepancy-reports/{report['id']}/transition",
        json={"investigation_status": "investigating"},
    )
    assert to_investigating.status_code == 200
    assert to_investigating.json()["investigation_status"] == "investigating"

    # investigating -> resolved is allowed (no skipping needed here)
    to_resolved = client.post(
        f"/api/cssd/discrepancy-reports/{report['id']}/transition",
        json={"investigation_status": "resolved", "resolution_notes": "Instrument located in linen room"},
    )
    assert to_resolved.status_code == 200
    resolved = to_resolved.json()
    assert resolved["investigation_status"] == "resolved"
    assert resolved["resolved_at"] is not None

    # Cannot transition further once resolved
    no_more = client.post(
        f"/api/cssd/discrepancy-reports/{report['id']}/transition",
        json={"investigation_status": "investigating"},
    )
    assert no_more.status_code == 400
