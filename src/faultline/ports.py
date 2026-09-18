"""Ports: the interfaces the rest of the system talks to.

Every one of these has an in-memory adapter and a live adapter. That is not
ceremony -- it is what lets the full webhook-to-report path run in a unit test
with no Docker, and it is the seam the evaluation harness will later use to swap
live telemetry for a recorded incident capsule.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from faultline.core.alerts import AlertGroup
from faultline.core.schemas import ApprovalDecision, RCAReport, Status


@dataclass(slots=True)
class Message:
    """One queue message. `deliveries` drives the dead-letter decision."""

    id: str
    stream: str
    payload: dict[str, str]
    deliveries: int = 1


@dataclass(slots=True)
class IncidentRecord:
    incident_id: str
    tenant_id: str
    group_key: str
    status: Status
    severity: str
    services: list[str]
    created_at: datetime
    updated_at: datetime
    title: str = ""
    report: RCAReport | None = None
    alert_count: int = 0


@dataclass(slots=True)
class ProgressEvent:
    """What the UI sees while an investigation runs."""

    incident_id: str
    # node_started | node_finished | evidence | triage | report
    # | awaiting_approval | error | done
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime | None = None


@runtime_checkable
class Bus(Protocol):
    """At-least-once queue with consumer groups. Redis Streams in production."""

    async def publish(self, stream: str, payload: dict[str, str]) -> str: ...

    # Not `async def`: the implementations are async generators, so calling
    # this returns the iterator directly rather than a coroutine yielding one.
    def consume(
        self, stream: str, group: str, consumer: str, block_ms: int = 5000, count: int = 1
    ) -> AsyncIterator[Message]: ...

    async def ack(self, stream: str, group: str, message_id: str) -> None: ...

    async def reclaim(
        self, stream: str, group: str, consumer: str, min_idle_ms: int
    ) -> list[Message]: ...

    async def dead_letter(self, message: Message, reason: str) -> None: ...

    async def pending_count(self, stream: str, group: str) -> int: ...


@runtime_checkable
class Repository(Protocol):
    """Source of truth. Postgres in production."""

    async def create_incident_if_absent(
        self, group: AlertGroup, incident_id: str, title: str
    ) -> tuple[IncidentRecord, bool]:
        """Idempotent on (tenant_id, group_key). Returns (record, created)."""

    async def get_incident(self, tenant_id: str, incident_id: str) -> IncidentRecord | None: ...

    async def list_incidents(self, tenant_id: str, limit: int = 50) -> Sequence[IncidentRecord]: ...

    async def set_status(self, incident_id: str, status: Status) -> None: ...

    async def save_report(self, incident_id: str, report: RCAReport) -> None: ...

    async def record_approval(self, incident_id: str, decision: ApprovalDecision) -> None: ...

    async def get_approval(self, incident_id: str) -> ApprovalDecision | None: ...

    async def enqueue_outbox(self, stream: str, payload: dict[str, str]) -> None:
        """Transactional outbox: the incident row and the job commit together."""

    async def drain_outbox(self, limit: int = 100) -> list[tuple[int, str, dict[str, str]]]: ...

    async def mark_outbox_sent(self, ids: Sequence[int]) -> None: ...

    async def audit(
        self, incident_id: str, actor: str, action: str, detail: dict[str, Any]
    ) -> None:
        """Append-only, hash-chained. Never updated, never deleted."""


# Events after which nothing more arrives until something external happens. A
# stream closes on these so a client is never left holding an idle socket: after
# an approval the UI reconnects to the resumed investigation.
TERMINAL_EVENTS = frozenset({"done", "awaiting_approval"})


@runtime_checkable
class EventPublisher(Protocol):
    """Fan-out from the worker running the graph to the API pod holding the SSE socket."""

    async def publish(self, event: ProgressEvent) -> None: ...

    def subscribe(self, incident_id: str) -> AsyncIterator[ProgressEvent]: ...
