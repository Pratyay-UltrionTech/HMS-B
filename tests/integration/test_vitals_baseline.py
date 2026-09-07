"""
Behavioral and API Contract Baseline Tests for Unmodified HMS-B Vitals Implementation.

Source under test:
- app/routers/vitals.py
- app/schemas_vitals.py
- app/models.py (VitalReading, Appointment, Patient, Hospital, AuditLog)

All tests execute against the unmodified existing HMS-B code and strictly against
an isolated in-memory SQLite database.
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.database
from app.models import Appointment, AppointmentStatus, AuditLog, Hospital, HospitalUser, Patient, VitalReading


# ===========================================================================
# 1. Azure PostgreSQL Database Isolation Proof
# ===========================================================================

def test_azure_database_isolation_guarantee(db_session: Session):
    """
    Mandatory check: Proves that test execution cannot and does not connect
    to Azure PostgreSQL Flexible Server.
    1. app.database.engine.connect is monkeypatched to raise RuntimeError.
    2. Active db_session dialect is SQLite.
    """
    # Verify production/staging engine connection attempt fails immediately
    with pytest.raises(RuntimeError, match="CRITICAL TEST ISOLATION FAILURE"):
        app.database.engine.connect()

    # Verify active test session is SQLite in-memory
    assert db_session.bind.dialect.name == "sqlite"
    assert ":memory:" in str(db_session.bind.url)


# ===========================================================================
# 2. Authentication and Role-Based Authorization
# ===========================================================================

def test_vitals_endpoints_unauthenticated_returns_403(client: TestClient):
    """Unauthenticated requests to any vitals endpoint must return HTTP 403 (HTTPBearer auto_error)."""
    random_id = uuid.uuid4()

    r1 = client.get("/api/vitals/today")
    assert r1.status_code == 403
    assert r1.json() == {"detail": "Not authenticated"}

    r2 = client.get(f"/api/vitals?appointment_id={random_id}")
    assert r2.status_code == 403
    assert r2.json() == {"detail": "Not authenticated"}

    r3 = client.post("/api/vitals", json={"appointment_id": str(random_id), "items": []})
    assert r3.status_code == 403
    assert r3.json() == {"detail": "Not authenticated"}

    r4 = client.put(f"/api/vitals/{random_id}", json={"name": "BP", "result": "120/80"})
    assert r4.status_code == 403
    assert r4.json() == {"detail": "Not authenticated"}

    r5 = client.delete(f"/api/vitals/{random_id}")
    assert r5.status_code == 403
    assert r5.json() == {"detail": "Not authenticated"}


def test_vitals_endpoints_invalid_token_returns_401(client: TestClient):
    """Invalid JWT bearer token returns HTTP 401."""
    headers = {"Authorization": "Bearer invalid.token.value"}
    res = client.get("/api/vitals/today", headers=headers)
    assert res.status_code == 401
    assert res.json() == {"detail": "Invalid or expired token"}


def test_vitals_endpoints_super_admin_forbidden(client: TestClient, super_admin_headers: dict[str, str]):
    """Super admin role is forbidden from hospital-scoped vitals endpoints (requires hospital_user)."""
    res = client.get("/api/vitals/today", headers=super_admin_headers)
    assert res.status_code == 403
    assert res.json() == {"detail": "Hospital access required"}


def test_vitals_endpoints_missing_hospital_uuid_returns_401(client: TestClient):
    """Token with role 'hospital_staff' but missing hospital_uuid returns HTTP 401."""
    from app.utils.auth import create_access_token

    token = create_access_token({"sub": "staff@test.com", "role": "hospital_staff", "name": "Staff"})
    headers = {"Authorization": f"Bearer {token}"}
    res = client.get("/api/vitals/today", headers=headers)
    assert res.status_code == 401
    assert res.json() == {"detail": "Hospital context missing from token"}


# ===========================================================================
# 3. Cross-Hospital Tenant Isolation
# ===========================================================================

def test_vitals_cross_hospital_isolation_today(
    client: TestClient,
    appointment_factory,
    auth_headers: dict[str, str],
    hospital_b: Hospital,
    doctor_b: HospitalUser,
    patient_b: Patient,
):
    """GET /api/vitals/today strictly isolates appointments by hospital_id in token."""
    # Create appointment in Hospital B
    appt_b = appointment_factory(
        status=AppointmentStatus.scheduled,
        custom_hospital=hospital_b,
        custom_doctor=doctor_b,
        custom_patient=patient_b,
    )

    # Request with Hospital A token
    res = client.get("/api/vitals/today", headers=auth_headers)
    assert res.status_code == 200
    returned_ids = [item["appointment_id"] for item in res.json()]
    assert str(appt_b.id) not in returned_ids


def test_vitals_cross_hospital_isolation_create(
    client: TestClient,
    appointment_factory,
    auth_headers: dict[str, str],
    hospital_b: Hospital,
    doctor_b: HospitalUser,
    patient_b: Patient,
):
    """POST /api/vitals returns 404 if appointment belongs to a different hospital."""
    appt_b = appointment_factory(
        status=AppointmentStatus.scheduled,
        custom_hospital=hospital_b,
        custom_doctor=doctor_b,
        custom_patient=patient_b,
    )

    payload = {
        "appointment_id": str(appt_b.id),
        "items": [{"name": "Blood Pressure", "result": "120/80"}],
    }
    # User A tries to record vitals for appointment in Hospital B
    res = client.post("/api/vitals", json=payload, headers=auth_headers)
    assert res.status_code == 404
    assert res.json() == {"detail": "Appointment not found"}


def test_vitals_cross_hospital_isolation_update_and_delete(
    client: TestClient,
    appointment_factory,
    vital_factory,
    auth_headers: dict[str, str],
    hospital_b: Hospital,
    doctor_b: HospitalUser,
    patient_b: Patient,
):
    """PUT and DELETE /api/vitals/{id} return 404 if vital belongs to another hospital."""
    appt_b = appointment_factory(
        status=AppointmentStatus.scheduled,
        custom_hospital=hospital_b,
        custom_doctor=doctor_b,
        custom_patient=patient_b,
    )
    vital_b = vital_factory(
        appointment=appt_b,
        name="Heart Rate",
        result="75 bpm",
        custom_hospital=hospital_b,
        custom_patient=patient_b,
    )

    # User A tries to update Hospital B vital
    res_put = client.put(
        f"/api/vitals/{vital_b.id}",
        json={"name": "Heart Rate", "result": "80 bpm"},
        headers=auth_headers,
    )
    assert res_put.status_code == 404
    assert res_put.json() == {"detail": "Vital reading not found"}

    # User A tries to delete Hospital B vital
    res_del = client.delete(f"/api/vitals/{vital_b.id}", headers=auth_headers)
    assert res_del.status_code == 404
    assert res_del.json() == {"detail": "Vital reading not found"}


# ===========================================================================
# 4. GET /api/vitals/today
# ===========================================================================

def test_get_today_empty_returns_empty_list(client: TestClient, auth_headers: dict[str, str]):
    """Returns empty list when there are no appointments today."""
    res = client.get("/api/vitals/today", headers=auth_headers)
    assert res.status_code == 200
    assert res.json() == []


def test_get_today_excludes_cancelled_appointments(
    client: TestClient, appointment_factory, auth_headers: dict[str, str]
):
    """Appointments with status 'cancelled' must be excluded from today's list."""
    appt = appointment_factory(status=AppointmentStatus.cancelled)
    res = client.get("/api/vitals/today", headers=auth_headers)
    assert res.status_code == 200
    returned_ids = [item["appointment_id"] for item in res.json()]
    assert str(appt.id) not in returned_ids


def test_get_today_ordered_by_appointment_time_ascending(
    client: TestClient, appointment_factory, auth_headers: dict[str, str]
):
    """Today's appointments must be sorted chronologically by appointment_time ascending."""
    appt_late = appointment_factory(appointment_time=time(14, 0), purpose="Afternoon checkup")
    appt_early = appointment_factory(appointment_time=time(9, 0), purpose="Morning checkup")

    res = client.get("/api/vitals/today", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    assert data[0]["appointment_id"] == str(appt_early.id)
    assert data[1]["appointment_id"] == str(appt_late.id)


def test_get_today_reverts_waiting_without_vitals_to_scheduled(
    client: TestClient, db_session: Session, appointment_factory, auth_headers: dict[str, str]
):
    """
    Business rule: An appointment in 'waiting' status that has NO vitals recorded
    must be reverted to 'scheduled' and have checked_in_at reset to None.
    """
    appt = appointment_factory(
        status=AppointmentStatus.waiting,
        checked_in_at=datetime.now(timezone.utc),
    )

    res = client.get("/api/vitals/today", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["status"] == "scheduled"

    # Verify database was updated
    db_session.expire_all()
    refreshed_appt = db_session.query(Appointment).filter_by(id=appt.id).first()
    assert refreshed_appt.status == AppointmentStatus.scheduled
    assert refreshed_appt.checked_in_at is None


def test_get_today_with_recorded_vitals_populates_details(
    client: TestClient,
    db_session: Session,
    appointment_factory,
    hospital: Hospital,
    patient: Patient,
    auth_headers: dict[str, str],
):
    """Returns appointment details, vitals_count, and nested vitals list."""
    appt = appointment_factory(status=AppointmentStatus.waiting)

    v1 = VitalReading(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        appointment_id=appt.id,
        patient_id=patient.id,
        name="Blood Pressure",
        suitable_range="90-120/60-80",
        result="118/76",
        recorded_by_name="Nurse Joy",
        recorded_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    v2 = VitalReading(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        appointment_id=appt.id,
        patient_id=patient.id,
        name="Temperature",
        suitable_range="97.8-99.1 F",
        result="98.6 F",
        recorded_by_name="Nurse Joy",
        recorded_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    db_session.add_all([v1, v2])
    db_session.commit()

    res = client.get("/api/vitals/today", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    item = data[0]
    assert item["appointment_id"] == str(appt.id)
    assert item["patient_name"] == patient.name
    assert item["patient_uhid"] == patient.uhid
    assert item["doctor_name"] == "Dr. Gregory House"
    assert item["vitals_count"] == 2
    assert len(item["vitals"]) == 2
    names = [v["name"] for v in item["vitals"]]
    assert "Blood Pressure" in names
    assert "Temperature" in names


# ===========================================================================
# 5. GET /api/vitals (Query Filtering)
# ===========================================================================

def test_list_vitals_missing_both_query_params_returns_400(client: TestClient, auth_headers: dict[str, str]):
    """GET /api/vitals requires at least one of appointment_id or patient_id."""
    res = client.get("/api/vitals", headers=auth_headers)
    assert res.status_code == 400
    assert res.json() == {"detail": "Provide appointment_id or patient_id"}


def test_list_vitals_by_appointment_id(
    client: TestClient,
    appointment_factory,
    vital_factory,
    patient: Patient,
    auth_headers: dict[str, str],
):
    """GET /api/vitals?appointment_id=... returns vitals for the appointment ordered by created_at."""
    appt = appointment_factory()
    vital_factory(
        appointment=appt,
        name="Pulse",
        result="72 bpm",
    )

    res = client.get(f"/api/vitals?appointment_id={appt.id}", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["name"] == "Pulse"
    assert data[0]["result"] == "72 bpm"
    assert data[0]["patient_name"] == patient.name


def test_list_vitals_by_patient_id(
    client: TestClient,
    appointment_factory,
    vital_factory,
    patient: Patient,
    auth_headers: dict[str, str],
):
    """GET /api/vitals?patient_id=... returns vitals for that patient across appointments."""
    appt1 = appointment_factory(appointment_date=date.today() - timedelta(days=1))
    appt2 = appointment_factory(appointment_date=date.today())

    vital_factory(
        appointment=appt1,
        name="BP",
        result="120/80",
        created_at=datetime.now(timezone.utc),
    )
    vital_factory(
        appointment=appt2,
        name="BP",
        result="122/82",
        created_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    res = client.get(f"/api/vitals?patient_id={patient.id}", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2


# ===========================================================================
# 6. POST /api/vitals (Batch Creation, Status Transitions, Queue Token, Audit)
# ===========================================================================

def test_create_vitals_appointment_not_found(client: TestClient, auth_headers: dict[str, str]):
    """POST /api/vitals returns 404 if appointment_id does not exist."""
    payload = {
        "appointment_id": str(uuid.uuid4()),
        "items": [{"name": "BP", "result": "120/80"}],
    }
    res = client.post("/api/vitals", json=payload, headers=auth_headers)
    assert res.status_code == 404
    assert res.json() == {"detail": "Appointment not found"}


@pytest.mark.parametrize(
    "appt_status,expected_detail",
    [
        (AppointmentStatus.cancelled, "Cannot change vitals on this visit"),
        (AppointmentStatus.no_show, "Cannot change vitals on this visit"),
        (AppointmentStatus.completed, "Visit is already completed"),
        (AppointmentStatus.transferred_to_inpatient, "Visit was transferred to inpatient"),
    ],
)
def test_create_vitals_rejected_on_terminal_or_invalid_statuses(
    client: TestClient,
    appointment_factory,
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


def test_create_vitals_rejects_empty_item_name_or_result(
    client: TestClient, appointment_factory, auth_headers: dict[str, str]
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


def test_create_vitals_success_and_complete_side_effects(
    client: TestClient,
    db_session: Session,
    appointment_factory,
    patient: Patient,
    hospital: Hospital,
    auth_headers: dict[str, str],
):
    """
    POST /api/vitals:
    1. Returns HTTP 201 Created.
    2. Persists vital records.
    3. Transitions appointment status from 'scheduled' to 'waiting'.
    4. Sets checked_in_at timestamp.
    5. Allocates queue_token starting at 1.
    6. Appends an AuditLog row with action='create' and entity_type='vital_reading'.
    """
    appt = appointment_factory(status=AppointmentStatus.scheduled)
    appt_id = appt.id

    payload = {
        "appointment_id": str(appt_id),
        "items": [
            {"name": "Blood Pressure", "result": "120/80", "suitable_range": "90-120/60-80"},
            {"name": "SpO2", "result": "98%", "suitable_range": "95-100%"},
        ],
    }

    res = client.post("/api/vitals", json=payload, headers=auth_headers)
    assert res.status_code == 201
    data = res.json()
    assert len(data) == 2

    # Verify appointment lifecycle transition
    db_session.expire_all()
    updated_appt = db_session.query(Appointment).filter_by(id=appt_id).first()
    assert updated_appt.status == AppointmentStatus.waiting
    assert updated_appt.checked_in_at is not None
    assert updated_appt.queue_token == 1

    # Verify vital records in DB
    readings = db_session.query(VitalReading).filter_by(appointment_id=appt_id).all()
    assert len(readings) == 2
    assert readings[0].recorded_by_name == "Nurse Joy"

    # Verify audit log entry was created
    audit = (
        db_session.query(AuditLog)
        .filter_by(hospital_id=hospital.id, entity_type="vital_reading", action="create")
        .first()
    )
    assert audit is not None
    assert audit.actor_email == "nurse.joy@apollocity.com"
    assert "Checked in" in audit.summary


def test_create_vitals_sequential_queue_tokens(
    client: TestClient,
    db_session: Session,
    appointment_factory,
    doctor: HospitalUser,
    auth_headers: dict[str, str],
):
    """Subsequent check-ins for the same doctor on the same date receive incremental queue tokens (1, 2)."""
    appt1 = appointment_factory(status=AppointmentStatus.scheduled, appointment_time=time(9, 0))
    appt2 = appointment_factory(status=AppointmentStatus.scheduled, appointment_time=time(9, 30))

    client.post(
        "/api/vitals",
        json={"appointment_id": str(appt1.id), "items": [{"name": "BP", "result": "120/80"}]},
        headers=auth_headers,
    )
    client.post(
        "/api/vitals",
        json={"appointment_id": str(appt2.id), "items": [{"name": "BP", "result": "122/82"}]},
        headers=auth_headers,
    )

    db_session.expire_all()
    a1 = db_session.query(Appointment).filter_by(id=appt1.id).first()
    a2 = db_session.query(Appointment).filter_by(id=appt2.id).first()
    assert a1.queue_token == 1
    assert a2.queue_token == 2


def test_create_vitals_on_already_waiting_appointment_preserves_token(
    client: TestClient,
    db_session: Session,
    appointment_factory,
    auth_headers: dict[str, str],
):
    """Adding extra vitals to an appointment already in 'waiting' preserves its existing queue token."""
    appt = appointment_factory(status=AppointmentStatus.waiting, queue_token=5)

    res = client.post(
        "/api/vitals",
        json={"appointment_id": str(appt.id), "items": [{"name": "Temperature", "result": "99.0 F"}]},
        headers=auth_headers,
    )
    assert res.status_code == 201

    db_session.expire_all()
    refreshed = db_session.query(Appointment).filter_by(id=appt.id).first()
    assert refreshed.status == AppointmentStatus.waiting
    assert refreshed.queue_token == 5  # Unaltered


# ===========================================================================
# 7. PUT /api/vitals/{vital_id} (Update, Completed Guards, Audit)
# ===========================================================================

def test_update_vital_not_found(client: TestClient, auth_headers: dict[str, str]):
    """PUT /api/vitals/{id} returns 404 if vital does not exist."""
    res = client.put(
        f"/api/vitals/{uuid.uuid4()}",
        json={"name": "BP", "result": "120/80"},
        headers=auth_headers,
    )
    assert res.status_code == 404
    assert res.json() == {"detail": "Vital reading not found"}


def test_update_vital_completed_appointment_rejected(
    client: TestClient,
    appointment_factory,
    vital_factory,
    auth_headers: dict[str, str],
):
    """PUT /api/vitals/{id} rejects modification if the linked visit is already completed."""
    appt = appointment_factory(status=AppointmentStatus.completed)
    vital = vital_factory(
        appointment=appt,
        name="Heart Rate",
        result="70 bpm",
    )

    res = client.put(
        f"/api/vitals/{vital.id}",
        json={"result": "75 bpm"},
        headers=auth_headers,
    )
    assert res.status_code == 400
    assert res.json() == {"detail": "Visit is already completed"}


def test_update_vital_validation_empty_fields(
    client: TestClient,
    appointment_factory,
    vital_factory,
    auth_headers: dict[str, str],
):
    """PUT /api/vitals/{id} rejects whitespace/empty name or result."""
    appt = appointment_factory(status=AppointmentStatus.waiting)
    vital = vital_factory(
        appointment=appt,
        name="Heart Rate",
        result="70 bpm",
    )

    res = client.put(f"/api/vitals/{vital.id}", json={"result": "  "}, headers=auth_headers)
    assert res.status_code == 400
    assert res.json() == {"detail": "Each vital needs name and result"}


def test_update_vital_success_and_audit(
    client: TestClient,
    db_session: Session,
    appointment_factory,
    hospital: Hospital,
    patient: Patient,
    auth_headers: dict[str, str],
):
    """
    PUT /api/vitals/{id}:
    1. Returns HTTP 200 with updated fields.
    2. Updates recorded_by_name and recorded_at.
    3. Writes an AuditLog row with action='update'.
    """
    appt = appointment_factory(status=AppointmentStatus.waiting)
    vital_id = uuid.uuid4()
    vital = VitalReading(
        id=vital_id,
        hospital_id=hospital.id,
        appointment_id=appt.id,
        patient_id=patient.id,
        name="Heart Rate",
        result="70 bpm",
        suitable_range="60-100 bpm",
        recorded_by_name="Nurse Clara",
        recorded_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db_session.add(vital)
    db_session.commit()

    payload = {
        "name": "Pulse Rate",
        "result": "78 bpm",
        "suitable_range": "60-90 bpm",
    }
    res = client.put(f"/api/vitals/{vital_id}", json=payload, headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "Pulse Rate"
    assert data["result"] == "78 bpm"
    assert data["suitable_range"] == "60-90 bpm"
    assert data["recorded_by_name"] == "Nurse Joy"

    # Verify audit log entry
    db_session.expire_all()
    audit = (
        db_session.query(AuditLog)
        .filter_by(hospital_id=hospital.id, entity_type="vital_reading", action="update")
        .first()
    )
    assert audit is not None
    assert "Updated vital Pulse Rate" in audit.summary


# ===========================================================================
# 8. DELETE /api/vitals/{vital_id} (Deletion, Completed Guards, Audit)
# ===========================================================================

def test_delete_vital_not_found(client: TestClient, auth_headers: dict[str, str]):
    """DELETE /api/vitals/{id} returns 404 if vital does not exist."""
    res = client.delete(f"/api/vitals/{uuid.uuid4()}", headers=auth_headers)
    assert res.status_code == 404
    assert res.json() == {"detail": "Vital reading not found"}


def test_delete_vital_completed_appointment_rejected(
    client: TestClient,
    appointment_factory,
    vital_factory,
    auth_headers: dict[str, str],
):
    """DELETE /api/vitals/{id} rejects deletion if visit is already completed."""
    appt = appointment_factory(status=AppointmentStatus.completed)
    vital = vital_factory(
        appointment=appt,
        name="Weight",
        result="70 kg",
    )

    res = client.delete(f"/api/vitals/{vital.id}", headers=auth_headers)
    assert res.status_code == 400
    assert res.json() == {"detail": "Visit is already completed"}


def test_delete_vital_success_and_audit(
    client: TestClient,
    db_session: Session,
    appointment_factory,
    vital_factory,
    hospital: Hospital,
    auth_headers: dict[str, str],
):
    """
    DELETE /api/vitals/{id}:
    1. Returns HTTP 204 No Content.
    2. Removes vital from database.
    3. Writes an AuditLog row with action='delete'.
    """
    appt = appointment_factory(status=AppointmentStatus.waiting)
    vital = vital_factory(
        appointment=appt,
        name="Weight",
        result="70 kg",
    )
    vital_id = vital.id

    res = client.delete(f"/api/vitals/{vital_id}", headers=auth_headers)
    assert res.status_code == 204

    # Verify vital row deleted
    db_session.expire_all()
    deleted = db_session.query(VitalReading).filter_by(id=vital_id).first()
    assert deleted is None

    # Verify audit log entry
    audit = (
        db_session.query(AuditLog)
        .filter_by(hospital_id=hospital.id, entity_type="vital_reading", action="delete")
        .first()
    )
    assert audit is not None
    assert "Deleted vital Weight" in audit.summary
