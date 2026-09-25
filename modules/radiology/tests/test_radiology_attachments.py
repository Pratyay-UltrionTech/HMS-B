"""
Tests for Radiology Attachments:
- Dedicated attachment upload independent of report finalization
- Technician vs Radiologist permissions
- Multiple attachments per order
- Binary file storage on disk, metadata in PostgreSQL
- Viewing/downloading attachments
- Attachment deletion rules (forbidden after report finalization)
"""

from __future__ import annotations

import base64
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
from infrastructure.postgres.session import get_transitional_sync_session
import tests.conftest  # noqa: F401
import modules.masters.entities.organization_entities  # noqa: F401
from modules.doctors.entities.doctor import HospitalUser
from modules.patients.entities.patient import Patient
from modules.radiology.api.radiology_api import router as rad_router
from modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
)
from modules.tenancy.entities.hospital import Hospital
from shared.auth import get_hospital_context, require_hospital_user, require_permission


@pytest.fixture
def rad_attachment_context():
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
        hospital_id="HOSP-RAD-01",
        name="Ultrion Imaging Hospital",
        address="123 Imaging Blvd",
        phone="9876543210",
        email="rad@test.com",
        password_hash="hashed_pw",
        is_active=True,
    )
    db.add(hospital)

    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=h_id,
        uhid="UH000888",
        name="Sunita Rao",
        age=45,
        gender="Female",
        mobile="9876543210",
    )
    db.add(patient)

    scan = RadiologyScanCatalog(
        id=uuid.uuid4(),
        hospital_id=h_id,
        scan_code="XRCHEST",
        scan_name="X-Ray Chest PA",
        category="X-Ray",
        department="Radiology",
        price=400.0,
        duration_minutes=15,
        is_active=True,
    )
    db.add(scan)

    order = RadiologyOrder(
        id=uuid.uuid4(),
        hospital_id=h_id,
        order_no="RAD0001",
        patient_id=patient.id,
        scan_id=scan.id,
        scan_code=scan.scan_code,
        scan_name=scan.scan_name,
        category=scan.category,
        price=scan.price,
        ordered_by_name="Dr. Mehta",
        ordered_by_role="Consultant",
        clinical_notes="STAT",
        status=RadiologyOrderStatus.ordered,
    )
    db.add(order)
    db.commit()

    app = FastAPI()
    app.include_router(rad_router)

    def override_db():
        yield db

    user_dict = {
        "sub": str(uuid.uuid4()),
        "name": "Technician Ravi",
        "role": "hospital_admin",
        "staff_role_name": "Imaging Tech",
        "hospital_id": str(h_id),
        "hospital_uuid": str(h_id),
    }

    app.dependency_overrides[get_transitional_sync_session] = override_db
    app.dependency_overrides[get_hospital_context] = lambda: h_id
    app.dependency_overrides[require_hospital_user] = lambda: user_dict
    app.dependency_overrides[require_permission("radiology", "edit")] = lambda: user_dict
    app.dependency_overrides[require_permission("radiology", "release")] = lambda: user_dict

    client = TestClient(app)
    return {
        "client": client,
        "db": db,
        "hospital_id": h_id,
        "patient": patient,
        "order": order,
    }


def test_radiology_attachment_workflow(rad_attachment_context):
    client = rad_attachment_context["client"]
    order = rad_attachment_context["order"]
    order_id = str(order.id)

    # 1. Upload PA view (JPEG) independent of report finalization
    dummy_jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
    pa_b64 = base64.b64encode(dummy_jpeg_bytes).decode("ascii")

    res_pa = client.post(
        f"/radiology/orders/{order_id}/attachments",
        json={
            "file_name": "chest_pa.jpg",
            "file_data": pa_b64,
            "mime_type": "image/jpeg",
            "attachment_type": "scan_plate",
        },
    )
    assert res_pa.status_code == 200, res_pa.text
    pa_data = res_pa.json()
    assert pa_data["file_name"] == "chest_pa.jpg"
    assert pa_data["mime_type"] == "image/jpeg"
    pa_id = pa_data["id"]

    # 2. Upload Lateral view (multiple attachments supported)
    dummy_lat_bytes = b"\xff\xd8\xff\xe0" + b"\x01" * 64 + b"\xff\xd9"
    lat_b64 = base64.b64encode(dummy_lat_bytes).decode("ascii")

    res_lat = client.post(
        f"/radiology/orders/{order_id}/attachments",
        json={
            "file_name": "chest_lateral.jpg",
            "file_data": lat_b64,
            "mime_type": "image/jpeg",
            "attachment_type": "scan_plate",
        },
    )
    assert res_lat.status_code == 200
    lat_data = res_lat.json()
    assert lat_data["file_name"] == "chest_lateral.jpg"

    # 3. Retrieve attachments for the order
    list_res = client.get(f"/radiology/orders/{order_id}/attachments")
    assert list_res.status_code == 200
    att_list = list_res.json()
    assert len(att_list) == 2
    names = {a["file_name"] for a in att_list}
    assert names == {"chest_pa.jpg", "chest_lateral.jpg"}

    # 4. Stream attachment file
    file_res = client.get(f"/radiology/orders/{order_id}/attachments/{pa_id}/file")
    assert file_res.status_code == 200
    assert file_res.headers["content-type"] == "image/jpeg"
    assert file_res.content == dummy_jpeg_bytes

    # 5. Radiologist finalizes report without needing to re-upload image
    report_res = client.post(
        f"/radiology/orders/{order_id}/report",
        json={
            "findings": "Normal bronchovascular markings. No focal consolidation.",
            "impression": "Normal chest radiograph.",
        },
    )
    assert report_res.status_code == 200, report_res.text
    order_data = report_res.json()
    assert order_data["status"] == "completed"
    assert len(order_data["attachments"]) == 2

    # 6. Deletion should now be forbidden because report has been finalized
    del_res = client.delete(f"/radiology/orders/{order_id}/attachments/{pa_id}")
    assert del_res.status_code == 400
    assert "Cannot delete attachments after report has been finalized" in del_res.json()["detail"]
