"""Regression tests verifying sync event publishing, canonical entity types, and details propagation."""

from __future__ import annotations

import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from infrastructure.postgres.base import Base
import tests.conftest  # noqa: F401
import modules.masters.entities.organization_entities  # noqa: F401
from modules.tenancy.entities.hospital import Hospital
from modules.patients.entities.patient import Patient
from modules.laboratory.entities.lab_entities import (
    LabTestCatalog,
    LabOrderStatus,
    LabSampleType,
)
from modules.laboratory.contracts.lab_contracts import (
    LabOrderCreate,
    SpecimenCollectRequest,
)
from modules.laboratory.actions.laboratory_actions import (
    CreateLabOrderAction,
    CollectSpecimenAction,
)
from shared.sync.broker import sync_broker
from shared.audit.service import write_audit_log


@pytest.fixture
def sync_test_env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()

    h_id = uuid.uuid4()
    hospital = Hospital(
        id=h_id,
        hospital_id=f"HOSP-SYNC-{uuid.uuid4().hex[:4]}",
        name="Sync Test Hospital",
        address="123 Sync St",
        phone="9876543210",
        email="sync@test.com",
        password_hash="hashed_pw",
        is_active=True,
    )
    session.add(hospital)

    patient = Patient(
        id=uuid.uuid4(),
        hospital_id=h_id,
        name="Sync Tester",
        gender="Male",
        age=30,
        mobile="9876543210",
        uhid=f"UHID-{uuid.uuid4().hex[:6]}",
    )
    session.add(patient)

    test_item = LabTestCatalog(
        id=uuid.uuid4(),
        hospital_id=h_id,
        test_code="CBC_SYNC",
        test_name="Complete Blood Count",
        department="Hematology",
        sample_type=LabSampleType.blood,
        tat_hours=12,
        price=350.0,
        is_active=True,
    )
    session.add(test_item)
    session.commit()

    return {
        "session": session,
        "hospital_id": h_id,
        "patient": patient,
        "test": test_item,
    }


def test_lab_order_and_specimen_events_published_on_commit(sync_test_env):
    session = sync_test_env["session"]
    h_id = sync_test_env["hospital_id"]
    patient = sync_test_env["patient"]
    test_item = sync_test_env["test"]

    actor = {"sub": "staff@test.com", "name": "Lab Tech", "role": "hospital_staff"}

    # 1. Create Order
    # Note: CreateLabOrderAction calls self.db.commit() internally, which immediately
    # triggers after_commit and publishes to sync_broker! So history_before must be
    # captured right before execute.
    hid_str = str(h_id)
    history_before = list(sync_broker._history.get(hid_str, []))
    order_in = LabOrderCreate(
        patient_id=patient.id,
        test_ids=[test_item.id],
        clinical_notes="STAT Pre-op sync test",
    )
    order = CreateLabOrderAction(session, h_id).execute(
        payload=order_in,
        user=actor,
    )

    # Verify events after commit
    history_after = list(sync_broker._history.get(hid_str, []))
    new_events = history_after[len(history_before):]

    # Must contain lab_order and lab_specimen events
    entity_types = [e["entity_type"] for e in new_events]
    assert "lab_order" in entity_types
    assert "lab_specimen" in entity_types

    # Lab specimen event should contain order_id in details
    specimen_events = [e for e in new_events if e["entity_type"] == "lab_specimen"]
    assert len(specimen_events) > 0
    assert specimen_events[0]["details"]["order_id"] == str(order.id)

    # 2. Collect Specimen
    history_before_collect = list(sync_broker._history.get(hid_str, []))
    specimen = order.specimens[0]
    collect_in = SpecimenCollectRequest(
        collected_by="Phlebotomist Joe",
        collection_remarks="Left arm antecubital",
    )
    updated_order = CollectSpecimenAction(session, h_id).execute(
        order_id=order.id,
        specimen_id=specimen.id,
        payload=collect_in,
        user=actor,
    )

    history_after_collect = list(sync_broker._history.get(hid_str, []))
    collect_events = history_after_collect[len(history_before_collect):]

    # Verify specimen collection event
    specimen_collect = [
        e for e in collect_events
        if e["entity_type"] == "lab_specimen" and e["action"] == "collect"
    ]
    assert len(specimen_collect) == 1
    assert specimen_collect[0]["entity_id"] == str(specimen.id)
    assert specimen_collect[0]["details"]["order_id"] == str(order.id)


def test_rollback_does_not_publish_sync_events(sync_test_env):
    session = sync_test_env["session"]
    h_id = sync_test_env["hospital_id"]

    history_before = list(sync_broker._history.get(h_id, []))
    actor = {"sub": "staff@test.com", "name": "Lab Tech", "role": "hospital_staff"}

    # Stage an audit log / sync event
    write_audit_log(
        session=session,
        hospital_id=h_id,
        actor=actor,
        action="uncommitted_action",
        entity_type="lab_order",
        entity_id=uuid.uuid4(),
        summary="Testing aborted action",
        details={"status": "aborted"},
    )

    # Roll back transaction
    session.rollback()

    # Verify no new events were published to the sync broker
    history_after = list(sync_broker._history.get(h_id, []))
    assert len(history_after) == len(history_before)
