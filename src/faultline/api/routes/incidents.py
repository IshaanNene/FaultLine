"""Incident reads, the live stream, and the approval endpoint."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from faultline.api.deps import ContainerDep, PrincipalDep, Role
from faultline.core.schemas import ApprovalDecision, Status
from faultline.logging import get_logger

router = APIRouter(prefix="/incidents", tags=["incidents"])
log = get_logger(__name__)


class IncidentSummary(BaseModel):
    incident_id: str
    title: str
    status: Status
    severity: str
    services: list[str]
    alert_count: int
    created_at: str


class IncidentDetail(IncidentSummary):
    report: dict[str, Any] | None = None


@router.get("", response_model=list[IncidentSummary])
async def list_incidents(
    container: ContainerDep, principal: PrincipalDep, limit: int = 50
) -> list[IncidentSummary]:
    principal.require(Role.VIEWER)
    rows = await container.repository.list_incidents(principal.tenant_id, limit=limit)
    return [_summary(r) for r in rows]


@router.get("/{incident_id}", response_model=IncidentDetail)
async def get_incident(
    incident_id: str, container: ContainerDep, principal: PrincipalDep
) -> IncidentDetail:
    principal.require(Role.VIEWER)
    record = await container.repository.get_incident(principal.tenant_id, incident_id)
    if record is None:
        # 404 rather than 403 for another tenant's incident: a 403 confirms it exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    return IncidentDetail(
        **_summary(record).model_dump(),
        report=record.report.model_dump(mode="json") if record.report else None,
    )


@router.get("/{incident_id}/stream")
async def stream_incident(
    incident_id: str, container: ContainerDep, principal: PrincipalDep
) -> StreamingResponse:
    """Server-sent events for the live investigation view.

    The worker running the graph is not the process holding this socket, so
    events arrive over pub/sub and this endpoint only fans them out.
    """
    principal.require(Role.VIEWER)
    record = await container.repository.get_incident(principal.tenant_id, incident_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")

    async def events() -> Any:
        async for event in container.publisher.subscribe(incident_id):
            body = json.dumps(
                {
                    "kind": event.kind,
                    "data": event.data,
                    "at": event.at.isoformat() if event.at else None,
                },
                default=str,
            )
            yield f"event: {event.kind}\ndata: {body}\n\n"
        yield "event: close\ndata: {}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ApprovalRequest(BaseModel):
    action_id: str
    decision: str  # approve | reject | edit
    note: str | None = None
    edited_arguments: dict[str, Any] | None = None


@router.post("/{incident_id}/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve(
    incident_id: str,
    body: ApprovalRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> dict[str, str]:
    """Record a human decision and wake the parked graph.

    The decision is persisted before the resume job is enqueued, so a worker that
    picks the job up always finds a decision waiting -- and a duplicate resume
    finds the same one rather than a second approval.
    """
    principal.require(Role.APPROVER)
    if body.decision not in {"approve", "reject", "edit"}:
        raise HTTPException(status_code=422, detail="decision must be approve, reject or edit")

    record = await container.repository.get_incident(principal.tenant_id, incident_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    if record.status is not Status.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"incident is {record.status}, not awaiting approval",
        )

    decision = ApprovalDecision(
        action_id=body.action_id,
        decision=body.decision,
        actor=principal.subject,
        note=body.note,
        edited_arguments=body.edited_arguments,
    )
    await container.repository.record_approval(incident_id, decision)
    await container.repository.audit(
        incident_id,
        principal.subject,
        f"action_{body.decision}",
        {"action_id": body.action_id, "note": body.note},
    )
    await container.bus.publish(
        container.settings.jobs_stream,
        {"job": "resume", "incident_id": incident_id, "tenant_id": principal.tenant_id},
    )
    return {"status": "accepted", "incident_id": incident_id}


def _summary(record: Any) -> IncidentSummary:
    return IncidentSummary(
        incident_id=record.incident_id,
        title=record.title,
        status=record.status,
        severity=record.severity,
        services=record.services,
        alert_count=record.alert_count,
        created_at=record.created_at.isoformat(),
    )
