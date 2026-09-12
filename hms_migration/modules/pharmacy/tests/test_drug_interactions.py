"""
Unit and integration tests for Feature 17: Drug Interaction Alerts Engine
and Feature 18: High-Alert Medication Control master verification.

Tests:
1. Create and list clinical drug interaction rules
2. Duplicate rule conflict prevention (409)
3. Evaluate safe drug combinations (no alerts)
4. Evaluate hazardous drug combinations (contraindicated & major alerts)
5. Verify High-Alert fields on Medicine master entity
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.models import Hospital
from hms_migration.infrastructure.postgres.base import Base
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.patients.entities.patient import Patient as TargetPatient
from hms_migration.modules.pharmacy.api.drug_interaction_api import router as interaction_router
from hms_migration.modules.tenancy.entities.hospital import Hospital as TargetHospital
from hms_migration.modules.pharmacy.entities.drug_interaction_entities import (
    DrugInteractionRule,
    InteractionSeverity,
)
from hms_migration.modules.pharmacy.entities.pharmacy_entities import (
    Medicine,
    MedicineCategory,
    MedicineType,
    MedicineUnit,
)
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, doctor, hospital


@pytest.fixture(scope="function")
def interaction_app(db_session: Session) -> FastAPI:
    test_app = FastAPI(title="Interaction Test App")
    test_app.include_router(interaction_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(interaction_app: FastAPI) -> TestClient:
    return TestClient(interaction_app)


@pytest.fixture(scope="function")
def staff_headers(hospital: Hospital) -> dict[str, str]:
    token = create_access_token(
        data={
            "sub": "pharmacist.anil@hospital.test",
            "name": "Anil Sharma",
            "email": "pharmacist.anil@hospital.test",
            "role": "hospital_staff",
            "staff_role_name": "pharmacist",
            "hospital_uuid": str(hospital.id),
            "user_id": str(uuid.uuid4()),
        }
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Hospital-ID": str(hospital.id),
    }


def test_feature_17_drug_interaction_rules_and_evaluation(
    client: TestClient, db_session: Session, staff_headers: dict[str, str]
):
    Base.metadata.create_all(bind=db_session.get_bind())

    # 1. Create Rule: Warfarin + Aspirin (Contraindicated)
    rule_1 = {
        "drug_a": "Warfarin",
        "drug_b": "Aspirin",
        "severity": "contraindicated",
        "mechanism": "Concomitant use substantially increases gastrointestinal bleeding and major hemorrhage risk.",
        "clinical_recommendation": "Avoid combination. Use alternative analgesic or reduce antiplatelet therapy under INR monitoring.",
    }
    res = client.post("/api/pharmacy/interactions", json=rule_1, headers=staff_headers)
    assert res.status_code == 201, res.text
    r1_data = res.json()
    assert r1_data["drug_a"] == "Warfarin"
    assert r1_data["severity"] == "contraindicated"

    # 2. Create Rule: Enalapril + Spironolactone (Major)
    rule_2 = {
        "drug_a": "Enalapril",
        "drug_b": "Spironolactone",
        "severity": "major",
        "mechanism": "Concurrent ACE inhibitor and potassium-sparing diuretic promotes severe life-threatening hyperkalemia.",
        "clinical_recommendation": "Monitor serum potassium and renal function within 1 week of co-prescription.",
    }
    res = client.post("/api/pharmacy/interactions", json=rule_2, headers=staff_headers)
    assert res.status_code == 201, res.text

    # 3. Duplicate rule prevention (409 Conflict)
    res = client.post("/api/pharmacy/interactions", json=rule_1, headers=staff_headers)
    assert res.status_code == 409
    assert "already exists" in res.json()["detail"]

    # 4. List interaction rules
    res = client.get("/api/pharmacy/interactions", headers=staff_headers)
    assert res.status_code == 200
    rules = res.json()
    assert len(rules) >= 2

    # 5. Check Interactions: Safe combination
    safe_req = {
        "medicines": ["Paracetamol 500mg", "Amoxicillin 500mg", "Pantoprazole 40mg"]
    }
    res = client.post("/api/pharmacy/check-interactions", json=safe_req, headers=staff_headers)
    assert res.status_code == 200
    safe_report = res.json()
    assert safe_report["has_conflicts"] is False
    assert len(safe_report["alerts"]) == 0
    assert safe_report["requires_clinical_override"] is False

    # 6. Check Interactions: Hazardous combination (Warfarin + Aspirin)
    hazardous_req = {
        "medicines": ["Warfarin 5mg Tab", "Atorvastatin 20mg Tab", "Aspirin 75mg Gastro-resistant Tab"]
    }
    res = client.post("/api/pharmacy/check-interactions", json=hazardous_req, headers=staff_headers)
    assert res.status_code == 200
    haz_report = res.json()
    assert haz_report["has_conflicts"] is True
    assert haz_report["highest_severity"] == "contraindicated"
    assert haz_report["requires_clinical_override"] is True
    assert len(haz_report["alerts"]) == 1
    alert = haz_report["alerts"][0]
    assert "Warfarin" in alert["drug_a"] or "Warfarin" in alert["drug_b"]
    assert "Aspirin" in alert["drug_a"] or "Aspirin" in alert["drug_b"]
    assert "hemorrhage risk" in alert["mechanism"]


def test_feature_18_high_alert_medication_master_fields(
    db_session: Session, hospital: Hospital
):
    Base.metadata.create_all(bind=db_session.get_bind())

    # Create category
    cat = MedicineCategory(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        name="Critical Care / Emergency Meds",
        is_active=True,
    )
    db_session.add(cat)
    db_session.flush()

    # Create high-alert medicine with PINCH category and dual sign-off requirement
    high_alert_med = Medicine(
        id=uuid.uuid4(),
        hospital_id=hospital.id,
        category_id=cat.id,
        medicine_name="Potassium Chloride 15% Injection",
        generic_name="Potassium Chloride",
        brand_name="Potchlor IV",
        manufacturer="Neon Laboratories",
        medicine_type=MedicineType.injection,
        strength="15% w/v",
        unit=MedicineUnit.ampoule,
        is_high_alert=True,
        high_alert_category="Concentrated Electrolytes",
        requires_dual_signoff=True,
        is_active=True,
    )
    db_session.add(high_alert_med)
    db_session.commit()
    db_session.refresh(high_alert_med)

    assert high_alert_med.is_high_alert is True
    assert high_alert_med.high_alert_category == "Concentrated Electrolytes"
    assert high_alert_med.requires_dual_signoff is True
