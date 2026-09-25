"""
Regression tests for radiology attachment upload contract.

Root cause: frontend sent `file_data_base64` while backend required `file_data`,
producing 422 Unprocessable Entity on POST /api/radiology/orders/{id}/attachments.
Backend now accepts both; frontend sends `file_data`.

Also verifies hospital_admin bypasses radiology edit permission.
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
from modules.patients.entities.patient import Patient
from modules.radiology.api.radiology_api import router as rad_router
from modules.radiology.contracts.radiology_contracts import RadAttachmentUploadRequest
from modules.radiology.entities.radiology_entities import (
    RadiologyOrder,
    RadiologyOrderStatus,
    RadiologyScanCatalog,
)
from modules.tenancy.entities.hospital import Hospital
from shared.auth import get_hospital_context, require_hospital_user, require_permission
from shared.auth.service import authorization


def _make_client(role="hospital_admin"):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    h_id = uuid.uuid4()
    db.add(Hospital(id=h_id, hospital_id="HOSP-REG-01", name="Reg", address="a",
                    phone="1", email="r@t.com", password_hash="x", is_active=True))
    patient = Patient(id=uuid.uuid4(), hospital_id=h_id, uhid="UHREG01",
                      name="Reg Patient", age=30, gender="Male", mobile="1")
    db.add(patient)
    scan = RadiologyScanCatalog(id=uuid.uuid4(), hospital_id=h_id, scan_code="XRCHEST",
                                scan_name="X-Ray Chest", category="X-Ray",
                                department="Radiology", price=400.0,
                                duration_minutes=15, is_active=True)
    db.add(scan)
    order = RadiologyOrder(id=uuid.uuid4(), hospital_id=h_id, order_no="RADREG01",
                           patient_id=patient.id, scan_id=scan.id,
                           scan_code=scan.scan_code, scan_name=scan.scan_name,
                           category=scan.category, price=scan.price,
                           ordered_by_name="Admin", ordered_by_role="Admin",
                           clinical_notes="STAT",
                           status=RadiologyOrderStatus.ordered)
    db.add(order)
    db.commit()

    app = FastAPI()
    app.include_router(rad_router)
    user_dict = {"sub": str(uuid.uuid4()), "name": "Admin", "role": role,
                 "hospital_id": str(h_id), "hospital_uuid": str(h_id)}

    app.dependency_overrides[get_transitional_sync_session] = lambda: db
    app.dependency_overrides[get_hospital_context] = lambda: h_id
    app.dependency_overrides[require_hospital_user] = lambda: user_dict
    app.dependency_overrides[require_permission("radiology", "edit")] = lambda: user_dict
    app.dependency_overrides[require_permission("radiology", "release")] = lambda: user_dict
    return TestClient(app), order


def test_contract_accepts_file_data():
    b64 = base64.b64encode(b"\xff\xd8\xff fake").decode()
    req = RadAttachmentUploadRequest(file_name="a.jpg", file_data=b64)
    assert req.file_data == b64


def test_contract_accepts_legacy_file_data_base64_alias():
    b64 = base64.b64encode(b"\xff\xd8\xff fake").decode()
    req = RadAttachmentUploadRequest(file_name="a.jpg", file_data_base64=b64)
    assert req.file_data == b64


def test_contract_rejects_missing_file_data():
    with pytest.raises(Exception):
        RadAttachmentUploadRequest(file_name="a.jpg")


def test_upload_with_legacy_alias_returns_200_and_persists():
    client, order = _make_client()
    b64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 32).decode()
    res = client.post(f"/radiology/orders/{order.id}/attachments",
                      json={"file_name": "legacy.jpg", "file_data_base64": b64,
                            "mime_type": "image/jpeg"})
    assert res.status_code == 200, res.text
    # Reload order: attachment must remain visible (no disappearance on refresh)
    list_res = client.get(f"/radiology/orders/{order.id}/attachments")
    assert list_res.status_code == 200
    assert any(a["file_name"] == "legacy.jpg" for a in list_res.json())
    order_res = client.get(f"/radiology/orders/{order.id}")
    assert order_res.status_code == 200
    assert any(a["file_name"] == "legacy.jpg" for a in order_res.json()["attachments"])


def test_admin_bypass_radiology_edit():
    admin = {"role": "hospital_admin", "user_id": str(uuid.uuid4()),
             "hospital_uuid": str(uuid.uuid4())}
    decision = authorization.evaluate(admin, ("radiology", "edit"))
    assert decision.allowed
    staff_no_roles = {"role": "hospital_staff", "user_id": str(uuid.uuid4()),
                      "hospital_uuid": str(uuid.uuid4())}
    denied = authorization.evaluate(staff_no_roles, ("radiology", "edit"))
    assert not denied.allowed
