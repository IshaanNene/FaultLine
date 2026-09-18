"""Postgres repository.

One transactional store for incidents, the outbox, the audit log and (once the
retrieval stack lands) chunks with both pgvector and BM25 indexes. Choosing this
over a separate vector database is a deliberate trade: it keeps the incident row
and the job enqueue in one transaction, which is what makes the outbox work.

Tenant isolation is enforced by row-level security. Every transaction sets
`app.tenant_id`, and the policies in db/init/001_schema.sql do the filtering, so
a forgotten WHERE clause cannot leak across tenants.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import asyncpg

from faultline.core.alerts import AlertGroup
from faultline.core.schemas import ApprovalDecision, RCAReport, Status
from faultline.ids import content_hash
from faultline.logging import get_logger
from faultline.ports import IncidentRecord

log = get_logger(__name__)


class PostgresRepository:
    def __init__(self, dsn: str, min_size: int = 2, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=self._min_size, max_size=self._max_size
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def _tx(self, tenant_id: str | None = None) -> AsyncIterator[asyncpg.Connection]:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn, conn.transaction():
            if tenant_id is not None:
                # set_config with is_local=true scopes it to this transaction, so a
                # pooled connection never carries a tenant into the next request.
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            yield conn

    # -- incidents --------------------------------------------------------

    async def create_incident_if_absent(
        self, group: AlertGroup, incident_id: str, title: str
    ) -> tuple[IncidentRecord, bool]:
        async with self._tx(group.tenant_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO incidents (
                    incident_id, tenant_id, group_key, status, severity,
                    services, title, alert_count
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (tenant_id, group_key) DO NOTHING
                RETURNING *, true AS created
                """,
                incident_id,
                group.tenant_id,
                group.group_key,
                Status.TRIAGE.value,
                group.severity.value,
                group.services,
                title,
                len(group.alerts),
            )
            if row is None:
                # The conflict path: a retried webhook. Return what is already there.
                row = await conn.fetchrow(
                    "SELECT *, false AS created FROM incidents "
                    "WHERE tenant_id = $1 AND group_key = $2",
                    group.tenant_id,
                    group.group_key,
                )
            assert row is not None
            return _to_record(row), bool(row["created"])

    async def get_incident(self, tenant_id: str, incident_id: str) -> IncidentRecord | None:
        async with self._tx(tenant_id) as conn:
            row = await conn.fetchrow("SELECT * FROM incidents WHERE incident_id = $1", incident_id)
        return _to_record(row) if row else None

    async def list_incidents(self, tenant_id: str, limit: int = 50) -> Sequence[IncidentRecord]:
        async with self._tx(tenant_id) as conn:
            rows = await conn.fetch(
                "SELECT * FROM incidents ORDER BY created_at DESC LIMIT $1", limit
            )
        return [_to_record(r) for r in rows]

    async def set_status(self, incident_id: str, status: Status) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE incidents SET status = $2, updated_at = now() WHERE incident_id = $1",
                incident_id,
                status.value,
            )

    async def save_report(self, incident_id: str, report: RCAReport) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE incidents SET report = $2::jsonb, updated_at = now() "
                "WHERE incident_id = $1",
                incident_id,
                report.model_dump_json(),
            )

    # -- approvals --------------------------------------------------------

    async def record_approval(self, incident_id: str, decision: ApprovalDecision) -> None:
        async with self._tx() as conn:
            await conn.execute(
                """
                INSERT INTO approvals (incident_id, action_id, decision, actor, payload)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (incident_id, action_id) DO NOTHING
                """,
                incident_id,
                decision.action_id,
                decision.decision,
                decision.actor,
                decision.model_dump_json(),
            )

    async def get_approval(self, incident_id: str) -> ApprovalDecision | None:
        async with self._tx() as conn:
            row = await conn.fetchrow(
                "SELECT payload FROM approvals WHERE incident_id = $1 "
                "ORDER BY decided_at DESC LIMIT 1",
                incident_id,
            )
        return ApprovalDecision.model_validate_json(row["payload"]) if row else None

    # -- outbox -----------------------------------------------------------

    async def enqueue_outbox(self, stream: str, payload: dict[str, str]) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO outbox (stream, payload) VALUES ($1, $2::jsonb)",
                stream,
                json.dumps(payload),
            )

    async def drain_outbox(self, limit: int = 100) -> list[tuple[int, str, dict[str, str]]]:
        async with self._tx() as conn:
            rows = await conn.fetch(
                # SKIP LOCKED so several API pods or the reconciler can drain
                # concurrently without publishing the same job twice.
                "SELECT id, stream, payload FROM outbox WHERE sent_at IS NULL "
                "ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED",
                limit,
            )
        return [(r["id"], r["stream"], json.loads(r["payload"])) for r in rows]

    async def mark_outbox_sent(self, ids: Sequence[int]) -> None:
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE outbox SET sent_at = now() WHERE id = ANY($1::bigint[])", list(ids)
            )

    # -- audit ------------------------------------------------------------

    async def audit(
        self, incident_id: str, actor: str, action: str, detail: dict[str, Any]
    ) -> None:
        """Append-only and hash-chained: each row commits to its predecessor, so a
        deleted or edited row is detectable rather than merely discouraged."""
        async with self._tx() as conn:
            previous = await conn.fetchval(
                "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
            )
            previous = previous or "genesis"
            body = json.dumps(detail, default=str, sort_keys=True)
            entry_hash = content_hash(previous, incident_id, actor, action, body)
            await conn.execute(
                """
                INSERT INTO audit_log
                    (incident_id, actor, action, detail, at, previous_hash, entry_hash)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                """,
                incident_id,
                actor,
                action,
                body,
                datetime.now(UTC),
                previous,
                entry_hash,
            )


def _to_record(row: asyncpg.Record) -> IncidentRecord:
    report = row["report"]
    return IncidentRecord(
        incident_id=row["incident_id"],
        tenant_id=row["tenant_id"],
        group_key=row["group_key"],
        status=Status(row["status"]),
        severity=row["severity"],
        services=list(row["services"] or []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        title=row["title"] or "",
        report=RCAReport.model_validate_json(report) if report else None,
        alert_count=row["alert_count"] or 0,
    )
