"""
Automated unit tests for Laboratory Specimen domain, collection workflows,
multi-specimen container mapping, specimen labels, and reprint tracking.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
import tests.conftest  # noqa: F401
import modules.masters.entities.organization_entities  # noqa: F401
from modules.doctors.entities.doctor import HospitalUser
from modules.laboratory.api.laboratory_api import router as lab_router
from modules.laboratory.contracts.lab_contracts import (
    LabOrderCreate,
    LabTestCreate,
    SampleCollectRequest,
    SpecimenCollectRequest,
)
from modules.laboratory.entities.lab_entities import (
    LabOrderStatus,
    LabSampleType,
    LabSpecimenStatus,
    LabTestCatalog,
)
from modules.laboratory.services.lab_specimen_service import (
    determine_specimen_requirement,
    CONTAINER_EDTA,
    CONTAINER_SERUM,
    CONTAINER_URINE,
)
from modules.laboratory.services.specimen_label_service import render_specimen_label_html
from modules.patients.entities.patient import Patient
from modules.tenancy.entities.hospital import Hospital
from shared.auth import get_hospital_context, require_hospital_user, require_permission


@pytest.fixture
def specimen_test_context():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    h_id = uuid.uuid4()
    hospital = Hospital(
        id=h_id,
        hospital_id="HOSP-SPEC-01",
        name="Ultrion Test Hospital",
        address="123 Lab Street",
        phone="9876543210",
        email="lab@test.com",
        password_hash="hashed_pw",
        is_active=True,
    )
    db.add(hospital)

    staff_id = uuid.uuid4()
    staff_name = "Staff Lab Tech"

    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=h_id,
        uhid="UH000999",
        name="Arjun Sharma",
        age=38,
        gender="Male",
        mobile="9876543210",
    )
    db.add(patient)

    # Add catalogue tests for multi-specimen testing
    cbc = LabTestCatalog(
        id=uuid.uuid4(),
        hospital_id=h_id,
        test_code="CBC",
        test_name="Complete Blood Count",
        department="Haematology",
        price=350.0,
        sample_type=LabSampleType.blood,
        tat_hours=6,
        is_active=True,
    )
    hba1c = LabTestCatalog(
        id=uuid.uuid4(),
        hospital_id=h_id,
        test_code="HBA1C",
        test_name="HbA1c Glycated",
        department="Biochemistry",
        price=500.0,
        sample_type=LabSampleType.blood,
        tat_hours=24,
        is_active=True,
    )
    lft = LabTestCatalog(
        id=uuid.uuid4(),
        hospital_id=h_id,
        test_code="LFT",
        test_name="Liver Function Test",
        department="Biochemistry",
        price=600.0,
        sample_type=LabSampleType.blood,
        tat_hours=12,
        is_active=True,
    )
    urine = LabTestCatalog(
        id=uuid.uuid4(),
        hospital_id=h_id,
        test_code="URINE",
        test_name="Urine Routine Examination",
        department="Clinical Pathology",
        price=150.0,
        sample_type=LabSampleType.urine,
        tat_hours=6,
        is_active=True,
    )
    db.add_all([cbc, hba1c, lft, urine])
    db.commit()

    app = FastAPI()
    app.include_router(lab_router)

    def override_db():
        yield db

    user_dict = {
        "sub": str(staff_id),
        "id": str(staff_id),
        "name": staff_name,
        "role": "hospital_admin",
        "staff_role_name": "Lab Technician",
        "hospital_id": str(h_id),
        "hospital_uuid": str(h_id),
    }

    app.dependency_overrides[get_transitional_sync_session] = override_db
    app.dependency_overrides[get_hospital_context] = lambda: h_id
    app.dependency_overrides[require_hospital_user] = lambda: user_dict
    app.dependency_overrides[require_permission("laboratory", "edit")] = lambda: user_dict
    app.dependency_overrides[require_permission("laboratory", "validate")] = lambda: user_dict

    client = TestClient(app)
    return {
        "client": client,
        "db": db,
        "hospital_id": h_id,
        "patient": patient,
        "staff_id": staff_id,
        "cbc": cbc,
        "hba1c": hba1c,
        "lft": lft,
        "urine": urine,
    }


def test_specimen_container_mapping():
    # CBC and HbA1c belong to EDTA blood
    st1, c1 = determine_specimen_requirement(LabSampleType.blood, "CBC", "Complete Blood Count", "Haematology")
    assert st1 == LabSampleType.blood
    assert c1 == CONTAINER_EDTA

    st2, c2 = determine_specimen_requirement(LabSampleType.blood, "HBA1C", "HbA1c", "Biochemistry")
    assert st2 == LabSampleType.blood
    assert c2 == CONTAINER_EDTA

    # LFT belongs to Serum SST
    st3, c3 = determine_specimen_requirement(LabSampleType.blood, "LFT", "Liver Function Test", "Biochemistry")
    assert st3 == LabSampleType.blood
    assert c3 == CONTAINER_SERUM

    # Urine belongs to Sterile Urine Cup
    st4, c4 = determine_specimen_requirement(LabSampleType.urine, "URINE", "Urine Routine", "Clinical Pathology")
    assert st4 == LabSampleType.urine
    assert c4 == CONTAINER_URINE


def test_multi_specimen_order_creation_and_partial_collection(specimen_test_context):
    client = specimen_test_context["client"]
    patient = specimen_test_context["patient"]
    cbc = specimen_test_context["cbc"]
    hba1c = specimen_test_context["hba1c"]
    lft = specimen_test_context["lft"]
    urine = specimen_test_context["urine"]

    # Order 4 tests spanning 3 containers:
    # 1. EDTA Tube: CBC + HbA1c
    # 2. Serum Tube: LFT
    # 3. Sterile Urine Cup: Urine Routine
    res = client.post(
        "/laboratory/orders",
        json={
            "patient_id": str(patient.id),
            "test_ids": [str(cbc.id), str(hba1c.id), str(lft.id), str(urine.id)],
            "clinical_notes": "Emergency STAT test panel",
        },
    )
    assert res.status_code == 201, res.text
    order_data = res.json()
    order_id = order_data["id"]

    # Verify specimens were automatically resolved and created
    specimens = order_data["specimens"]
    assert len(specimens) == 3, f"Expected 3 distinct containers, got {len(specimens)}"

    # Find the EDTA, Serum, and Urine specimens
    edta_spec = next(s for s in specimens if "EDTA" in s["container_type"])
    serum_spec = next(s for s in specimens if "Serum" in s["container_type"])
    urine_spec = next(s for s in specimens if "Urine" in s["container_type"])

    # CBC and HbA1c should share the EDTA specimen
    assert len(edta_spec["items"]) == 2
    edta_test_codes = {i["test_code"] for i in edta_spec["items"]}
    assert edta_test_codes == {"CBC", "HBA1C"}

    # Unique specimen numbers
    assert edta_spec["specimen_no"] != serum_spec["specimen_no"]
    assert edta_spec["specimen_no"] != urine_spec["specimen_no"]
    assert edta_spec["status"] == "pending"

    # Mark billing charge as paid to simulate cashier settlement prior to phlebotomy collection
    from modules.billing.entities.billing_entities import BillingCharge, BillingChargeStatus
    charge = specimen_test_context["db"].query(BillingCharge).filter(BillingCharge.source_id == uuid.UUID(order_id)).first()
    if charge:
        charge.status = BillingChargeStatus.paid
        charge.amount_paid = charge.net_amount
        specimen_test_context["db"].commit()

    # PARTIAL COLLECTION: Collect only the EDTA specimen
    collect_res = client.post(
        f"/laboratory/orders/{order_id}/specimens/{edta_spec['id']}/collect",
        json={"collected_by": "Nurse Sunita", "collection_remarks": "Left cubital vein"},
    )
    assert collect_res.status_code == 200, collect_res.text
    updated_order = collect_res.json()

    # Order status should remain 'ordered' because Serum and Urine are still pending!
    assert updated_order["status"] == "ordered"

    # Check that EDTA specimen is collected
    updated_edta = next(s for s in updated_order["specimens"] if s["id"] == edta_spec["id"])
    assert updated_edta["status"] == "collected"
    assert updated_edta["collected_by"] == "Nurse Sunita"

    # Guard: Collecting the same specimen again should fail
    dup_res = client.post(
        f"/laboratory/orders/{order_id}/specimens/{edta_spec['id']}/collect",
        json={"collected_by": "Nurse Sunita"},
    )
    assert dup_res.status_code == 400

    # COMPLETE COLLECTION: Collect Serum and Urine
    client.post(
        f"/laboratory/orders/{order_id}/specimens/{serum_spec['id']}/collect",
        json={"collected_by": "Nurse Sunita"},
    )
    final_res = client.post(
        f"/laboratory/orders/{order_id}/specimens/{urine_spec['id']}/collect",
        json={"collected_by": "Nurse Sunita"},
    )
    assert final_res.status_code == 200
    final_order = final_res.json()

    # Now that ALL specimens are collected, order status becomes 'sample_collected'
    assert final_order["status"] == "sample_collected"


def test_specimen_label_rendering_and_reprint(specimen_test_context):
    client = specimen_test_context["client"]
    patient = specimen_test_context["patient"]
    cbc = specimen_test_context["cbc"]

    # Create order
    res = client.post(
        "/laboratory/orders",
        json={
            "patient_id": str(patient.id),
            "test_ids": [str(cbc.id)],
            "clinical_notes": "STAT CBC test",
        },
    )
    order_data = res.json()
    order_id = order_data["id"]
    specimen = order_data["specimens"][0]
    specimen_id = specimen["id"]

    # Mark billing charge as paid to simulate cashier settlement prior to collection
    from modules.billing.entities.billing_entities import BillingCharge, BillingChargeStatus
    charge = specimen_test_context["db"].query(BillingCharge).filter(BillingCharge.source_id == uuid.UUID(order_id)).first()
    if charge:
        charge.status = BillingChargeStatus.paid
        charge.amount_paid = charge.net_amount
        specimen_test_context["db"].commit()

    # Collect specimen
    collect_res = client.post(
        f"/laboratory/orders/{order_id}/specimens/{specimen_id}/collect",
        json={"collected_by": "Tech Amit"},
    )
    assert collect_res.status_code == 200, collect_res.text
    collected_order = collect_res.json()
    assert collected_order["id"] == order_id
    # Collection returns an order, whose ID is different from the specimen ID.
    # The printable label route must receive the nested specimen's ID.
    collected_specimen = next(s for s in collected_order["specimens"] if s["id"] == specimen_id)
    assert collected_specimen["status"] == "collected"
    assert collected_specimen["barcode_value"] == collected_specimen["specimen_no"]

    # Refreshing order state preserves collection and its specimen identifier.
    refreshed = client.get(f"/laboratory/orders/{order_id}")
    assert refreshed.status_code == 200
    assert next(s for s in refreshed.json()["specimens"] if s["id"] == specimen_id)["status"] == "collected"

    # Fetch printable specimen label HTML
    label_res = client.get(f"/laboratory/specimens/{specimen_id}/label")
    assert label_res.status_code == 200
    html_content = label_res.text

    # Verify 50x25mm label features
    assert "50mm 25mm" in html_content
    assert specimen["specimen_no"] in html_content
    assert patient.name.upper() in html_content
    assert patient.uhid.upper() in html_content
    assert order_data["order_no"].upper() in html_content
    assert "CBC" in html_content
    assert "<svg" in html_content  # Barcode present

    # Already collected specimens remain printable, and invalid IDs remain 404.
    assert client.get(f"/laboratory/specimens/{specimen_id}/label").status_code == 200
    assert client.get(f"/laboratory/specimens/{uuid.uuid4()}/label").status_code == 404

    # Test permanent reprint tracking
    assert specimen["reprint_count"] == 0
    reprint_res = client.post(f"/laboratory/specimens/{specimen_id}/reprint")
    assert reprint_res.status_code == 200
    reprint_data = reprint_res.json()
    assert reprint_data["reprint_count"] == 1
    assert reprint_data["last_reprinted_by"] == "Staff Lab Tech"
    assert reprint_data["last_reprinted_at"] is not None

    # Reprinting preserves the EXACT same specimen identifier
    assert reprint_data["specimen_no"] == specimen["specimen_no"]
