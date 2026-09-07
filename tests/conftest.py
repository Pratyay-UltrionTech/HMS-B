"""
Test configuration and fixtures for HMS Backend baseline test suite.

Guarantees:
1. Complete isolation from Azure PostgreSQL Flexible Server.
2. In-memory SQLite database for deterministic test execution.
3. FastAPI dependency overriding for database and authentication.
"""

import os
import uuid
from datetime import date, datetime, time, timezone
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# 1. SQLAlchemy SQLite compatibility hooks
# ---------------------------------------------------------------------------
# PostgreSQL JSONB column compiler hook for SQLite
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


# ---------------------------------------------------------------------------
# 2. Azure isolation safeguards
# ---------------------------------------------------------------------------
# Enforce dummy credentials in environment to prevent any external networking
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = ""
os.environ["POSTGRES_HOST"] = "127.0.0.1"
os.environ["POSTGRES_PORT"] = "9999"  # Closed port to fail-fast if attempted
os.environ["POSTGRES_DB"] = "HMS_ISOLATED_TEST"

import app.database
from app.database import Base, get_db
import app.models  # Register all 63 ORM models in Base.metadata
from app.models import Appointment, AppointmentStatus, Hospital, HospitalUser, Patient, VitalReading
from app.routers import vitals
from app.utils.auth import create_access_token

# Defensively monkeypatch production engine.connect so that ANY code path
# accidentally referencing app.database.engine fails immediately.
def _blocked_azure_connect(*args, **kwargs):
    raise RuntimeError(
        "CRITICAL TEST ISOLATION FAILURE: Attempted connection to production/staging "
        "Azure PostgreSQL database engine during test run!"
    )

app.database.engine.connect = _blocked_azure_connect


# ---------------------------------------------------------------------------
# 3. Test Database Engine & Session
# ---------------------------------------------------------------------------
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=test_engine)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    """Provides a transactional database session for each test, cleaned up afterwards."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        # Clean data between test runs to ensure full isolation without cyclic FK warnings
        with test_engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            for table in Base.metadata.tables.values():
                conn.execute(table.delete())
            conn.execute(text("PRAGMA foreign_keys = ON"))



@pytest.fixture(scope="function")
def app(db_session: Session) -> FastAPI:
    """
    Constructs an isolated FastAPI application mounting the unmodified vitals router.
    No lifespan context is executed, guaranteeing zero background loops and zero Azure calls.
    """
    test_app = FastAPI(title="HMS Vitals Baseline Test App")
    test_app.include_router(vitals.router, prefix="/api")

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = _override_get_db
    return test_app


@pytest.fixture(scope="function")
def client(app: FastAPI) -> TestClient:
    """FastAPI TestClient pointing to the isolated test application."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# 4. Deterministic Test Fixtures (Models & Auth)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="function")
def hospital(db_session: Session) -> Hospital:
    """Primary test hospital fixture."""
    h = Hospital(
        id=uuid.uuid4(),
        hospital_id="HOSP001",
        name="Apollo City Hospital",
        address="100 Medical Center Way",
        phone="555-0100",
        email="admin@apollocity.com",
        password_hash="dummy_hash_for_test",
    )
    db_session.add(h)
    db_session.commit()
    db_session.refresh(h)
    return h


@pytest.fixture(scope="function")
def hospital_b(db_session: Session) -> Hospital:
    """Secondary test hospital for cross-hospital tenant isolation tests."""
    h = Hospital(
        id=uuid.uuid4(),
        hospital_id="HOSP002",
        name="Fortis Regional Hospital",
        address="200 Health Blvd",
        phone="555-0200",
        email="admin@fortisreg.com",
        password_hash="dummy_hash_for_test",
    )
    db_session.add(h)
    db_session.commit()
    db_session.refresh(h)
    return h


@pytest.fixture(scope="function")
def doctor(db_session: Session, hospital: Hospital) -> HospitalUser:
    """Primary doctor user in hospital."""
    doc = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        role_id=uuid.uuid4(),
        name="Dr. Gregory House",
        phone="555-0101",
        email="house@apollocity.com",
        password_hash="dummy_hash_for_test",
        specialization="Diagnostic Medicine",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.fixture(scope="function")
def doctor_b(db_session: Session, hospital_b: Hospital) -> HospitalUser:
    """Doctor in secondary hospital."""
    doc = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hospital_b.id,
        role_id=uuid.uuid4(),
        name="Dr. James Wilson",
        phone="555-0201",
        email="wilson@fortisreg.com",
        password_hash="dummy_hash_for_test",
        specialization="Oncology",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.fixture(scope="function")
def patient(db_session: Session, hospital: Hospital) -> Patient:
    """Primary patient in hospital."""
    pat = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        uhid="UHID-2026-0001",
        first_name="Robert",
        last_name="Chase",
        name="Robert Chase",
        mobile="555-0102",
        email="chase@example.com",
        age=35,
        gender="male",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(pat)
    db_session.commit()
    db_session.refresh(pat)
    return pat


@pytest.fixture(scope="function")
def patient_b(db_session: Session, hospital_b: Hospital) -> Patient:
    """Patient in secondary hospital."""
    pat = Patient(
        id=uuid.uuid4(),
        hospital_id=hospital_b.id,
        uhid="UHID-2026-0002",
        first_name="Allison",
        last_name="Cameron",
        name="Allison Cameron",
        mobile="555-0202",
        email="cameron@example.com",
        age=32,
        gender="female",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(pat)
    db_session.commit()
    db_session.refresh(pat)
    return pat


@pytest.fixture(scope="function")
def appointment_factory(db_session: Session, hospital: Hospital, doctor: HospitalUser, patient: Patient):
    """Factory fixture to create appointments with arbitrary status, date, and time."""
    def _create(
        status: AppointmentStatus = AppointmentStatus.scheduled,
        appointment_date: date | None = None,
        appointment_time: time | None = None,
        purpose: str = "General Consultation",
        queue_token: int | None = None,
        checked_in_at: datetime | None = None,
        custom_hospital: Hospital | None = None,
        custom_doctor: HospitalUser | None = None,
        custom_patient: Patient | None = None,
    ) -> Appointment:
        appt = Appointment(
            id=uuid.uuid4(),
            hospital_id=(custom_hospital or hospital).id,
            doctor_id=(custom_doctor or doctor).id,
            patient_id=(custom_patient or patient).id,
            appointment_date=appointment_date or date.today(),
            appointment_time=appointment_time or time(9, 30),
            purpose=purpose,
            status=status,
            queue_token=queue_token,
            checked_in_at=checked_in_at,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(appt)
        db_session.commit()
        db_session.refresh(appt)
        return appt

    return _create


@pytest.fixture(scope="function")
def vital_factory(db_session: Session, hospital: Hospital, patient: Patient):
    """Factory fixture to create valid VitalReading records."""
    def _create(
        appointment: Appointment,
        name: str = "Blood Pressure",
        result: str = "120/80",
        suitable_range: str = "90-120/60-80",
        recorded_by_name: str = "Staff",
        custom_hospital: Hospital | None = None,
        custom_patient: Patient | None = None,
        recorded_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> VitalReading:
        v = VitalReading(
            id=uuid.uuid4(),
            hospital_id=(custom_hospital or hospital).id,
            appointment_id=appointment.id,
            patient_id=(custom_patient or patient).id,
            name=name,
            result=result,
            suitable_range=suitable_range or "",
            recorded_by_name=recorded_by_name,
            recorded_at=recorded_at or datetime.now(timezone.utc),
            created_at=created_at or datetime.now(timezone.utc),
        )
        db_session.add(v)
        db_session.commit()
        db_session.refresh(v)
        return v

    return _create


@pytest.fixture(scope="function")
def auth_headers(hospital: Hospital) -> dict[str, str]:
    """Valid Bearer authorization header for staff user in primary hospital."""
    token = create_access_token({
        "sub": "nurse.joy@apollocity.com",
        "name": "Nurse Joy",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def auth_headers_b(hospital_b: Hospital) -> dict[str, str]:
    """Valid Bearer authorization header for staff user in secondary hospital."""
    token = create_access_token({
        "sub": "nurse.clara@fortisreg.com",
        "name": "Nurse Clara",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_b.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def admin_headers(hospital: Hospital) -> dict[str, str]:
    """Valid Bearer authorization header for hospital_admin role."""
    token = create_access_token({
        "sub": "admin@apollocity.com",
        "name": "Hospital Administrator",
        "role": "hospital_admin",
        "hospital_uuid": str(hospital.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def super_admin_headers() -> dict[str, str]:
    """Valid Bearer authorization header for super_admin role."""
    token = create_access_token({
        "sub": "superadmin@ultriontech.com",
        "name": "Global SuperAdmin",
        "role": "super_admin",
    })
    return {"Authorization": f"Bearer {token}"}
