"""
Integration test: recording vitals automatically runs the Clinical Decision rule
engine, so a matching active rule produces a clinical_alerts row without any manual
evaluate call (Feature 20 wiring into the Vitals module).

Self-contained on its own in-memory SQLite engine so it is isolated from the shared
test harness teardown and does not depend on the legacy ``app.models`` import.
"""

import os
import uuid
from datetime import date, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("ENV", "test")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

# Register all entities so Base.metadata is complete (mirrors tests/conftest.py).
import modules.tenancy.entities.hospital  # noqa: F401
import modules.doctors.entities.doctor  # noqa: F401
import modules.patients.entities.patient  # noqa: F401
import modules.appointments.entities.appointment  # noqa: F401
import modules.appointments.entities.enums  # noqa: F401
import modules.clinical_records.entities.clinical_record  # noqa: F401
import modules.vitals.entities.vital_reading  # noqa: F401
import modules.laboratory.entities.lab_entities  # noqa: F401
import modules.radiology.entities.radiology_entities  # noqa: F401
import modules.billing.entities.billing_entities  # noqa: F401
import modules.pharmacy.entities.pharmacy_entities  # noqa: F401
import modules.inpatient.entities.admission  # noqa: F401
import modules.beds.entities.bed  # noqa: F401
import modules.ot.entities.ot_entities  # noqa: F401
import modules.masters.entities.organization_entities  # noqa: F401
import modules.masters.entities.insurance_entities  # noqa: F401
import modules.blood_bank.entities.blood_bank_entities  # noqa: F401
import modules.ambulance.entities.ambulance_entities  # noqa: F401
import modules.equipment.entities.equipment_entities  # noqa: F401
import modules.inventory.entities.inventory_entities  # noqa: F401
import modules.patients.entities.allergy  # noqa: F401
import modules.pharmacy.entities.drug_interaction_entities  # noqa: F401
import shared.audit.entities.audit_log  # noqa: F401
import modules.clinical_decision.entities.clinical_decision_entities  # noqa: F401

from infrastructure.postgres.base import Base
from modules.tenancy.entities.hospital import Hospital
from modules.doctors.entities.doctor import HospitalUser
from modules.patients.entities.patient import Patient
from modules.appointments.entities.appointment import Appointment
from modules.appointments.entities.enums import AppointmentStatus
from modules.clinical_decision.entities.clinical_decision_entities import (
    ClinicalAlert,
    ClinicalAlertSeverity,
    ClinicalRule,
    ClinicalRuleCategory,
)
from modules.vitals.actions.create_vitals_action import CreateVitalsAction
from modules.vitals.db.vitals_repository import VitalsRepository
from modules.vitals.contracts.vitals_contracts import VitalBatchCreate, VitalItemCreate


@pytest.fixture(scope="function")
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _seed(db: Session):
    hosp = Hospital(
        id=uuid.uuid4(),
        hospital_id="H1",
        name="Test Hospital",
        address="Addr",
        phone="1",
        email=f"h{uuid.uuid4().hex[:8]}@test.com",
        password_hash="x",
    )
    db.add(hosp)
    doc = HospitalUser(
        id=uuid.uuid4(),
        hospital_id=hosp.id,
        role_id=uuid.uuid4(),
        name="Dr X",
        phone="1",
        email=f"d{uuid.uuid4().hex[:8]}@test.com",
        password_hash="x",
        specialization="Med",
    )
    db.add(doc)
    pat = Patient(
        id=uuid.uuid4(),
        hospital_id=hosp.id,
        name="Jane",
        uhid="U1",
        mobile="9999999999",
    )
    db.add(pat)
    db.flush()
    return hosp, doc, pat


def _make_rule(db: Session, hospital_id, code: str) -> ClinicalRule:
    rule = ClinicalRule(
        hospital_id=hospital_id,
        rule_code=code,
        name="Elevated Heart Rate & Fever Alert",
        category=ClinicalRuleCategory.vitals_alert,
        severity=ClinicalAlertSeverity.critical,
        rule_definition={
            "logic": "AND",
            "conditions": [
                {"name": "heart_rate", "operator": "gte", "value": 110},
                {"name": "temperature", "operator": "gte", "value": 101.0},
            ],
        },
        recommendation="Evaluate for systemic infection or tachycardia response.",
        is_active=True,
    )
    db.add(rule)
    db.commit()
    return rule


def _make_appt(db: Session, hosp: Hospital, doc: HospitalUser, pat: Patient) -> Appointment:
    appt = Appointment(
        id=uuid.uuid4(),
        hospital_id=hosp.id,
        doctor_id=doc.id,
        patient_id=pat.id,
        appointment_date=date.today(),
        appointment_time=time(9, 30),
        purpose="Visit",
        visit_type="OPD",
        status=AppointmentStatus.scheduled,
    )
    db.add(appt)
    db.commit()
    return appt


def _record_vitals(db: Session, hosp: Hospital, appt: Appointment, hr: str, temp: str) -> None:
    repo = VitalsRepository(db=db, hospital_id=hosp.id)
    action = CreateVitalsAction(repo)
    user = {"name": "Nurse", "sub": "nurse@x.com", "role": "hospital_staff"}
    payload = VitalBatchCreate(
        appointment_id=appt.id,
        items=[
            VitalItemCreate(name="Heart Rate", result=hr),
            VitalItemCreate(name="Temperature", result=temp),
        ],
    )
    action.execute(payload, user)


def test_vitals_save_auto_creates_clinical_alert(db: Session):
    hosp, doc, pat = _seed(db)
    rule = _make_rule(db, hosp.id, "RULE_AUTO_1")
    appt = _make_appt(db, hosp, doc, pat)

    _record_vitals(db, hosp, appt, "125", "102.4")

    db.expire_all()
    alert = (
        db.query(ClinicalAlert)
        .filter_by(patient_id=pat.id, hospital_id=hosp.id, rule_id=rule.id)
        .first()
    )
    assert alert is not None
    assert alert.status.value == "active"
    assert alert.severity == ClinicalAlertSeverity.critical
    assert alert.title == rule.name


def test_vitals_save_no_alert_when_conditions_not_met(db: Session):
    hosp, doc, pat = _seed(db)
    rule = _make_rule(db, hosp.id, "RULE_SAFE_1")
    appt = _make_appt(db, hosp, doc, pat)

    _record_vitals(db, hosp, appt, "78", "98.6")

    db.expire_all()
    alert = (
        db.query(ClinicalAlert)
        .filter_by(patient_id=pat.id, hospital_id=hosp.id, rule_id=rule.id)
        .first()
    )
    assert alert is None
