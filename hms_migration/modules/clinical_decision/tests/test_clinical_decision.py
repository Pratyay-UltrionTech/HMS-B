"""
Unit and integration tests for Clinical Decision Support and Medication Safety (Features 19–21).

Tests:
1. Feature 19: Medication Reconciliation creation, discrepancy detection, and item updates
2. Feature 20: Configurable multi-parameter rule creation, patient evaluation, and alert acknowledgment
3. Feature 21: Clinical Order Set creation, typed item catalog linkage, and 1-click orchestration
4. Tenant Isolation: Reconciliations, rules, alerts, and order sets strictly isolated across hospitals
"""

from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hospital, HospitalUser as LegacyHospitalUser, Patient, StaffRole
from hms_migration.infrastructure.postgres.base import Base as TargetBase
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.clinical_decision.api.clinical_decision_api import router as clinical_decision_router
from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.entities.admission import Admission
from hms_migration.modules.laboratory.entities.lab_entities import LabOrder, LabOrderItem, LabTestCatalog, LabSampleType
from hms_migration.modules.patients.entities.patient import Patient as TargetPatient
from hms_migration.modules.pharmacy.entities.pharmacy_entities import Medicine
from hms_migration.modules.radiology.entities.radiology_entities import RadiologyOrder, RadiologyScanCatalog
from hms_migration.modules.tenancy.entities.hospital import Hospital as TargetHospital
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b, doctor


@pytest.fixture(scope="function")
def clinical_app(db_session: Session) -> FastAPI:
    TargetBase.metadata.create_all(bind=db_session.bind)
    test_app = FastAPI(title="Clinical Decision Test App")
    test_app.include_router(clinical_decision_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(clinical_app: FastAPI) -> TestClient:
    return TestClient(clinical_app)


@pytest.fixture(scope="function")
def doctor_auth(hospital: Hospital, doctor: LegacyHospitalUser) -> dict[str, str]:
    token = create_access_token({
        "sub": "doctor@hospital.com",
        "name": "Dr. House",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(doctor.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def staff_auth_b(hospital_b: Hospital) -> dict[str, str]:
    token = create_access_token({
        "sub": "other@hospital.com",
        "name": "Staff B",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_b.id),
        "user_id": str(uuid4()),
    })
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Feature 19: Medication Reconciliation Tests
# ---------------------------------------------------------------------------

def test_feature_19_medication_reconciliation_workflow(
    client: TestClient, doctor_auth: dict[str, str], patient: Patient
):
    # 1. Create reconciliation encounter with 2 medications (one having an omission discrepancy)
    create_payload = {
        "patient_id": str(patient.id),
        "stage": "admission",
        "notes": "Admission medication reconciliation from home regimen",
        "items": [
            {
                "medicine_name": "Metformin",
                "dosage": "500mg",
                "frequency": "BD",
                "route": "oral",
                "source": "home_medication",
                "action": "continue",
                "discrepancy_type": "none",
                "resolved": True,
            },
            {
                "medicine_name": "Aspirin",
                "dosage": "75mg",
                "frequency": "OD",
                "route": "oral",
                "source": "home_medication",
                "action": "hold",
                "discrepancy_type": "omission",
                "discrepancy_reason": "Pre-op hold for scheduled surgery",
                "clinical_override_justification": "Patient scheduled for OR, antiplatelet held for 48h",
                "resolved": False,
            },
        ],
    }

    res = client.post("/api/clinical/medication-reconciliation", json=create_payload, headers=doctor_auth)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["status"] == "discrepancy_flagged"
    assert len(data["items"]) == 2
    rec_id = data["id"]

    # 2. Retrieve reconciliation record
    get_res = client.get(f"/api/clinical/medication-reconciliation/{rec_id}", headers=doctor_auth)
    assert get_res.status_code == 200
    assert get_res.json()["id"] == rec_id

    # 3. Resolve discrepancy and mark completed
    update_payload = {
        "mark_completed": True,
        "reconciliation_notes": "All discrepancies reconciled by attending physician",
        "items": [
            {
                "medicine_name": "Metformin",
                "dosage": "500mg",
                "frequency": "BD",
                "route": "oral",
                "source": "home_medication",
                "action": "continue",
                "discrepancy_type": "none",
                "resolved": True,
            },
            {
                "medicine_name": "Aspirin",
                "dosage": "75mg",
                "frequency": "OD",
                "route": "oral",
                "source": "home_medication",
                "action": "hold",
                "discrepancy_type": "omission",
                "discrepancy_reason": "Pre-op hold for scheduled surgery",
                "clinical_override_justification": "Patient scheduled for OR, antiplatelet held for 48h",
                "resolved": True,
            },
        ],
    }
    put_res = client.put(f"/api/clinical/medication-reconciliation/{rec_id}/items", json=update_payload, headers=doctor_auth)
    assert put_res.status_code == 200
    assert put_res.json()["status"] == "completed"


# ---------------------------------------------------------------------------
# Feature 20: Clinical Alerts Tests
# ---------------------------------------------------------------------------

def test_feature_20_clinical_rules_and_alerts(
    client: TestClient, doctor_auth: dict[str, str], patient: Patient
):
    # 1. Create a hospital-configurable multi-parameter rule (Correction #2)
    rule_payload = {
        "rule_code": f"RULE_VITALS_{uuid4().hex[:6]}",
        "name": "Elevated Heart Rate & Fever Alert",
        "category": "vitals_alert",
        "severity": "critical",
        "rule_definition": {
            "logic": "AND",
            "conditions": [
                {"name": "heart_rate", "operator": "gte", "value": 110},
                {"name": "temperature", "operator": "gte", "value": 101.0},
            ],
        },
        "recommendation": "Evaluate for systemic infection or tachycardia response; obtain blood cultures if indicated.",
        "is_active": True,
    }
    rule_res = client.post("/api/clinical/rules", json=rule_payload, headers=doctor_auth)
    assert rule_res.status_code == 201, rule_res.text
    rule_data = rule_res.json()
    assert rule_data["rule_code"] == rule_payload["rule_code"].upper()

    # 2. Evaluate patient when conditions DO NOT meet criteria
    safe_eval = client.post(
        f"/api/clinical/alerts/evaluate/{patient.id}",
        json={"additional_observations": {"heart_rate": 78, "temperature": 98.6}},
        headers=doctor_auth,
    )
    assert safe_eval.status_code == 200
    # No critical alert should be generated
    assert not any(a["title"] == rule_payload["name"] for a in safe_eval.json())

    # 3. Evaluate patient when conditions DO meet multi-parameter criteria
    crit_eval = client.post(
        f"/api/clinical/alerts/evaluate/{patient.id}",
        json={"additional_observations": {"heart_rate": 125, "temperature": 102.4}},
        headers=doctor_auth,
    )
    assert crit_eval.status_code == 200
    alerts = crit_eval.json()
    matched = [a for a in alerts if a["title"] == rule_payload["name"]]
    assert len(matched) >= 1
    alert_id = matched[0]["id"]
    assert matched[0]["status"] == "active"
    assert matched[0]["severity"] == "critical"

    # 4. Acknowledge the alert
    ack_res = client.put(
        f"/api/clinical/alerts/{alert_id}/acknowledge",
        json={"status": "acknowledged", "clinical_override_reason": "IV fluids started, antipyretic given"},
        headers=doctor_auth,
    )
    assert ack_res.status_code == 200
    assert ack_res.json()["status"] == "acknowledged"

    # 5. Resolve the alert (Finding F-02 valid transition)
    res_alert = client.put(
        f"/api/clinical/alerts/{alert_id}/acknowledge",
        json={"status": "resolved", "clinical_override_reason": "Vitals normalized"},
        headers=doctor_auth,
    )
    assert res_alert.status_code == 200
    assert res_alert.json()["status"] == "resolved"

    # 6. Reject invalid transition: resolved cannot transition back to active (Finding F-02 rejection)
    invalid_res = client.put(
        f"/api/clinical/alerts/{alert_id}/acknowledge",
        json={"status": "active"},
        headers=doctor_auth,
    )
    assert invalid_res.status_code == 400
    assert "Invalid alert state transition" in invalid_res.json()["detail"]


# ---------------------------------------------------------------------------
# Feature 21: Clinical Order Sets Tests
# ---------------------------------------------------------------------------

def test_feature_21_clinical_order_sets_orchestration(
    client: TestClient, doctor_auth: dict[str, str], patient: Patient, db_session: Session, hospital: Hospital
):
    # Setup prerequisite catalog items in laboratory and radiology
    lab_test = LabTestCatalog(
        hospital_id=hospital.id,
        test_code=f"CBC_{uuid4().hex[:6]}",
        test_name="Complete Blood Count",
        department="Hematology",
        price=350.0,
        sample_type=LabSampleType.blood,
    )
    rad_scan = RadiologyScanCatalog(
        hospital_id=hospital.id,
        scan_code=f"CXR_{uuid4().hex[:6]}",
        scan_name="Chest X-Ray PA View",
        category="X-Ray",
        price=500.0,
    )
    db_session.add(lab_test)
    db_session.add(rad_scan)
    db_session.commit()

    # 1. Create standardized Clinical Order Set (Correction #3: real typed relations)
    order_set_payload = {
        "code": f"ADMISSION_BUNDLE_{uuid4().hex[:6]}",
        "name": "Standard Inpatient Admission Protocol",
        "category": "Inpatient",
        "description": "Routine admission laboratory and imaging bundle",
        "is_active": True,
        "items": [
            {
                "order_type": "lab",
                "lab_test_catalog_id": str(lab_test.id),
                "item_name": "Complete Blood Count",
                "instructions": "Fasting blood sample",
                "is_mandatory": True,
                "sort_order": 1,
            },
            {
                "order_type": "radiology",
                "radiology_scan_catalog_id": str(rad_scan.id),
                "item_name": "Chest X-Ray PA",
                "instructions": "Bedside portable or radiology suite",
                "is_mandatory": True,
                "sort_order": 2,
            },
            {
                "order_type": "nursing",
                "item_name": "Record vitals q4h & intake/output chart",
                "instructions": "Maintain strict I/O",
                "is_mandatory": True,
                "sort_order": 3,
            },
            {
                "order_type": "medication",
                "item_name": "Paracetamol 500mg Tab",
                "dosage_or_instruction": "1 tablet",
                "frequency": "TDS pc",
                "instructions": "Administer after meals",
                "is_mandatory": True,
                "sort_order": 4,
            },
        ],
    }

    set_res = client.post("/api/clinical/order-sets", json=order_set_payload, headers=doctor_auth)
    assert set_res.status_code == 201, set_res.text
    order_set_data = set_res.json()
    set_id = order_set_data["id"]
    assert len(order_set_data["items"]) == 4

    # 2. 1-Click apply order set for patient
    apply_res = client.post(
        f"/api/clinical/order-sets/{set_id}/apply",
        json={"patient_id": str(patient.id), "clinical_notes": "Admitted with fever workup"},
        headers=doctor_auth,
    )
    assert apply_res.status_code == 200, apply_res.text
    apply_data = apply_res.json()
    assert apply_data["lab_order_id"] is not None
    assert len(apply_data["radiology_order_ids"]) == 1
    assert apply_data["nursing_instructions_logged"] == 1
    # Finding F-01: canonical Prescription created when doctor context present
    assert apply_data["prescription_id"] is not None
    from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
    from uuid import UUID as PyUUID
    rx_record = db_session.query(Prescription).filter(Prescription.id == PyUUID(apply_data["prescription_id"])).first()
    assert rx_record is not None
    assert rx_record.patient_id == patient.id
    assert "Paracetamol" in rx_record.medicines


# ---------------------------------------------------------------------------
# Multi-tenant Isolation Test
# ---------------------------------------------------------------------------

def test_clinical_decision_multi_tenant_isolation(
    client: TestClient, doctor_auth: dict[str, str], staff_auth_b: dict[str, str], patient: Patient
):
    # Create rule in Hospital A
    rule_res = client.post(
        "/api/clinical/rules",
        json={
            "rule_code": f"RULE_T_{uuid4().hex[:6]}",
            "name": "Hospital A Rule",
            "category": "preventive_care",
            "severity": "info",
            "rule_definition": {"conditions": [{"name": "age", "operator": "gte", "value": 65}]},
        },
        headers=doctor_auth,
    )
    assert rule_res.status_code == 201

    # Hospital B queries rules — should not see Hospital A rule
    b_rules = client.get("/api/clinical/rules", headers=staff_auth_b)
    assert b_rules.status_code == 200
    assert not any(r["name"] == "Hospital A Rule" for r in b_rules.json())

    # Hospital B cannot evaluate patient from Hospital A
    cross_eval = client.post(f"/api/clinical/alerts/evaluate/{patient.id}", headers=staff_auth_b)
    assert cross_eval.status_code == 404
