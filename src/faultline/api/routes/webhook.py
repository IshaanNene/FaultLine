"""Alertmanager webhook.

Two properties matter here and nothing else does:

1. The ack is fast. Alertmanager retries aggressively, and a slow webhook turns
   one incident into a queue of duplicates.
2. The write is idempotent. A retried delivery maps to the same group key and the
   repository's unique constraint returns the existing incident.

The incident row and the investigation job commit together through the outbox, so
there is no window where an alert is stored but nobody is investigating it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from faultline.api.deps import ContainerDep, PrincipalDep, Role
from faultline.core.alerts import Alert, AlertGroup, correlate, make_incident_id
from faultline.logging import get_logger

router = APIRouter(prefix="/webhook", tags=["webhook"])
log = get_logger(__name__)


class AlertmanagerPayload(BaseModel):
    """The subset of Alertmanager's webhook body Faultline uses."""

    version: str = "4"
    group_key: str = Field(default="", alias="groupKey")
    status: str = "firing"
    receiver: str = ""
    alerts: list[Alert] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class WebhookAccepted(BaseModel):
    incidents: list[dict[str, Any]]
    deduplicated: int


@router.post("/alertmanager", status_code=status.HTTP_202_ACCEPTED, response_model=WebhookAccepted)
async def alertmanager(
    payload: AlertmanagerPayload,
    container: ContainerDep,
    principal: PrincipalDep,
) -> WebhookAccepted:
    principal.require(Role.RESPONDER)

    groups = correlate(
        payload.alerts,
        tenant_id=principal.tenant_id,
        window_seconds=container.settings.correlation_window_seconds,
    )
    accepted: list[dict[str, Any]] = []
    deduplicated = 0

    for group in groups:
        record, created = await container.repository.create_incident_if_absent(
            group, make_incident_id(), title=_title(group)
        )
        if not created:
            deduplicated += 1
            log.info("webhook_deduplicated", incident_id=record.incident_id)
            accepted.append({"incident_id": record.incident_id, "created": False})
            continue

        await container.repository.enqueue_outbox(
            container.settings.jobs_stream, _job(record.incident_id, group)
        )
        await container.repository.audit(
            record.incident_id,
            principal.subject,
            "incident_created",
            {"group_key": group.group_key, "alerts": len(group.alerts)},
        )
        accepted.append({"incident_id": record.incident_id, "created": True})

    await _flush_outbox(container)
    return WebhookAccepted(incidents=accepted, deduplicated=deduplicated)


def _title(group: AlertGroup) -> str:
    names = sorted({a.name for a in group.alerts})
    services = ", ".join(group.services) or "unknown service"
    return f"{', '.join(names[:2])} on {services}"


def _job(incident_id: str, group: AlertGroup) -> dict[str, str]:
    # Record-separator joined rather than JSON-encoded, because Redis Streams
    # fields are flat strings and this keeps the payload readable in XRANGE.
    return {
        "job": "investigate",
        "incident_id": incident_id,
        "tenant_id": group.tenant_id,
        "alerts": "\x1e".join(a.model_dump_json() for a in group.alerts),
        "window": group.window().model_dump_json(),
        "severity": group.severity.value,
    }


async def _flush_outbox(container: ContainerDep) -> None:
    """Publish committed outbox rows.

    In production a separate reconciler also runs this every 60s, so a crash
    between commit and publish costs latency rather than a lost investigation.
    """
    pending = await container.repository.drain_outbox()
    sent = []
    for row_id, stream, job_payload in pending:
        await container.bus.publish(stream, job_payload)
        sent.append(row_id)
    if sent:
        await container.repository.mark_outbox_sent(sent)
