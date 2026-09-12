"""
Comprehensive integration and unit test suite for Insurance & TPA Lifecycle (Features 22-31).

Verifies:
- Feature 22: Canonical InsuranceProvider in masters
- Feature 23: Patient policy creation & admission policy linkage (Correction #4)
- Feature 24: Eligibility verification
- Feature 25 & 26: Pre-auth request, queries, responses, and decisions
- Feature 27 & 28: Claim preparation referencing existing BillingInvoice and submission tracking (Correction #5)
- Feature 29: Claim dispute and resubmission
- Feature 30: Cashless settlement split and BillingPayment creation (Correction #5)
- Feature 31: TPA performance reporting
- Strict multi-tenant isolation and state transition enforcement
"""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hospital, HospitalUser as LegacyHospitalUser, Patient, StaffRole
from hms_migration.infrastructure.postgres.base import Base as TargetBase
from hms_migration.infrastructure.postgres.session import get_transitional_sync_session
from hms_migration.modules.appointments.entities.appointment import Appointment
from hms_migration.modules.billing.entities.billing_entities import (
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPayment,
)
from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.beds.entities.bed import Bed, Room, Ward, WardType
from hms_migration.modules.clinical_records.entities.clinical_record import Prescription
from hms_migration.modules.doctors.entities.doctor import HospitalUser
from hms_migration.modules.inpatient.entities.admission import Admission, AdmissionStatus
from hms_migration.modules.insurance.api.insurance_api import router as insurance_router
from hms_migration.modules.insurance.entities.insurance_entities import (
    ClaimStatus,
    DisputeStatus,
    PolicyStatus,
    PreAuthStatus,
    SettlementStatus,
    SubmissionStatus,
)
from hms_migration.modules.masters.entities.insurance_entities import InsuranceProvider
from hms_migration.modules.patients.entities.patient import Patient as TargetPatient
from hms_migration.modules.tenancy.entities.hospital import Hospital as TargetHospital
from hms_migration.shared.auth.jwt import create_access_token
from tests.conftest import db_session, hospital, hospital_b, patient, patient_b, doctor


@pytest.fixture(scope="function")
def insurance_app(db_session: Session) -> FastAPI:
    TargetBase.metadata.create_all(bind=db_session.bind)
    test_app = FastAPI(title="Insurance Lifecycle Test App")
    test_app.include_router(insurance_router, prefix="/api")

    def _override_db():
        yield db_session

    test_app.dependency_overrides[get_transitional_sync_session] = _override_db
    return test_app


@pytest.fixture(scope="function")
def client(insurance_app: FastAPI) -> TestClient:
    return TestClient(insurance_app)


@pytest.fixture(scope="function")
def staff_auth(hospital: Hospital, doctor: LegacyHospitalUser) -> dict[str, str]:
    token = create_access_token({
        "sub": "billing_staff@hospital.com",
        "name": "Insurance Coordinator",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital.id),
        "user_id": str(doctor.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def staff_auth_b(hospital_b: Hospital, doctor: LegacyHospitalUser) -> dict[str, str]:
    token = create_access_token({
        "sub": "staff_b@hospitalb.com",
        "name": "Hospital B Staff",
        "role": "hospital_staff",
        "hospital_uuid": str(hospital_b.id),
        "user_id": str(doctor.id),
    })
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def insurance_provider(db_session: Session, hospital: Hospital) -> InsuranceProvider:
    provider = InsuranceProvider(
        hospital_id=hospital.id,
        code="TPA-STAR-01",
        name="Star Health Insurance",
        category="Private TPA",
        pre_auth_sla_hours=4,
        tariff_discount_percent=10.0,
        contact_person="Claims Desk",
        phone="1800-425-2255",
        email="claims@starhealth.in",
        status="active",
        is_active=True,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)
    return provider


@pytest.fixture(scope="function")
def patient_admission(db_session: Session, hospital: Hospital, patient: Patient) -> Admission:
    now = datetime.now(timezone.utc)
    ward = Ward(
        hospital_id=hospital.id,
        name=f"Medical Ward {uuid4().hex[:4]}",
        ward_type=WardType.general,
    )
    db_session.add(ward)
    db_session.flush()

    room = Room(
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_code=f"R-{uuid4().hex[:4]}",
    )
    db_session.add(room)
    db_session.flush()

    bed = Bed(
        hospital_id=hospital.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_code=f"B-{uuid4().hex[:4]}",
    )
    db_session.add(bed)
    db_session.flush()

    admission = Admission(
        hospital_id=hospital.id,
        patient_id=patient.id,
        ward_id=ward.id,
        room_id=room.id,
        bed_id=bed.id,
        status=AdmissionStatus.admitted,
        admitted_at=now,
    )
    db_session.add(admission)
    db_session.commit()
    db_session.refresh(admission)
    return admission


@pytest.fixture(scope="function")
def billing_invoice(db_session: Session, hospital: Hospital, patient: Patient) -> BillingInvoice:
    invoice = BillingInvoice(
        hospital_id=hospital.id,
        patient_id=patient.id,
        invoice_number=f"INV-{uuid4().hex[:6].upper()}",
        invoice_date=date.today(),
        subtotal=50000.0,
        discount_amount=0.0,
        tax_amount=0.0,
        grand_total=50000.0,
        status=BillingInvoiceStatus.generated,
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def test_feature_23_and_24_policy_and_eligibility(
    client: TestClient,
    staff_auth: dict[str, str],
    hospital: Hospital,
    patient: Patient,
    insurance_provider: InsuranceProvider,
    patient_admission: Admission,
):
    """Verify Feature 23 (Patient Policy & Admission Linkage) and Feature 24 (Eligibility Verification)."""
    # 1. Create Patient Policy
    policy_payload = {
        "patient_id": str(patient.id),
        "provider_id": str(insurance_provider.id),
        "policy_number": "POL-998877",
        "member_id": "MEM-12345",
        "corporate_name": "Acme Corp",
        "sum_insured": 500000.0,
        "balance_sum_insured": 450000.0,
        "valid_from": str(date.today() - timedelta(days=30)),
        "valid_to": str(date.today() + timedelta(days=335)),
        "co_pay_percent": 10.0,
        "room_rent_capping_per_day": 5000.0,
        "icu_capping_per_day": 10000.0,
    }
    p_res = client.post("/api/insurance/policies", json=policy_payload, headers=staff_auth)
    assert p_res.status_code == 201, p_res.text
    policy_data = p_res.json()
    policy_id = policy_data["id"]
    assert policy_data["policy_number"] == "POL-998877"
    assert policy_data["status"] == "active"

    # 2. Link policy to Admission (Correction #4)
    link_payload = {
        "admission_id": str(patient_admission.id),
        "patient_policy_id": policy_id,
        "is_primary": True,
        "notes": "Corporate group policy",
    }
    l_res = client.post("/api/insurance/admissions/link-policy", json=link_payload, headers=staff_auth)
    assert l_res.status_code == 201, l_res.text
    link_data = l_res.json()
    link_id = link_data["id"]
    assert link_data["is_primary"] is True
    assert link_data["pre_auth_status"] == "not_required"

    # 3. Feature 24: Eligibility verification
    elig_payload = {
        "patient_policy_id": policy_id,
        "admission_id": str(patient_admission.id),
        "verification_source": "portal",
        "is_eligible": True,
        "reference_number": "ELIG-PORTAL-001",
        "coverage_details": "Co-pay 10%, Room Rent 5000 cap",
        "remarks": "Active and in good standing",
    }
    e_res = client.post("/api/insurance/eligibility-checks", json=elig_payload, headers=staff_auth)
    assert e_res.status_code == 201, e_res.text
    elig_data = e_res.json()
    assert elig_data["is_eligible"] is True
    assert elig_data["reference_number"] == "ELIG-PORTAL-001"


def test_feature_25_and_26_pre_auth_lifecycle(
    client: TestClient,
    staff_auth: dict[str, str],
    hospital: Hospital,
    patient: Patient,
    insurance_provider: InsuranceProvider,
    patient_admission: Admission,
):
    """Verify Features 25 & 26: Pre-Auth Request, Queries, Responses, and Approval."""
    # Setup policy & admission link
    pol = client.post("/api/insurance/policies", json={
        "patient_id": str(patient.id),
        "provider_id": str(insurance_provider.id),
        "policy_number": "POL-554433",
        "member_id": "MEM-554433",
        "sum_insured": 300000.0,
        "balance_sum_insured": 300000.0,
        "valid_from": str(date.today() - timedelta(days=10)),
        "valid_to": str(date.today() + timedelta(days=350)),
    }, headers=staff_auth).json()

    link = client.post("/api/insurance/admissions/link-policy", json={
        "admission_id": str(patient_admission.id),
        "patient_policy_id": pol["id"],
        "is_primary": True,
    }, headers=staff_auth).json()

    # 1. Create Pre-Auth Request
    pa_payload = {
        "admission_policy_id": link["id"],
        "request_type": "initial",
        "provisional_diagnosis": "Acute Appendicitis",
        "treatment_plan": "Emergency Appendectomy",
        "estimated_cost": 85000.0,
        "requested_amount": 80000.0,
    }
    pa_res = client.post("/api/insurance/pre-auths", json=pa_payload, headers=staff_auth)
    assert pa_res.status_code == 201, pa_res.text
    pa_data = pa_res.json()
    pa_id = pa_data["id"]
    assert pa_data["status"] == "submitted"
    assert pa_data["pre_auth_number"].startswith("PA-")

    # 2. TPA raises query
    q_res = client.post(f"/api/insurance/pre-auths/{pa_id}/queries", json={
        "query_type": "clinical_clarification",
        "query_text": "Please provide USG Abdomen report copy",
    }, headers=staff_auth)
    assert q_res.status_code == 201, q_res.text
    q_data = q_res.json()
    q_id = q_data["id"]

    # Pre-auth status transitioned to query_raised
    check_pa = client.get(f"/api/insurance/pre-auths/{pa_id}", headers=staff_auth).json()
    assert check_pa["status"] == "query_raised"

    # 3. Hospital responds to query
    resp_res = client.post(f"/api/insurance/pre-auth-queries/{q_id}/respond", json={
        "response_text": "USG report uploaded to portal, confirms acute inflammation.",
    }, headers=staff_auth)
    assert resp_res.status_code == 200, resp_res.text

    # Pre-auth status restored to submitted
    check_pa2 = client.get(f"/api/insurance/pre-auths/{pa_id}", headers=staff_auth).json()
    assert check_pa2["status"] == "submitted"

    # 4. TPA issues approval
    dec_res = client.patch(f"/api/insurance/pre-auths/{pa_id}/decision", json={
        "status": "approved",
        "approved_amount": 75000.0,
        "approval_letter_ref": "AL-STAR-8899",
    }, headers=staff_auth)
    assert dec_res.status_code == 200, dec_res.text
    dec_data = dec_res.json()
    assert dec_data["status"] == "approved"
    assert dec_data["approved_amount"] == 75000.0


def test_feature_27_to_30_claims_and_settlement(
    client: TestClient,
    db_session: Session,
    staff_auth: dict[str, str],
    hospital: Hospital,
    patient: Patient,
    insurance_provider: InsuranceProvider,
    patient_admission: Admission,
    billing_invoice: BillingInvoice,
):
    """Verify Features 27-30: Claim preparation, submission tracking, rejection/dispute, cashless settlement."""
    # Setup policy & admission link
    pol = client.post("/api/insurance/policies", json={
        "patient_id": str(patient.id),
        "provider_id": str(insurance_provider.id),
        "policy_number": "POL-CLAIM-100",
        "member_id": "MEM-CLAIM-100",
        "sum_insured": 200000.0,
        "balance_sum_insured": 200000.0,
        "valid_from": str(date.today() - timedelta(days=10)),
        "valid_to": str(date.today() + timedelta(days=350)),
    }, headers=staff_auth).json()

    link = client.post("/api/insurance/admissions/link-policy", json={
        "admission_id": str(patient_admission.id),
        "patient_policy_id": pol["id"],
        "is_primary": True,
    }, headers=staff_auth).json()

    # 1. Feature 27: Create Claim Dossier referencing existing billing invoice (Correction #5)
    claim_payload = {
        "admission_policy_id": link["id"],
        "billing_invoice_id": str(billing_invoice.id),
        "claimed_amount": 50000.0,
    }
    c_res = client.post("/api/insurance/claims", json=claim_payload, headers=staff_auth)
    assert c_res.status_code == 201, c_res.text
    claim_data = c_res.json()
    claim_id = claim_data["id"]
    assert claim_data["status"] == "draft"

    # Verify checklist -> transitions to prepared
    verify_res = client.patch(f"/api/insurance/claims/{claim_id}/verify-checklist", json={
        "checklist_verified": True,
        "discharge_summary_verified": True,
        "investigation_reports_verified": True,
        "final_bill_verified": True,
    }, headers=staff_auth)
    assert verify_res.status_code == 200
    assert verify_res.json()["status"] == "prepared"

    # 2. Feature 28: Submit Claim
    sub_res = client.post(f"/api/insurance/claims/{claim_id}/submit", json={
        "submission_mode": "portal",
        "tracking_reference": "PORTAL-REF-9988",
    }, headers=staff_auth)
    assert sub_res.status_code == 201, sub_res.text
    sub_data = sub_res.json()
    sub_id = sub_data["id"]
    assert sub_data["status"] == "submitted"

    # Acknowledge submission
    ack_res = client.patch(f"/api/insurance/submissions/{sub_id}/acknowledge", json={
        "tpa_ack_number": "TPA-ACK-0012",
    }, headers=staff_auth)
    assert ack_res.status_code == 200
    assert ack_res.json()["status"] == "acknowledged"

    # 3. Feature 29: Simulate rejection & dispute
    dec_res = client.patch(f"/api/insurance/submissions/{sub_id}/decision", json={
        "status": "rejected",
        "insurer_decision": "Query unfulfilled",
        "settled_amount": 0.0,
    }, headers=staff_auth)
    assert dec_res.status_code == 200

    # Dispute the rejection
    disp_res = client.post(f"/api/insurance/claims/{claim_id}/dispute", json={
        "submission_id": sub_id,
        "denial_code": "DEN-042",
        "denial_reason": "Missing pre-existing disease history",
        "dispute_rationale": "First time manifestation, enclosed physician certificate.",
    }, headers=staff_auth)
    assert disp_res.status_code == 201, disp_res.text
    disp_data = disp_res.json()
    disp_id = disp_data["id"]
    assert disp_data["status"] == "filed"

    # Resubmit disputed claim
    resub_res = client.post(f"/api/insurance/disputes/{disp_id}/resubmit", json={
        "resubmission_reference": "RESUB-001",
    }, headers=staff_auth)
    assert resub_res.status_code == 200
    assert resub_res.json()["status"] == "resubmitted"

    # 4. Feature 30: Cashless Settlement
    settle_payload = {
        "claim_id": claim_id,
        "approved_by_insurer_amount": 42000.0,
        "disallowed_deduction_amount": 5000.0,
        "tds_amount": 2000.0,
        "patient_copay_payable": 3000.0,
        "payment_reference": "NEFT-UTR-883719",
    }
    settle_res = client.post("/api/insurance/settlements", json=settle_payload, headers=staff_auth)
    assert settle_res.status_code == 201, settle_res.text
    settle_data = settle_res.json()
    assert settle_data["net_payable_by_insurer"] == 40000.0  # 42000 - 2000
    assert settle_data["settlement_status"] == "settled"
    assert settle_data["billing_payment_id"] is not None

    # Verify canonical billing payment was created in billing engine
    from uuid import UUID as PyUUID
    bp_id = settle_data["billing_payment_id"]
    billing_payment = db_session.query(BillingPayment).filter(BillingPayment.id == PyUUID(str(bp_id))).first()
    assert billing_payment is not None
    assert billing_payment.amount == 40000.0

    # 5. Feature 31: TPA Performance Report
    rep_res = client.get("/api/insurance/reports/tpa-performance", headers=staff_auth)
    assert rep_res.status_code == 200, rep_res.text
    report_data = rep_res.json()
    assert len(report_data["providers"]) >= 1
    tpa_sum = report_data["providers"][0]
    assert tpa_sum["provider_name"] == "Star Health Insurance"
    assert tpa_sum["total_claims"] == 1
    assert tpa_sum["settled_claims"] == 1


def test_insurance_multi_tenant_isolation(
    client: TestClient,
    staff_auth: dict[str, str],
    staff_auth_b: dict[str, str],
    patient: Patient,
    patient_b: Patient,
    insurance_provider: InsuranceProvider,
):
    """Verify tenant isolation: Hospital B cannot access Hospital A's policies or claims."""
    # Hospital A creates policy
    pol_res = client.post("/api/insurance/policies", json={
        "patient_id": str(patient.id),
        "provider_id": str(insurance_provider.id),
        "policy_number": "POL-TENANT-A",
        "member_id": "MEM-A",
        "sum_insured": 100000.0,
        "balance_sum_insured": 100000.0,
        "valid_from": str(date.today()),
        "valid_to": str(date.today() + timedelta(days=365)),
    }, headers=staff_auth)
    assert pol_res.status_code == 201

    # Hospital B tries to list policies for Patient A -> should return empty list (tenant isolated)
    b_res = client.get(f"/api/insurance/patients/{patient.id}/policies", headers=staff_auth_b)
    assert b_res.status_code == 200
    assert len(b_res.json()) == 0
