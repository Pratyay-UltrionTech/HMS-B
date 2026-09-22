"""Tests for SSE sync endpoint and poll fallback."""

from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from modules.sync.api.sync_api import router as sync_router
from shared.auth.jwt import create_access_token
from shared.sync.broker import sync_broker


def test_sync_events_poll_and_auth():
    app = FastAPI()
    app.include_router(sync_router, prefix="/api")
    client = TestClient(app)

    hospital_id = uuid4()
    token = create_access_token(
        {
            "sub": "doctor@hospital.com",
            "name": "Dr Test",
            "role": "hospital_staff",
            "hospital_uuid": str(hospital_id),
        }
    )

    # 1. Test unauthenticated request is rejected
    bad = client.get("/api/sync/poll?since=0")
    assert bad.status_code == 401

    bad_sse = client.get("/api/sync/events")
    assert bad_sse.status_code == 401

    # 2. Test poll endpoint returns events published to broker
    sync_broker.publish(hospital_id, "appointment", "create", str(uuid4()))
    poll_res = client.get(f"/api/sync/poll?since=0&token={token}")
    assert poll_res.status_code == 200
    data = poll_res.json()
    assert len(data["events"]) > 0
    assert data["events"][-1]["entity_type"] == "appointment"

    # 3. Test filter by since timestamp
    now_ms = data["server_time"]
    empty_res = client.get(f"/api/sync/poll?since={now_ms + 10000}&token={token}")
    assert empty_res.status_code == 200
    assert len(empty_res.json()["events"]) == 0
