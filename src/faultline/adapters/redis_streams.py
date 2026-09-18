"""Redis Streams queue and pub/sub progress fan-out.

Redis Streams, not Kafka: the worst-case alert volume this system sees is
thousands per minute, and consumer groups already give at-least-once delivery,
a pending entry list for reclaiming stuck jobs, and a natural backpressure
signal (stream length) for KEDA to scale on.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from faultline.logging import get_logger
from faultline.ports import TERMINAL_EVENTS, Message, ProgressEvent

log = get_logger(__name__)

DEAD_LETTER_SUFFIX = ":dead"


class RedisBus:
    def __init__(self, url: str) -> None:
        self._redis: Redis = Redis.from_url(url, decode_responses=True)
        self._groups_ready: set[tuple[str, str]] = set()

    async def _ensure_group(self, stream: str, group: str) -> None:
        if (stream, group) in self._groups_ready:
            return
        try:
            # mkstream so a consumer can start before the first producer.
            await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups_ready.add((stream, group))

    async def publish(self, stream: str, payload: dict[str, str]) -> str:
        return str(await self._redis.xadd(stream, cast(Any, payload)))

    async def consume(
        self, stream: str, group: str, consumer: str, block_ms: int = 5000, count: int = 1
    ) -> AsyncIterator[Message]:
        await self._ensure_group(stream, group)
        response = await self._redis.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms
        )
        for _stream_name, entries in cast(Any, response) or []:
            for message_id, payload in entries:
                yield Message(
                    id=message_id,
                    stream=stream,
                    payload=payload,
                    deliveries=await self._delivery_count(stream, group, message_id),
                )

    async def _delivery_count(self, stream: str, group: str, message_id: str) -> int:
        pending = await self._redis.xpending_range(
            stream, group, min=message_id, max=message_id, count=1
        )
        return int(pending[0]["times_delivered"]) if pending else 1

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        await self._redis.xack(stream, group, message_id)

    async def reclaim(
        self, stream: str, group: str, consumer: str, min_idle_ms: int
    ) -> list[Message]:
        """Take over entries a dead worker never acked."""
        await self._ensure_group(stream, group)
        _cursor, entries, _deleted = await self._redis.xautoclaim(
            stream, group, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=50
        )
        return [
            Message(
                id=mid,
                stream=stream,
                payload=payload,
                deliveries=await self._delivery_count(stream, group, mid),
            )
            for mid, payload in entries
        ]

    async def dead_letter(self, message: Message, reason: str) -> None:
        await self._redis.xadd(
            message.stream + DEAD_LETTER_SUFFIX,
            cast(
                Any,
                {
                    **message.payload,
                    "_reason": reason,
                    "_original_id": message.id,
                    "_at": datetime.now(UTC).isoformat(),
                },
            ),
        )

    async def pending_count(self, stream: str, group: str) -> int:
        await self._ensure_group(stream, group)
        summary = await self._redis.xpending(stream, group)
        return int(summary["pending"]) if summary else 0

    async def backlog(self, stream: str, group: str) -> int:
        """Entries not yet delivered to the group. This is the KEDA scaling metric."""
        try:
            groups = await self._redis.xinfo_groups(stream)
        except ResponseError:
            return 0
        for info in groups:
            if info["name"] == group:
                return int(info.get("lag") or 0)
        return 0

    async def close(self) -> None:
        await self._redis.aclose()


class RedisEventPublisher:
    """Per-incident pub/sub channel, so any API pod can serve any incident's SSE."""

    def __init__(self, url: str) -> None:
        self._redis: Redis = Redis.from_url(url, decode_responses=True)

    @staticmethod
    def _channel(incident_id: str) -> str:
        return f"faultline:progress:{incident_id}"

    async def publish(self, event: ProgressEvent) -> None:
        if event.at is None:
            event.at = datetime.now(UTC)
        await self._redis.publish(
            self._channel(event.incident_id),
            json.dumps(
                {"kind": event.kind, "data": event.data, "at": event.at.isoformat()},
                default=str,
            ),
        )

    async def subscribe(self, incident_id: str) -> AsyncIterator[ProgressEvent]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel(incident_id))
        try:
            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                body = json.loads(raw["data"])
                event = ProgressEvent(
                    incident_id=incident_id,
                    kind=body["kind"],
                    data=body["data"],
                    at=datetime.fromisoformat(body["at"]),
                )
                yield event
                if event.kind in TERMINAL_EVENTS:
                    return
        finally:
            await pubsub.unsubscribe(self._channel(incident_id))
            await pubsub.aclose()

    async def close(self) -> None:
        await self._redis.aclose()
