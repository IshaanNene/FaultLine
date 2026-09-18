"""In-memory adapters.

These are not toys: they implement the same delivery semantics as the Redis and
Postgres adapters, including consumer-group redelivery, pending-entry reclaim and
the transactional outbox. The end-to-end test drives the real graph through
these, so a regression in the queue contract fails in CI without any containers.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from faultline.core.alerts import AlertGroup
from faultline.core.schemas import ApprovalDecision, RCAReport, Status
from faultline.ids import content_hash
from faultline.ports import TERMINAL_EVENTS, IncidentRecord, Message, ProgressEvent


class InMemoryBus:
    """Consumer-group semantics without Redis: deliver, hold pending, reclaim, ack."""

    def __init__(self) -> None:
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
        self._cursors: dict[tuple[str, str], int] = defaultdict(int)
        self._pending: dict[tuple[str, str, str], tuple[Message, float]] = {}
        self._deliveries: dict[str, int] = defaultdict(int)
        self.dead_letters: list[tuple[Message, str]] = []
        self._seq = itertools.count(1)
        self._wakeup: dict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def publish(self, stream: str, payload: dict[str, str]) -> str:
        message_id = f"{int(datetime.now(UTC).timestamp() * 1000)}-{next(self._seq)}"
        self._streams[stream].append((message_id, payload))
        self._wakeup[stream].set()
        return message_id

    async def consume(
        self, stream: str, group: str, consumer: str, block_ms: int = 5000, count: int = 1
    ) -> AsyncIterator[Message]:
        key = (stream, group)
        while True:
            entries = self._streams[stream]
            if self._cursors[key] >= len(entries):
                self._wakeup[stream].clear()
                try:
                    await asyncio.wait_for(self._wakeup[stream].wait(), timeout=block_ms / 1000)
                except TimeoutError:
                    return
                continue
            for _ in range(count):
                if self._cursors[key] >= len(entries):
                    break
                message_id, payload = entries[self._cursors[key]]
                self._cursors[key] += 1
                self._deliveries[message_id] += 1
                message = Message(
                    id=message_id,
                    stream=stream,
                    payload=payload,
                    deliveries=self._deliveries[message_id],
                )
                self._pending[(stream, group, message_id)] = (
                    message,
                    asyncio.get_running_loop().time(),
                )
                yield message

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        self._pending.pop((stream, group, message_id), None)

    async def reclaim(
        self, stream: str, group: str, consumer: str, min_idle_ms: int
    ) -> list[Message]:
        now = asyncio.get_running_loop().time()
        stale = []
        for (s, g, mid), (message, claimed_at) in list(self._pending.items()):
            if s == stream and g == group and (now - claimed_at) * 1000 >= min_idle_ms:
                self._deliveries[mid] += 1
                message.deliveries = self._deliveries[mid]
                self._pending[(s, g, mid)] = (message, now)
                stale.append(message)
        return stale

    async def dead_letter(self, message: Message, reason: str) -> None:
        self.dead_letters.append((message, reason))
        await self.ack(message.stream, "*", message.id)

    async def pending_count(self, stream: str, group: str) -> int:
        return sum(1 for (s, g, _) in self._pending if s == stream and g == group)

    def backlog(self, stream: str, group: str) -> int:
        """Unread entries. KEDA scales workers on this in production."""
        return max(0, len(self._streams[stream]) - self._cursors[(stream, group)])


class InMemoryRepository:
    """Source of truth for tests and `faultline demo`."""

    def __init__(self) -> None:
        self._incidents: dict[str, IncidentRecord] = {}
        self._by_group: dict[tuple[str, str], str] = {}
        self._approvals: dict[str, ApprovalDecision] = {}
        self._outbox: list[tuple[int, str, dict[str, str], bool]] = []
        self._outbox_seq = itertools.count(1)
        self.audit_log: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def create_incident_if_absent(
        self, group: AlertGroup, incident_id: str, title: str
    ) -> tuple[IncidentRecord, bool]:
        async with self._lock:
            key = (group.tenant_id, group.group_key)
            if key in self._by_group:
                return self._incidents[self._by_group[key]], False
            now = datetime.now(UTC)
            record = IncidentRecord(
                incident_id=incident_id,
                tenant_id=group.tenant_id,
                group_key=group.group_key,
                status=Status.TRIAGE,
                severity=group.severity.value,
                services=group.services,
                created_at=now,
                updated_at=now,
                title=title,
                alert_count=len(group.alerts),
            )
            self._incidents[incident_id] = record
            self._by_group[key] = incident_id
            return record, True

    async def get_incident(self, tenant_id: str, incident_id: str) -> IncidentRecord | None:
        record = self._incidents.get(incident_id)
        # Tenant scoping is enforced here, mirroring the row-level security policy
        # the Postgres adapter relies on. The caller can never widen it.
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    async def list_incidents(self, tenant_id: str, limit: int = 50) -> Sequence[IncidentRecord]:
        rows = [r for r in self._incidents.values() if r.tenant_id == tenant_id]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    async def set_status(self, incident_id: str, status: Status) -> None:
        if record := self._incidents.get(incident_id):
            record.status = status
            record.updated_at = datetime.now(UTC)

    async def save_report(self, incident_id: str, report: RCAReport) -> None:
        if record := self._incidents.get(incident_id):
            record.report = report
            record.updated_at = datetime.now(UTC)

    async def record_approval(self, incident_id: str, decision: ApprovalDecision) -> None:
        self._approvals[incident_id] = decision

    async def get_approval(self, incident_id: str) -> ApprovalDecision | None:
        return self._approvals.get(incident_id)

    async def enqueue_outbox(self, stream: str, payload: dict[str, str]) -> None:
        self._outbox.append((next(self._outbox_seq), stream, payload, False))

    async def drain_outbox(self, limit: int = 100) -> list[tuple[int, str, dict[str, str]]]:
        return [(i, s, p) for (i, s, p, sent) in self._outbox if not sent][:limit]

    async def mark_outbox_sent(self, ids: Sequence[int]) -> None:
        wanted = set(ids)
        self._outbox = [
            (i, s, p, True if i in wanted else sent) for (i, s, p, sent) in self._outbox
        ]

    async def audit(
        self, incident_id: str, actor: str, action: str, detail: dict[str, Any]
    ) -> None:
        previous = self.audit_log[-1]["entry_hash"] if self.audit_log else "genesis"
        entry = {
            "incident_id": incident_id,
            "actor": actor,
            "action": action,
            "detail": detail,
            "at": datetime.now(UTC).isoformat(),
            "previous_hash": previous,
        }
        entry["entry_hash"] = content_hash(previous, incident_id, actor, action, str(detail))
        self.audit_log.append(entry)


class InMemoryEventPublisher:
    """Progress fan-out. Redis pub/sub replaces this when api and worker are separate pods."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[ProgressEvent | None]]] = defaultdict(list)
        self.history: dict[str, list[ProgressEvent]] = defaultdict(list)

    async def publish(self, event: ProgressEvent) -> None:
        if event.at is None:
            event.at = datetime.now(UTC)
        self.history[event.incident_id].append(event)
        for queue in self._subscribers[event.incident_id]:
            queue.put_nowait(event)
        if event.kind in TERMINAL_EVENTS:
            for queue in self._subscribers[event.incident_id]:
                queue.put_nowait(None)

    async def subscribe(self, incident_id: str) -> AsyncIterator[ProgressEvent]:
        queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        self._subscribers[incident_id].append(queue)
        try:
            # Replay what already happened, so a late-connecting UI is not blank.
            replayed = list(self.history[incident_id])
            for event in replayed:
                yield event
            # If the stream already reached a stopping point, close rather than
            # hanging: a client that connects after the investigation parked for
            # approval would otherwise hold the socket open forever.
            if replayed and replayed[-1].kind in TERMINAL_EVENTS:
                return
            while True:
                pending = await queue.get()
                if pending is None:
                    return
                yield pending
        finally:
            self._subscribers[incident_id].remove(queue)
