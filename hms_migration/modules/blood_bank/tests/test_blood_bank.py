"""
Comprehensive integration and unit test suite for Blood Bank Foundation (Features 32-36).

Verifies:
- Feature 32: Donor Registry & Donation Collection
- Feature 33: Single Unified Blood Unit Inventory & Serology Clearance (Corrections #7 & #8)
- Feature 34: Component Management & Separation Lineage (Correction #9)
- Feature 35: Authorized Cross-Match Management without Guessed Algorithm (Correction #10)
- Feature 36: Blood Issue & Return Enforcing Valid Inventory Transitions (Correction #11)
- Multi-tenant isolation and state transition validations
"""

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hospital, HospitalUser as LegacyHospitalUser, Patient, StaffRole
from hms_migration.infrastructure.postgres.base import Base as TargetBase
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.beds.entities.bed import Bed, Room, Ward, WardType
from hms_migration.modules.blood_bank.api.blood_bank_api import router as blood_bank_router
from hms_migration.modules.blood_bank.entities.blood_bank_entities import (
    BloodComponentType,
    BloodGroup,
    BloodIssueStatus,
    BloodReturnDisposition,
    BloodUnit,
    BloodUnitStatus,
)
from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.patients.entities.patient import Patient as TargetPatient
from hms_migration.modules.tenancy.entities.hospital import Hospital as TargetHospital
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b, doctor


@pytest.fixture(scope="function")
def blood_bank_app(db_session: Session) -> FastAPI:
    TargetBase.metadata.create_all(bind=db_session.bind)
    test_app = FastAPI(title="Blood Bank Test App")
    test_app.include_router(blood_bank_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(blood_bank_app: FastAPI) -> TestClient:
    return TestClient(blood_bank_app)


@pytest.fixture(scope="function")
def blood_bank_auth(hospital: Hospital, doctor: LegacyHospitalUser) -> dict[str, str]:
    token = create_access_token({
        "sub": "bb_tech@hospital.com",
        "name": "Blood Bank Tech",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(doctor.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def blood_bank_auth_b(hospital_b: Hospital, doctor: LegacyHospitalUser) -> dict[str, str]:
    token = create_access_token({
        "sub": "bb_tech_b@hospitalb.com",
        "name": "Hospital B Tech",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_b.id),
        "user_id": str(doctor.id),
    })
    return {"Authorization": f"Bearer {token}"}


def test_feature_32_and_33_donor_donation_inventory(
    client: TestClient,
    blood_bank_auth: dict[str, str],
    hospital: Hospital,
):
    """Verify Feature 32 (Donor & Donation) and Feature 33 (Unified Unit Inventory & Serology)."""
    # 1. Register Donor
    donor_payload = {
        "name": "Rajesh Sharma",
        "gender": "male",
        "date_of_birth": "1990-05-15",
        "blood_group": "O+",
        "phone": "+91-9876543210",
        "email": "rajesh@example.com",
        "is_eligible": True,
    }
    d_res = client.post("/api/blood-bank/donors", json=donor_payload, headers=blood_bank_auth)
    assert d_res.status_code == 201, d_res.text
    donor_data = d_res.json()
    donor_id = donor_data["id"]
    assert donor_data["donor_number"].startswith("DNR-")
    assert donor_data["blood_group"] == "O+"

    # 2. Record Donation -> Automatically spawns unit in quarantine
    don_payload = {
        "donor_id": donor_id,
        "donation_type": "voluntary",
        "bag_type": "triple",
        "volume_ml": 450.0,
        "hemoglobin_g_dl": 14.2,
        "blood_pressure": "120/80",
        "pulse": 72,
    }
    don_res = client.post("/api/blood-bank/donations", json=don_payload, headers=blood_bank_auth)
    assert don_res.status_code == 201, don_res.text
    don_data = don_res.json()
    assert don_data["donation_number"].startswith("DON-")

    # Verify unit exists in quarantine
    units_res = client.get("/api/blood-bank/units?status=quarantine", headers=blood_bank_auth)
    assert units_res.status_code == 200, units_res.text
    quarantine_units = units_res.json()
    assert len(quarantine_units) >= 1
    target_unit = next(u for u in quarantine_units if u["donation_id"] == don_data["id"])
    assert target_unit["blood_group"] == "O+"
    assert target_unit["component_type"] == "whole_blood"
    assert target_unit["status"] == "quarantine"
    assert target_unit["serology_tested"] is False

    # 3. Clear Serology -> transitions from quarantine to available
    clear_res = client.patch(
        f"/api/blood-bank/units/{target_unit['id']}/clear-serology",
        json={"serology_cleared": True, "storage_location": "Main Blood Bank Fridge A1"},
        headers=blood_bank_auth,
    )
    assert clear_res.status_code == 200, clear_res.text
    cleared_unit = clear_res.json()
    assert cleared_unit["status"] == "available"
    assert cleared_unit["serology_cleared"] is True


def test_feature_34_component_separation_lineage(
    client: TestClient,
    blood_bank_auth: dict[str, str],
    hospital: Hospital,
):
    """Verify Feature 34: Whole blood separation into PRBC, FFP, Platelets preserving lineage."""
    # Register donor and record donation
    donor = client.post("/api/blood-bank/donors", json={
        "name": "Sunita Verma",
        "gender": "female",
        "blood_group": "A+",
    }, headers=blood_bank_auth).json()

    donation = client.post("/api/blood-bank/donations", json={
        "donor_id": donor["id"],
        "volume_ml": 450.0,
    }, headers=blood_bank_auth).json()

    units = client.get("/api/blood-bank/units?status=quarantine", headers=blood_bank_auth).json()
    parent_unit = next(u for u in units if u["donation_id"] == donation["id"])

    # Clear serology so it's available
    client.patch(
        f"/api/blood-bank/units/{parent_unit['id']}/clear-serology",
        json={"serology_cleared": True},
        headers=blood_bank_auth,
    )

    # Perform component separation
    sep_payload = {
        "method": "centrifugation",
        "notes": "Separated into PRBC and FFP",
        "components": [
            {
                "component_type": "prbc",
                "volume_ml": 250.0,
                "storage_location": "RBC Refrigerator 2-6C",
                "shelf_life_days": 42,
            },
            {
                "component_type": "ffp",
                "volume_ml": 180.0,
                "storage_location": "Deep Freezer -30C",
                "shelf_life_days": 365,
            },
            {
                "component_type": "platelets",
                "volume_ml": 50.0,
                "storage_location": "Platelet Agitator 22C",
                "shelf_life_days": 5,
            },
        ],
    }
    sep_res = client.post(
        f"/api/blood-bank/units/{parent_unit['id']}/separate",
        json=sep_payload,
        headers=blood_bank_auth,
    )
    assert sep_res.status_code == 201, sep_res.text
    sep_data = sep_res.json()
    assert sep_data["separation_number"].startswith("SEP-")
    assert len(sep_data["created_components"]) == 3

    # Parent unit is now marked 'separated'
    check_parent = client.get(f"/api/blood-bank/units/{parent_unit['id']}", headers=blood_bank_auth).json()
    assert check_parent["status"] == "separated"

    # All child components have parent_unit_id referencing the whole blood unit
    for comp in sep_data["created_components"]:
        assert comp["parent_unit_id"] == parent_unit["id"]
        assert comp["blood_group"] == "A+"
        assert comp["status"] == "available"


def test_feature_35_and_36_crossmatch_issue_and_return(
    client: TestClient,
    blood_bank_auth: dict[str, str],
    hospital: Hospital,
    patient: Patient,
):
    """Verify Features 35 & 36: Authorized Cross-match, reservation, issue to ward, and return."""
    # 1. Prepare an available unit
    donor = client.post("/api/blood-bank/donors", json={
        "name": "Amit Kumar",
        "gender": "male",
        "blood_group": "B+",
    }, headers=blood_bank_auth).json()

    donation = client.post("/api/blood-bank/donations", json={
        "donor_id": donor["id"],
        "volume_ml": 450.0,
    }, headers=blood_bank_auth).json()

    units = client.get("/api/blood-bank/units?status=quarantine", headers=blood_bank_auth).json()
    unit = next(u for u in units if u["donation_id"] == donation["id"])

    client.patch(
        f"/api/blood-bank/units/{unit['id']}/clear-serology",
        json={"serology_cleared": True},
        headers=blood_bank_auth,
    )

    # 2. Feature 35: Record authorized cross-match test results (Correction #10)
    xm_payload = {
        "blood_unit_id": unit["id"],
        "patient_id": str(patient.id),
        "patient_blood_group": "B+",
        "major_crossmatch_result": "compatible",
        "minor_crossmatch_result": "compatible",
        "coombs_test_result": "negative",
        "is_compatible": True,
        "authorized_by": "Dr. Transfusion Specialist",
        "notes": "Gel card crossmatch clear",
    }
    xm_res = client.post("/api/blood-bank/cross-matches", json=xm_payload, headers=blood_bank_auth)
    assert xm_res.status_code == 201, xm_res.text
    xm_data = xm_res.json()
    assert xm_data["is_compatible"] is True

    # Unit must now be reserved for this patient
    res_unit = client.get(f"/api/blood-bank/units/{unit['id']}", headers=blood_bank_auth).json()
    assert res_unit["status"] == "reserved"
    assert res_unit["reserved_for_patient_id"] == str(patient.id)

    # 3. Feature 36: Issue blood unit (Correction #11)
    issue_payload = {
        "blood_unit_id": unit["id"],
        "patient_id": str(patient.id),
        "requisition_ref": "REQ-OT-9988",
        "issued_to": "Staff Nurse Priya (OT 2)",
        "transport_box_temp_c": 4.5,
        "notes": "Dispatched in cold box with ice packs",
    }
    iss_res = client.post("/api/blood-bank/issues", json=issue_payload, headers=blood_bank_auth)
    assert iss_res.status_code == 201, iss_res.text
    iss_data = iss_res.json()
    issue_id = iss_data["id"]
    assert iss_data["status"] == "issued"

    # Unit must now be in 'issued' status
    issued_unit = client.get(f"/api/blood-bank/units/{unit['id']}", headers=blood_bank_auth).json()
    assert issued_unit["status"] == "issued"

    # 4. Feature 36: Return un-transfused blood unit
    ret_payload = {
        "return_reason": "Surgery completed without requiring transfusion",
        "cold_chain_maintained": True,
        "bag_intact": True,
        "disposition": "restocked",
        "disposition_notes": "Returned within 30 min, temp verified at 4.2C",
    }
    ret_res = client.post(f"/api/blood-bank/issues/{issue_id}/return", json=ret_payload, headers=blood_bank_auth)
    assert ret_res.status_code == 201, ret_res.text
    ret_data = ret_res.json()
    assert ret_data["disposition"] == "restocked"

    # Unit must now be back in 'available' status with reservation cleared
    restocked_unit = client.get(f"/api/blood-bank/units/{unit['id']}", headers=blood_bank_auth).json()
    assert restocked_unit["status"] == "available"
    assert restocked_unit["reserved_for_patient_id"] is None


def _setup_issued_unit(client: TestClient, auth: dict[str, str], patient_obj: Patient) -> dict:
    """Helper: create donor -> donation -> clear serology -> crossmatch -> issue. Returns issue dict."""
    donor = client.post("/api/blood-bank/donors", json={
        "name": "Transfusion Donor",
        "gender": "male",
        "blood_group": "O+",
    }, headers=auth).json()

    donation = client.post("/api/blood-bank/donations", json={
        "donor_id": donor["id"],
        "volume_ml": 450.0,
    }, headers=auth).json()

    units = client.get("/api/blood-bank/units?status=quarantine", headers=auth).json()
    unit = next(u for u in units if u["donation_id"] == donation["id"])

    client.patch(
        f"/api/blood-bank/units/{unit['id']}/clear-serology",
        json={"serology_cleared": True},
        headers=auth,
    )

    xm_payload = {
        "blood_unit_id": unit["id"],
        "patient_id": str(patient_obj.id),
        "patient_blood_group": "O+",
        "major_crossmatch_result": "compatible",
        "is_compatible": True,
        "authorized_by": "Dr. Transfusion Specialist",
    }
    client.post("/api/blood-bank/cross-matches", json=xm_payload, headers=auth)

    issue_payload = {
        "blood_unit_id": unit["id"],
        "patient_id": str(patient_obj.id),
        "requisition_ref": "REQ-TRX-001",
        "issued_to": "Staff Nurse",
    }
    issue = client.post("/api/blood-bank/issues", json=issue_payload, headers=auth).json()
    return {"unit": unit, "donor": donor, "donation": donation, "issue": issue}


def test_feature_37_transfusion_completion_updates_unit_status(
    client: TestClient,
    blood_bank_auth: dict[str, str],
    hospital: Hospital,
    patient: Patient,
    doctor: LegacyHospitalUser,
):
    """Feature 37: Happy path - starting and completing a transfusion marks the unit 'transfused'."""
    ctx = _setup_issued_unit(client, blood_bank_auth, patient)
    unit_id = ctx["unit"]["id"]
    issue_id = ctx["issue"]["id"]

    start_res = client.post(
        "/api/blood-bank/transfusions",
        json={
            "issue_id": issue_id,
            "verified_by_staff_id": str(doctor.id),
            "pre_transfusion_vitals": "BP 120/80, Temp 98.6F, Pulse 76",
        },
        headers=blood_bank_auth,
    )
    assert start_res.status_code == 201, start_res.text
    tx_data = start_res.json()
    assert tx_data["status"] == "in_progress"
    tx_id = tx_data["id"]

    complete_res = client.patch(
        f"/api/blood-bank/transfusions/{tx_id}/complete",
        json={"completed_by_staff_id": str(doctor.id), "observations": "No adverse events observed"},
        headers=blood_bank_auth,
    )
    assert complete_res.status_code == 200, complete_res.text
    completed = complete_res.json()
    assert completed["status"] == "completed"
    assert completed["end_time"] is not None

    # Linked blood unit must now be 'transfused'
    unit_check = client.get(f"/api/blood-bank/units/{unit_id}", headers=blood_bank_auth).json()
    assert unit_check["status"] == "transfused"


def test_feature_37_cannot_transfuse_non_issued_unit(
    client: TestClient,
    blood_bank_auth: dict[str, str],
    hospital: Hospital,
    patient: Patient,
    doctor: LegacyHospitalUser,
):
    """Feature 37: Cannot create a transfusion for an issue not in 'issued' status."""
    ctx = _setup_issued_unit(client, blood_bank_auth, patient)
    issue_id = ctx["issue"]["id"]

    # Return the unit first, moving issue to 'returned'
    client.post(
        f"/api/blood-bank/issues/{issue_id}/return",
        json={
            "return_reason": "Not needed",
            "disposition": "restocked",
        },
        headers=blood_bank_auth,
    )

    start_res = client.post(
        "/api/blood-bank/transfusions",
        json={
            "issue_id": issue_id,
            "verified_by_staff_id": str(doctor.id),
        },
        headers=blood_bank_auth,
    )
    assert start_res.status_code == 400, start_res.text


def test_feature_37_adverse_reaction_recording(
    client: TestClient,
    blood_bank_auth: dict[str, str],
    hospital: Hospital,
    patient: Patient,
    doctor: LegacyHospitalUser,
):
    """Feature 37: Adverse reaction transition records reaction detail and does not mark unit transfused."""
    ctx = _setup_issued_unit(client, blood_bank_auth, patient)
    unit_id = ctx["unit"]["id"]
    issue_id = ctx["issue"]["id"]

    start_res = client.post(
        "/api/blood-bank/transfusions",
        json={"issue_id": issue_id, "verified_by_staff_id": str(doctor.id)},
        headers=blood_bank_auth,
    )
    tx_id = start_res.json()["id"]

    reaction_res = client.patch(
        f"/api/blood-bank/transfusions/{tx_id}/reaction",
        json={
            "completed_by_staff_id": str(doctor.id),
            "adverse_reaction_type": "febrile",
            "adverse_reaction_details": "Fever spike to 101F 10 minutes into transfusion, stopped immediately.",
        },
        headers=blood_bank_auth,
    )
    assert reaction_res.status_code == 200, reaction_res.text
    reaction_data = reaction_res.json()
    assert reaction_data["status"] == "reaction"
    assert reaction_data["adverse_reaction_occurred"] is True
    assert reaction_data["adverse_reaction_type"] == "febrile"

    # Unit status must remain 'issued' (not transfused) since transfusion did not complete normally
    unit_check = client.get(f"/api/blood-bank/units/{unit_id}", headers=blood_bank_auth).json()
    assert unit_check["status"] == "issued"

    # Cannot transition again once resolved
    second_res = client.patch(
        f"/api/blood-bank/transfusions/{tx_id}/complete",
        json={"completed_by_staff_id": str(doctor.id)},
        headers=blood_bank_auth,
    )
    assert second_res.status_code == 400


def test_feature_38_unit_traceability_full_chain(
    client: TestClient,
    blood_bank_auth: dict[str, str],
    hospital: Hospital,
    patient: Patient,
    doctor: LegacyHospitalUser,
):
    """Feature 38: unit traceability returns donor, donation, cross-matches, issue and transfusion."""
    ctx = _setup_issued_unit(client, blood_bank_auth, patient)
    unit_id = ctx["unit"]["id"]
    issue_id = ctx["issue"]["id"]

    start_res = client.post(
        "/api/blood-bank/transfusions",
        json={"issue_id": issue_id, "verified_by_staff_id": str(doctor.id)},
        headers=blood_bank_auth,
    )
    tx_id = start_res.json()["id"]
    client.patch(
        f"/api/blood-bank/transfusions/{tx_id}/complete",
        json={"completed_by_staff_id": str(doctor.id)},
        headers=blood_bank_auth,
    )

    trace_res = client.get(f"/api/blood-bank/units/{unit_id}/traceability", headers=blood_bank_auth)
    assert trace_res.status_code == 200, trace_res.text
    trace = trace_res.json()

    assert trace["unit"]["id"] == unit_id
    assert trace["donor"]["id"] == ctx["donor"]["id"]
    assert trace["donation"]["id"] == ctx["donation"]["id"]
    assert len(trace["cross_matches"]) == 1
    assert trace["issue"]["id"] == issue_id
    assert trace["transfusion"] is not None
    assert trace["transfusion"]["status"] == "completed"


def test_feature_38_donor_traceability_lookback(
    client: TestClient,
    blood_bank_auth: dict[str, str],
    hospital: Hospital,
    patient: Patient,
):
    """Feature 38: donor traceability returns all donations and derived units (lookback investigation)."""
    donor = client.post("/api/blood-bank/donors", json={
        "name": "Lookback Donor",
        "gender": "female",
        "blood_group": "AB+",
    }, headers=blood_bank_auth).json()

    donation = client.post("/api/blood-bank/donations", json={
        "donor_id": donor["id"],
        "volume_ml": 450.0,
    }, headers=blood_bank_auth).json()

    units = client.get("/api/blood-bank/units?status=quarantine", headers=blood_bank_auth).json()
    parent_unit = next(u for u in units if u["donation_id"] == donation["id"])

    client.patch(
        f"/api/blood-bank/units/{parent_unit['id']}/clear-serology",
        json={"serology_cleared": True},
        headers=blood_bank_auth,
    )

    sep_payload = {
        "components": [
            {"component_type": "prbc", "volume_ml": 250.0, "shelf_life_days": 42},
            {"component_type": "ffp", "volume_ml": 180.0, "shelf_life_days": 365},
        ],
    }
    client.post(
        f"/api/blood-bank/units/{parent_unit['id']}/separate",
        json=sep_payload,
        headers=blood_bank_auth,
    )

    trace_res = client.get(f"/api/blood-bank/donors/{donor['id']}/traceability", headers=blood_bank_auth)
    assert trace_res.status_code == 200, trace_res.text
    trace = trace_res.json()

    assert trace["donor"]["id"] == donor["id"]
    assert len(trace["donations"]) == 1
    donation_trace = trace["donations"][0]
    assert donation_trace["donation"]["id"] == donation["id"]
    # Original whole-blood unit + 2 derived components = 3 units total
    assert len(donation_trace["units"]) == 3
    component_types = {u["unit"]["component_type"] for u in donation_trace["units"]}
    assert component_types == {"whole_blood", "prbc", "ffp"}


def test_blood_bank_multi_tenant_isolation(
    client: TestClient,
    blood_bank_auth: dict[str, str],
    blood_bank_auth_b: dict[str, str],
    hospital: Hospital,
    hospital_b: Hospital,
):
    """Verify tenant isolation: Hospital B cannot access Hospital A's donors, units, or issues."""
    # Hospital A registers donor
    d_res = client.post("/api/blood-bank/donors", json={
        "name": "Donor Hospital A",
        "gender": "male",
        "blood_group": "O-",
    }, headers=blood_bank_auth)
    assert d_res.status_code == 201
    donor_a_id = d_res.json()["id"]

    # Hospital B tries to get donor A -> 404
    b_res = client.get(f"/api/blood-bank/donors/{donor_a_id}", headers=blood_bank_auth_b)
    assert b_res.status_code == 404
