"""
Module-level tests for the migrated Vitals slice.

Verifies:
1. Unit tests for validators (vitals_validator.py).
2. Action orchestration and business workflows.
3. API routing, authentication, tenant isolation, and response contracts
   via TestClient targeting hms_migration.modules.vitals.api.vitals_api.router.
"""

import uuid
from datetime import date, datetime, time, timezone
from typing import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Appointment,
    AppointmentStatus,
    AuditLog,
    Hospital,
    HospitalUser,
    Patient,
    VitalReading,
)
from hms_migration.modules.vitals.actions.create_vitals_action import CreateVitalsAction
from hms_migration.modules.vitals.actions.get_today_vitals_action import GetTodayVitalsAction
from hms_migration.modules.vitals.contracts.vitals_contracts import (
    VitalBatchCreate,
    VitalItemCreate,
)
from hms_migration.modules.vitals.db.vitals_repository import VitalsRepository
from hms_migration.modules.vitals.exceptions import (
    VitalMutationNotAllowedError,
    VitalValidationError,
)
from hms_migration.modules.vitals.validators.vitals_validator import (
    assert_can_mutate_vitals,
    validate_list_vitals_query,
    validate_vital_item_strings,
)


# ===========================================================================
# 1. Validator Unit Tests
# ===========================================================================

def test_validator_list_query_params_enforcement():
    """Verify validate_list_vitals_query rejects requests without filter parameters."""
    with pytest.raises(VitalValidationError) as exc_info:
        validate_list_vitals_query(None, None)
    assert exc_info.value.code == "VITAL_VALIDATION_ERROR"
    assert exc_info.value.message == "Provide appointment_id or patient_id"

    # Supplying either parameter should pass without error
    validate_list_vitals_query(uuid.uuid4(), None)
    validate_list_vitals_query(None, uuid.uuid4())
    validate_list_vitals_query(uuid.uuid4(), uuid.uuid4())


def test_validator_vital_item_strings():
    """Verify validate_vital_item_strings strips whitespace and rejects empty values."""
    with pytest.raises(VitalValidationError) as exc_info:
        validate_vital_item_strings("   ", "120/80")
    assert exc_info.value.code == "VITAL_VALIDATION_ERROR"
    assert exc_info.value.message == "Each vital needs name and result"

    with pytest.raises(VitalValidationError) as exc_info2:
        validate_vital_item_strings("Blood Pressure", "   ")
    assert exc_info2.value.code == "VITAL_VALIDATION_ERROR"
    assert exc_info2.value.message == "Each vital needs name and result"

    name, result = validate_vital_item_strings("  Pulse  ", "  72 bpm  ")
    assert name == "Pulse"
    assert result == "72 bpm"


@pytest.mark.parametrize(
    "status_val,expected_detail",
    [
        (AppointmentStatus.cancelled, "Cannot change vitals on this visit"),
        (AppointmentStatus.no_show, "Cannot change vitals on this visit"),
        (AppointmentStatus.completed, "Visit is already completed"),
        (AppointmentStatus.transferred_to_inpatient, "Visit was transferred to inpatient"),
    ],
)
def test_validator_assert_can_mutate_vitals_rejections(status_val, expected_detail):
    """Verify assert_can_mutate_vitals blocks terminal visit states."""
    appt = Appointment(status=status_val)
    with pytest.raises(VitalMutationNotAllowedError) as exc_info:
        assert_can_mutate_vitals(appt)
    assert exc_info.value.code == "VITAL_MUTATION_DISALLOWED"
    assert exc_info.value.message == expected_detail


def test_validator_assert_can_mutate_vitals_allowed_states():
    """Verify assert_can_mutate_vitals passes for active states."""
    for active_status in [AppointmentStatus.scheduled, AppointmentStatus.waiting]:
        appt = Appointment(status=active_status)
        assert_can_mutate_vitals(appt)  # Should not raise


# ===========================================================================
# 2. Action & Repository Unit Tests
# ===========================================================================

def test_action_get_today_vitals_reversion(
    db_session: Session,
    hospital: Hospital,
    appointment_factory: Callable,
):
    """Directly test GetTodayVitalsAction auto-reverts waiting visits without vitals."""
    appt = appointment_factory(
        status=AppointmentStatus.waiting,
        appointment_date=date.today(),
        checked_in_at=datetime.now(timezone.utc),
    )
    repo = VitalsRepository(db=db_session, hospital_id=hospital.id)
    action = GetTodayVitalsAction(repo)

    result = action.execute(on_date=date.today())
    assert len(result) == 1
    assert result[0].status == "scheduled"
    assert result[0].vitals_count == 0

    # Verify database persistence
    db_session.refresh(appt)
    assert appt.status == AppointmentStatus.scheduled
    assert appt.checked_in_at is None


def test_action_create_vitals_sequential_queue_tokens(
    db_session: Session,
    hospital: Hospital,
    doctor: HospitalUser,
    patient: Patient,
    appointment_factory: Callable,
):
    """Directly test CreateVitalsAction assigns sequential queue tokens."""
    appt1 = appointment_factory(status=AppointmentStatus.scheduled, appointment_time=time(9, 0))
    appt2 = appointment_factory(status=AppointmentStatus.scheduled, appointment_time=time(9, 30))

    repo = VitalsRepository(db=db_session, hospital_id=hospital.id)
    action = CreateVitalsAction(repo)
    user = {"name": "Nurse Joy", "sub": "nurse@hospital.com", "role": "hospital_staff"}

    payload1 = VitalBatchCreate(
        appointment_id=appt1.id,
        items=[VitalItemCreate(name="BP", result="120/80")],
    )
    payload2 = VitalBatchCreate(
        appointment_id=appt2.id,
        items=[VitalItemCreate(name="BP", result="118/78")],
    )

    action.execute(payload1, user)
    action.execute(payload2, user)

    db_session.refresh(appt1)
    db_session.refresh(appt2)
    assert appt1.queue_token == 1
    assert appt2.queue_token == 2


# ===========================================================================
# 3. API Routing, Auth, Multi-Tenancy & Endpoint Integration Tests
# ===========================================================================

def test_api_unauthenticated_returns_403(client: TestClient):
    """Endpoints require Bearer auth."""
    resp = client.get("/api/vitals/today")
    assert resp.status_code == 403


def test_api_invalid_token_returns_401(client: TestClient):
    """Invalid token returns 401."""
    resp = client.get("/api/vitals/today", headers={"Authorization": "Bearer invalid.jwt.token"})
    assert resp.status_code == 401


def test_api_super_admin_forbidden(client: TestClient, super_admin_headers: dict[str, str]):
    """Super admins cannot perform hospital-scoped vitals operations."""
    resp = client.get("/api/vitals/today", headers=super_admin_headers)
    assert resp.status_code == 403


def test_api_missing_hospital_uuid_returns_401(client: TestClient):
    """Token without hospital_uuid returns 401."""
    from app.utils.auth import create_access_token
    token = create_access_token({"sub": "staff@test.com", "role": "hospital_staff"})
    resp = client.get("/api/vitals/today", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_api_cross_hospital_isolation_today(
    client: TestClient,
    appointment_factory: Callable,
    auth_headers: dict[str, str],
    auth_headers_b: dict[str, str],
    hospital_b: Hospital,
    doctor_b: HospitalUser,
    patient_b: Patient,
):
    """Hospital A user cannot view Hospital B appointments in /today."""
    appointment_factory(
        custom_hospital=hospital_b,
        custom_doctor=doctor_b,
        custom_patient=patient_b,
        appointment_date=date.today(),
    )
    resp_a = client.get("/api/vitals/today", headers=auth_headers)
    assert resp_a.status_code == 200
    assert resp_a.json() == []

    resp_b = client.get("/api/vitals/today", headers=auth_headers_b)
    assert resp_b.status_code == 200
    assert len(resp_b.json()) == 1


def test_api_cross_hospital_isolation_create(
    client: TestClient,
    appointment_factory: Callable,
    auth_headers: dict[str, str],
    hospital_b: Hospital,
    doctor_b: HospitalUser,
    patient_b: Patient,
):
    """Hospital A user cannot record vitals for Hospital B appointments."""
    appt_b = appointment_factory(
        custom_hospital=hospital_b,
        custom_doctor=doctor_b,
        custom_patient=patient_b,
    )
    payload = {
        "appointment_id": str(appt_b.id),
        "items": [{"name": "Blood Pressure", "result": "120/80"}],
    }
    resp = client.post("/api/vitals", json=payload, headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Appointment not found"


def test_api_cross_hospital_isolation_update_and_delete(
    client: TestClient,
    appointment_factory: Callable,
    vital_factory: Callable,
    auth_headers: dict[str, str],
    hospital_b: Hospital,
    doctor_b: HospitalUser,
    patient_b: Patient,
):
    """Hospital A user cannot update or delete Hospital B vitals."""
    appt_b = appointment_factory(
        custom_hospital=hospital_b,
        custom_doctor=doctor_b,
        custom_patient=patient_b,
        status=AppointmentStatus.waiting,
    )
    vital_b = vital_factory(
        appointment=appt_b,
        custom_hospital=hospital_b,
        custom_patient=patient_b,
    )

    # Attempt update across tenant boundary
    resp_put = client.put(f"/api/vitals/{vital_b.id}", json={"result": "130/90"}, headers=auth_headers)
    assert resp_put.status_code == 404

    # Attempt delete across tenant boundary
    resp_del = client.delete(f"/api/vitals/{vital_b.id}", headers=auth_headers)
    assert resp_del.status_code == 404


def test_api_get_today_empty(client: TestClient, auth_headers: dict[str, str]):
    """Returns empty list when no appointments exist today."""
    resp = client.get("/api/vitals/today", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_get_today_ordered_chronologically(
    client: TestClient,
    appointment_factory: Callable,
    auth_headers: dict[str, str],
):
    """Appointments are returned sorted by appointment_time ascending."""
    a3 = appointment_factory(appointment_date=date.today(), appointment_time=time(11, 0))
    a1 = appointment_factory(appointment_date=date.today(), appointment_time=time(9, 0))
    a2 = appointment_factory(appointment_date=date.today(), appointment_time=time(10, 0))

    resp = client.get("/api/vitals/today", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    assert data[0]["appointment_id"] == str(a1.id)
    assert data[1]["appointment_id"] == str(a2.id)
    assert data[2]["appointment_id"] == str(a3.id)


def test_api_get_today_excludes_cancelled(
    client: TestClient,
    appointment_factory: Callable,
    auth_headers: dict[str, str],
):
    """Cancelled appointments are excluded from /today."""
    appointment_factory(status=AppointmentStatus.cancelled, appointment_date=date.today())
    a_valid = appointment_factory(status=AppointmentStatus.scheduled, appointment_date=date.today())

    resp = client.get("/api/vitals/today", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["appointment_id"] == str(a_valid.id)


def test_api_get_today_reverts_waiting_without_vitals_to_scheduled(
    client: TestClient,
    db_session: Session,
    appointment_factory: Callable,
    auth_headers: dict[str, str],
):
    """OPD queue reverts waiting visits without vitals back to scheduled."""
    appt = appointment_factory(
        status=AppointmentStatus.waiting,
        appointment_date=date.today(),
        checked_in_at=datetime.now(timezone.utc),
    )
    resp = client.get("/api/vitals/today", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "scheduled"
    assert data[0]["vitals_count"] == 0

    db_session.refresh(appt)
    assert appt.status == AppointmentStatus.scheduled
    assert appt.checked_in_at is None


def test_api_get_today_with_recorded_vitals_populates_details(
    client: TestClient,
    appointment_factory: Callable,
    vital_factory: Callable,
    auth_headers: dict[str, str],
):
    """OPD queue includes vitals list and vitals_count when vitals exist."""
    appt = appointment_factory(status=AppointmentStatus.waiting, appointment_date=date.today())
    vital_factory(appointment=appt, name="BP", result="120/80")
    vital_factory(appointment=appt, name="Pulse", result="72 bpm")

    resp = client.get("/api/vitals/today", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["vitals_count"] == 2
    assert len(data[0]["vitals"]) == 2


def test_api_list_vitals_query_validation(client: TestClient, auth_headers: dict[str, str]):
    """GET /api/vitals requires appointment_id or patient_id."""
    resp = client.get("/api/vitals", headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Provide appointment_id or patient_id"


def test_api_list_vitals_by_appointment(
    client: TestClient,
    appointment_factory: Callable,
    vital_factory: Callable,
    auth_headers: dict[str, str],
):
    """GET /api/vitals?appointment_id=... filters by appointment."""
    appt = appointment_factory()
    vital_factory(appointment=appt, name="Temperature", result="98.6 F")

    resp = client.get(f"/api/vitals?appointment_id={appt.id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "Temperature"
    assert data[0]["result"] == "98.6 F"


def test_api_list_vitals_by_patient(
    client: TestClient,
    patient: Patient,
    appointment_factory: Callable,
    vital_factory: Callable,
    auth_headers: dict[str, str],
):
    """GET /api/vitals?patient_id=... filters by patient."""
    appt1 = appointment_factory()
    appt2 = appointment_factory()
    vital_factory(appointment=appt1, name="BP", result="120/80")
    vital_factory(appointment=appt2, name="Pulse", result="75 bpm")

    resp = client.get(f"/api/vitals?patient_id={patient.id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = [d["name"] for d in data]
    assert "BP" in names
    assert "Pulse" in names


def test_api_create_vitals_appointment_not_found(client: TestClient, auth_headers: dict[str, str]):
    """POST /api/vitals with unknown appointment_id returns 404."""
    payload = {
        "appointment_id": str(uuid.uuid4()),
        "items": [{"name": "BP", "result": "120/80"}],
    }
    resp = client.post("/api/vitals", json=payload, headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Appointment not found"


@pytest.mark.parametrize(
    "appt_status,expected_detail",
    [
        (AppointmentStatus.cancelled, "Cannot change vitals on this visit"),
        (AppointmentStatus.no_show, "Cannot change vitals on this visit"),
        (AppointmentStatus.completed, "Visit is already completed"),
        (AppointmentStatus.transferred_to_inpatient, "Visit was transferred to inpatient"),
    ],
)
def test_api_create_vitals_rejected_on_terminal_or_invalid_statuses(
    client: TestClient,
    appointment_factory: Callable,
    auth_headers: dict[str, str],
    appt_status: AppointmentStatus,
    expected_detail: str,
):
    """POST /api/vitals rejects appointments in completed, cancelled, no-show, or transferred states."""
    appt = appointment_factory(status=appt_status)
    payload = {
        "appointment_id": str(appt.id),
        "items": [{"name": "BP", "result": "120/80"}],
    }
    res = client.post("/api/vitals", json=payload, headers=auth_headers)
    assert res.status_code == 400
    assert res.json() == {"detail": expected_detail}


def test_api_create_vitals_rejects_empty_item_name_or_result(
    client: TestClient,
    appointment_factory: Callable,
    auth_headers: dict[str, str],
):
    """POST /api/vitals rejects items with whitespace or empty name / result."""
    appt = appointment_factory(status=AppointmentStatus.scheduled)

    # Empty name
    p1 = {"appointment_id": str(appt.id), "items": [{"name": "   ", "result": "120/80"}]}
    r1 = client.post("/api/vitals", json=p1, headers=auth_headers)
    assert r1.status_code == 400
    assert r1.json() == {"detail": "Each vital needs name and result"}

    # Empty result
    p2 = {"appointment_id": str(appt.id), "items": [{"name": "BP", "result": "   "}]}
    r2 = client.post("/api/vitals", json=p2, headers=auth_headers)
    assert r2.status_code == 400
    assert r2.json() == {"detail": "Each vital needs name and result"}


def test_api_create_vitals_success_and_side_effects(
    client: TestClient,
    db_session: Session,
    appointment_factory: Callable,
    auth_headers: dict[str, str],
    hospital: Hospital,
):
    """POST /api/vitals batch creation transitions status, assigns queue token, and logs audit."""
    appt = appointment_factory(status=AppointmentStatus.scheduled)
    payload = {
        "appointment_id": str(appt.id),
        "items": [
            {"name": "Blood Pressure", "result": "120/80", "suitable_range": "90-120"},
            {"name": "Pulse", "result": "72 bpm"},
        ],
    }
    resp = client.post("/api/vitals", json=payload, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert len(data) == 2

    # Verify visit transitioned to waiting and received queue token
    db_session.refresh(appt)
    assert appt.status == AppointmentStatus.waiting
    assert appt.checked_in_at is not None
    assert appt.queue_token == 1

    # Verify audit log row created
    audit = db_session.query(AuditLog).filter_by(hospital_id=hospital.id, action="create").first()
    assert audit is not None
    assert audit.entity_type == "vital_reading"
    assert "Checked in" in audit.summary


def test_api_create_vitals_on_already_waiting_appointment_preserves_token(
    client: TestClient,
    db_session: Session,
    appointment_factory: Callable,
    auth_headers: dict[str, str],
):
    """POST /api/vitals on an appointment already in waiting status preserves existing queue token."""
    appt = appointment_factory(status=AppointmentStatus.waiting, queue_token=42)
    payload = {
        "appointment_id": str(appt.id),
        "items": [{"name": "Blood Pressure", "result": "120/80"}],
    }
    resp = client.post("/api/vitals", json=payload, headers=auth_headers)
    assert resp.status_code == 201

    db_session.refresh(appt)
    assert appt.status == AppointmentStatus.waiting
    assert appt.queue_token == 42


def test_api_update_vital_success_and_audit(
    client: TestClient,
    db_session: Session,
    appointment_factory: Callable,
    vital_factory: Callable,
    auth_headers: dict[str, str],
    hospital: Hospital,
):
    """PUT /api/vitals/{id} updates fields and writes audit log."""
    appt = appointment_factory(status=AppointmentStatus.waiting)
    vital = vital_factory(appointment=appt, name="Blood Pressure", result="120/80")

    update_payload = {"result": "130/85"}
    resp = client.put(f"/api/vitals/{vital.id}", json=update_payload, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["result"] == "130/85"

    audit = db_session.query(AuditLog).filter_by(hospital_id=hospital.id, action="update").first()
    assert audit is not None
    assert audit.entity_id == str(vital.id)


def test_api_update_vital_not_found(client: TestClient, auth_headers: dict[str, str]):
    """PUT /api/vitals/{id} with nonexistent ID returns 404."""
    resp = client.put(f"/api/vitals/{uuid.uuid4()}", json={"name": "Pulse"}, headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Vital reading not found"


def test_api_update_vital_validation_empty_fields(
    client: TestClient,
    appointment_factory: Callable,
    vital_factory: Callable,
    auth_headers: dict[str, str],
):
    """PUT /api/vitals/{id} rejecting empty strings after stripping."""
    appt = appointment_factory(status=AppointmentStatus.waiting)
    vital = vital_factory(appointment=appt)

    resp = client.put(f"/api/vitals/{vital.id}", json={"name": "   "}, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Each vital needs name and result"


def test_api_update_vital_completed_appointment_rejected(
    client: TestClient,
    appointment_factory: Callable,
    vital_factory: Callable,
    auth_headers: dict[str, str],
):
    """PUT /api/vitals/{id} on completed appointment returns 400."""
    appt = appointment_factory(status=AppointmentStatus.completed)
    vital = vital_factory(appointment=appt)

    resp = client.put(f"/api/vitals/{vital.id}", json={"result": "110/70"}, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Visit is already completed"


def test_api_delete_vital_success_and_audit(
    client: TestClient,
    db_session: Session,
    appointment_factory: Callable,
    vital_factory: Callable,
    auth_headers: dict[str, str],
    hospital: Hospital,
):
    """DELETE /api/vitals/{id} deletes row, returns 204, and writes audit."""
    appt = appointment_factory(status=AppointmentStatus.waiting)
    vital = vital_factory(appointment=appt)
    vital_id = vital.id

    resp = client.delete(f"/api/vitals/{vital_id}", headers=auth_headers)
    assert resp.status_code == 204

    db_session.expire_all()
    assert db_session.query(VitalReading).filter_by(id=vital_id).first() is None

    audit = db_session.query(AuditLog).filter_by(hospital_id=hospital.id, action="delete").first()
    assert audit is not None
    assert audit.entity_id == str(vital_id)



def test_api_delete_vital_not_found(client: TestClient, auth_headers: dict[str, str]):
    """DELETE /api/vitals/{id} with nonexistent ID returns 404."""
    resp = client.delete(f"/api/vitals/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Vital reading not found"


def test_api_delete_vital_completed_appointment_rejected(
    client: TestClient,
    appointment_factory: Callable,
    vital_factory: Callable,
    auth_headers: dict[str, str],
):
    """DELETE /api/vitals/{id} on completed appointment returns 400."""
    appt = appointment_factory(status=AppointmentStatus.completed)
    vital = vital_factory(appointment=appt)

    resp = client.delete(f"/api/vitals/{vital.id}", headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Visit is already completed"
