"""Tests for Patient Identification Label generation and printing.

Verifies:
1. Label endpoint returns 200 HTML with barcode SVG and PHI allowlist fields.
2. PHI allowlist: Hospital name, UHID, Patient name, Age, Sex, Blood Group are present.
3. Strict Exclusion: No diagnosis, medication, or unauthenticated clinical URLs.
4. Tenant Scoping: Patients from other hospitals return 404 or cannot be accessed across tenants.
5. Format types: standard (50x25mm) and wristband (250x25mm).
6. Label printing writes an audit log entry.
7. Appointment encounter context: OP, appointment datetime, doctor, security validation.
"""

from datetime import date, datetime, time, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infrastructure.postgres.session import get_transitional_sync_session
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.patients.api.patients_api import router as migrated_patients_router
from modules.patients.entities.patient import Patient, PatientStatus
from modules.patients.services.patient_label_service import normalize_gender_short
from modules.tenancy.entities.hospital import Hospital
from shared.audit.entities.audit_log import AuditLog
from shared.exceptions import register_exception_handlers
from tests.conftest import (
    auth_headers,
    db_session,
    doctor,
    hospital,
    hospital_b,
)


@pytest.fixture(scope="function")
def patients_app(db_session: Session) -> FastAPI:
    app = FastAPI(title="Patients Test App")
    register_exception_handlers(app)
    app.include_router(migrated_patients_router, prefix="/api")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_transitional_sync_session] = _override_db
    return app


@pytest.fixture(scope="function")
def patients_client(patients_app: FastAPI) -> TestClient:
    return TestClient(patients_app)


def test_patient_label_html_generation(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    auth_headers: dict[str, str],
):
    """Test generating patient identification label HTML with barcode and allowlist."""
    hospital.facility_settings = {
        "display_name": "Apollo City",
        "institutional_code": "SHC",
    }
    db_session.add(hospital)
    db_session.commit()

    patient = Patient(
        hospital_id=hospital.id,
        uhid="P0042",
        first_name="Ravi",
        last_name="Kumar",
        name="Ravi Kumar",
        gender="Male",
        age=35,
        mobile="+919876543210",
        blood_group="O+",
        status=PatientStatus.active,
        created_at=datetime(2020, 1, 15, 9, 30, tzinfo=timezone.utc),
    )
    db_session.add(patient)
    db_session.commit()
    db_session.refresh(patient)

    res = patients_client.get(
        f"/api/registration/patients/{patient.id}/label",
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    html = res.text

    assert "Ravi Kumar" in html
    assert "P0042" in html
    assert "35Y/M" in html
    assert "O+" in html
    assert "SHC" in html
    assert "15-Jan-2020" in html
    assert "APPOINTMENT" not in html
    assert "<svg" in html
    assert "window.print()" in html
    assert "50mm 25mm" in html
    assert "transform: rotate" not in html

    assert "diagnosis" not in html.lower()
    assert "prescription" not in html.lower()

    res_band = patients_client.get(
        f"/api/registration/patients/{patient.id}/label?format=wristband&download=true",
        headers=auth_headers,
    )
    assert res_band.status_code == 200
    assert "Wristband" in res_band.text
    assert "window.print()" not in res_band.text
    assert "250mm 25mm" in res_band.text

    audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.hospital_id == hospital.id,
            AuditLog.action == "print_label",
            AuditLog.entity_id == str(patient.id),
        )
        .first()
    )
    assert audit is not None
    assert "P0042" in audit.summary


def test_patient_label_appointment_context(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    doctor,
    auth_headers: dict[str, str],
):
    """Appointment-aware label shows visit datetime and doctor, not registration as appointment time."""
    patient = Patient(
        hospital_id=hospital.id,
        uhid="P0100",
        first_name="Shoba",
        last_name="Rani",
        name="Shoba rani",
        gender="Female",
        age=38,
        mobile="+919876543299",
        status=PatientStatus.active,
        created_at=datetime(2026, 9, 22, 4, 41, tzinfo=timezone.utc),
    )
    db_session.add(patient)
    db_session.flush()

    op_id = "OP-2026-00042"
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        appointment_date=date(2026, 9, 25),
        appointment_time=time(10, 30),
        purpose="Follow-up",
        visit_type="OPD",
        status=AppointmentStatus.scheduled,
        op_id=op_id,
    )
    db_session.add(appt)
    db_session.commit()

    res = patients_client.get(
        f"/api/registration/patients/{patient.id}/label?encounter_id={op_id}",
        headers=auth_headers,
    )
    assert res.status_code == 200
    html = res.text

    assert "APPOINTMENT" in html
    assert op_id in html
    assert "25-Sep-2026 10:30" in html
    assert doctor.name.split()[-1] in html or doctor.name in html
    assert "38Y/F" in html
    assert "04:41" not in html and "22-Sep-2026 04:41" not in html


def test_patient_label_appointment_security(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    hospital_b: Hospital,
    doctor,
    auth_headers: dict[str, str],
):
    """Invalid or cross-patient encounter_id must not render appointment context."""
    patient_a = Patient(
        hospital_id=hospital.id,
        uhid="P0200",
        first_name="A",
        last_name="One",
        name="A One",
        gender="Male",
        age=30,
        mobile="+919800000001",
        status=PatientStatus.active,
    )
    patient_b = Patient(
        hospital_id=hospital.id,
        uhid="P0201",
        first_name="B",
        last_name="Two",
        name="B Two",
        gender="Female",
        age=31,
        mobile="+919800000002",
        status=PatientStatus.active,
    )
    db_session.add_all([patient_a, patient_b])
    db_session.flush()

    op_id = "OP-2026-00999"
    appt = Appointment(
        id=uuid4(),
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient_b.id,
        appointment_date=date(2026, 10, 1),
        appointment_time=time(9, 0),
        purpose="Consult",
        visit_type="OPD",
        status=AppointmentStatus.scheduled,
        op_id=op_id,
    )
    db_session.add(appt)
    db_session.commit()

    wrong_patient = patients_client.get(
        f"/api/registration/patients/{patient_a.id}/label?encounter_id={op_id}",
        headers=auth_headers,
    )
    assert wrong_patient.status_code == 404

    foreign_patient = Patient(
        hospital_id=hospital_b.id,
        uhid="P9998",
        first_name="Foreign",
        last_name="Patient",
        name="Foreign Patient",
        gender="Male",
        age=40,
        mobile="+919800000099",
        status=PatientStatus.active,
    )
    db_session.add(foreign_patient)
    db_session.commit()

    cross_tenant = patients_client.get(
        f"/api/registration/patients/{foreign_patient.id}/label?encounter_id={op_id}",
        headers=auth_headers,
    )
    assert cross_tenant.status_code == 404


def test_normalize_gender_short_codes():
    assert normalize_gender_short("Male") == "M"
    assert normalize_gender_short("female") == "F"
    assert normalize_gender_short("Non-binary") == "O"
    assert normalize_gender_short(None) == "—"


def test_patient_label_cross_tenant_isolation(
    patients_client: TestClient,
    db_session: Session,
    hospital: Hospital,
    hospital_b: Hospital,
    auth_headers: dict[str, str],
):
    """Test that a user cannot generate labels for a patient belonging to a different hospital."""
    foreign_patient = Patient(
        hospital_id=hospital_b.id,
        uhid="P9999",
        first_name="Anita",
        last_name="Sharma",
        name="Anita Sharma",
        gender="Female",
        age=28,
        mobile="+919876543211",
        status=PatientStatus.active,
    )
    db_session.add(foreign_patient)
    db_session.commit()

    res = patients_client.get(
        f"/api/registration/patients/{foreign_patient.id}/label",
        headers=auth_headers,
    )
    assert res.status_code == 404
