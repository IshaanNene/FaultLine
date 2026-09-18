"""The HTTP surface: idempotency, RBAC, tenant isolation and the approval gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from faultline.api.app import create_app
from faultline.api.deps import build_container
from faultline.core.schemas import Status

RESPONDER = {"X-Tenant-Id": "acme", "X-Roles": "responder", "Authorization": "Bearer sre@acme"}
APPROVER = {"X-Tenant-Id": "acme", "X-Roles": "approver", "Authorization": "Bearer lead@acme"}
VIEWER = {"X-Tenant-Id": "acme", "X-Roles": "viewer"}
OTHER_TENANT = {"X-Tenant-Id": "evilcorp", "X-Roles": "approver"}


def _payload(service: str = "frontend") -> dict:
    started = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    return {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "namespace": "shop",
                    "service": service,
                    "severity": "critical",
                },
                "startsAt": started,
            }
        ],
    }


@pytest.fixture
async def client(settings):
    app = create_app(settings)
    app.state.container = build_container(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.container = app.state.container  # type: ignore[attr-defined]
        yield c


async def test_health_does_not_depend_on_storage(client) -> None:
    assert (await client.get("/healthz")).status_code == 200


async def test_webhook_creates_an_incident(client) -> None:
    response = await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    assert response.status_code == 202
    assert response.json()["incidents"][0]["created"] is True


async def test_a_retried_delivery_does_not_create_a_second_incident(client) -> None:
    first = await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    second = await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)

    assert second.json()["deduplicated"] == 1
    assert (
        first.json()["incidents"][0]["incident_id"] == second.json()["incidents"][0]["incident_id"]
    )


async def test_a_retried_delivery_does_not_queue_a_second_job(client) -> None:
    """Deduplicating the row but not the job would still burn a full investigation."""
    await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    assert client.container.bus.backlog("faultline:jobs", "investigators") == 1


async def test_the_job_is_queued_through_the_outbox(client) -> None:
    await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    assert await client.container.repository.drain_outbox() == []  # drained on the request path
    assert client.container.bus.backlog("faultline:jobs", "investigators") == 1


async def test_an_unauthenticated_request_is_rejected(client) -> None:
    assert (await client.post("/webhook/alertmanager", json=_payload())).status_code == 401


async def test_a_viewer_cannot_post_alerts(client) -> None:
    response = await client.post("/webhook/alertmanager", json=_payload(), headers=VIEWER)
    assert response.status_code == 403


async def test_an_approver_can_read(client) -> None:
    """Roles are cumulative: you must be able to read what you are approving."""
    await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    assert (await client.get("/incidents", headers=APPROVER)).status_code == 200


async def test_another_tenant_sees_nothing(client) -> None:
    await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    assert (await client.get("/incidents", headers=OTHER_TENANT)).json() == []


async def test_another_tenant_gets_404_not_403(client) -> None:
    """A 403 would confirm the incident exists."""
    created = await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    incident_id = created.json()["incidents"][0]["incident_id"]
    assert (await client.get(f"/incidents/{incident_id}", headers=OTHER_TENANT)).status_code == 404


async def test_approving_an_incident_that_is_not_waiting_is_a_conflict(client) -> None:
    created = await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    incident_id = created.json()["incidents"][0]["incident_id"]

    response = await client.post(
        f"/incidents/{incident_id}/approve",
        json={"action_id": "act_1", "decision": "approve"},
        headers=APPROVER,
    )
    assert response.status_code == 409


async def test_a_responder_cannot_approve(client) -> None:
    created = await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    incident_id = created.json()["incidents"][0]["incident_id"]
    await client.container.repository.set_status(incident_id, Status.AWAITING_APPROVAL)

    response = await client.post(
        f"/incidents/{incident_id}/approve",
        json={"action_id": "act_1", "decision": "approve"},
        headers=RESPONDER,
    )
    assert response.status_code == 403


async def test_an_invalid_decision_is_rejected(client) -> None:
    created = await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    incident_id = created.json()["incidents"][0]["incident_id"]
    await client.container.repository.set_status(incident_id, Status.AWAITING_APPROVAL)

    response = await client.post(
        f"/incidents/{incident_id}/approve",
        json={"action_id": "act_1", "decision": "yolo"},
        headers=APPROVER,
    )
    assert response.status_code == 422


async def test_approval_persists_the_decision_before_enqueuing_the_resume(client) -> None:
    """A worker that picks up the resume must always find a decision waiting."""
    created = await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    incident_id = created.json()["incidents"][0]["incident_id"]
    await client.container.repository.set_status(incident_id, Status.AWAITING_APPROVAL)

    await client.post(
        f"/incidents/{incident_id}/approve",
        json={"action_id": "act_1", "decision": "approve", "note": "ok"},
        headers=APPROVER,
    )
    decision = await client.container.repository.get_approval(incident_id)
    assert decision is not None
    assert decision.approved
    assert decision.actor == "lead@acme"


async def test_every_decision_is_audited(client) -> None:
    created = await client.post("/webhook/alertmanager", json=_payload(), headers=RESPONDER)
    incident_id = created.json()["incidents"][0]["incident_id"]
    await client.container.repository.set_status(incident_id, Status.AWAITING_APPROVAL)
    await client.post(
        f"/incidents/{incident_id}/approve",
        json={"action_id": "act_1", "decision": "approve"},
        headers=APPROVER,
    )
    actions = [e["action"] for e in client.container.repository.audit_log]
    assert "incident_created" in actions
    assert "action_approve" in actions


async def test_streaming_an_unknown_incident_is_404(client) -> None:
    assert (await client.get("/incidents/inc_nope/stream", headers=VIEWER)).status_code == 404
