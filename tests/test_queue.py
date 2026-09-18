"""Queue semantics and the transactional outbox.

The in-memory bus implements the same contract as Redis Streams consumer groups,
so these tests guard delivery behavior without any containers.
"""

from __future__ import annotations

import pytest
from tests.conftest import make_alert

from faultline.adapters.memory import InMemoryBus, InMemoryRepository
from faultline.core.alerts import correlate


@pytest.fixture
def bus() -> InMemoryBus:
    return InMemoryBus()


async def _drain(bus: InMemoryBus, stream: str, group: str, consumer: str) -> list:
    return [m async for m in bus.consume(stream, group, consumer, block_ms=10)]


async def test_a_message_is_delivered_once_to_a_group(bus: InMemoryBus) -> None:
    await bus.publish("jobs", {"job": "investigate"})
    first = await _drain(bus, "jobs", "g", "c1")
    second = await _drain(bus, "jobs", "g", "c2")
    assert len(first) == 1
    assert second == []


async def test_an_unacked_message_is_reclaimable(bus: InMemoryBus) -> None:
    """A worker that dies mid-investigation must not strand the job."""
    await bus.publish("jobs", {"job": "investigate"})
    await _drain(bus, "jobs", "g", "dead-worker")
    assert await bus.pending_count("jobs", "g") == 1

    reclaimed = await bus.reclaim("jobs", "g", "live-worker", min_idle_ms=0)
    assert len(reclaimed) == 1
    assert reclaimed[0].deliveries == 2


async def test_an_acked_message_is_not_reclaimed(bus: InMemoryBus) -> None:
    await bus.publish("jobs", {"job": "investigate"})
    messages = await _drain(bus, "jobs", "g", "c1")
    await bus.ack("jobs", "g", messages[0].id)
    assert await bus.reclaim("jobs", "g", "c2", min_idle_ms=0) == []
    assert await bus.pending_count("jobs", "g") == 0


async def test_dead_letter_captures_the_reason(bus: InMemoryBus) -> None:
    await bus.publish("jobs", {"job": "investigate"})
    messages = await _drain(bus, "jobs", "g", "c1")
    await bus.dead_letter(messages[0], "max deliveries exceeded")
    assert bus.dead_letters[0][1] == "max deliveries exceeded"


async def test_backlog_is_the_scaling_signal(bus: InMemoryBus) -> None:
    for _ in range(5):
        await bus.publish("jobs", {"job": "investigate"})
    assert bus.backlog("jobs", "g") == 5
    await _drain(bus, "jobs", "g", "c1")
    assert bus.backlog("jobs", "g") < 5


async def test_incident_creation_is_idempotent_on_the_group_key() -> None:
    """A retried Alertmanager delivery must not open a second incident."""
    repo = InMemoryRepository()
    group = correlate([make_alert(), make_alert(service="checkout-service")], tenant_id="acme")[0]

    first, created_first = await repo.create_incident_if_absent(group, "inc_1", "t")
    second, created_second = await repo.create_incident_if_absent(group, "inc_2", "t")

    assert created_first and not created_second
    assert first.incident_id == second.incident_id == "inc_1"


async def test_the_outbox_is_drained_exactly_once() -> None:
    repo = InMemoryRepository()
    await repo.enqueue_outbox("jobs", {"job": "investigate"})

    pending = await repo.drain_outbox()
    assert len(pending) == 1
    await repo.mark_outbox_sent([pending[0][0]])
    assert await repo.drain_outbox() == []


async def test_the_audit_log_is_hash_chained() -> None:
    """Each entry commits to its predecessor, so a deleted row is detectable."""
    repo = InMemoryRepository()
    await repo.audit("inc_1", "sre", "created", {})
    await repo.audit("inc_1", "sre", "approved", {"action": "rollback"})

    first, second = repo.audit_log
    assert first["previous_hash"] == "genesis"
    assert second["previous_hash"] == first["entry_hash"]
    assert second["entry_hash"] != first["entry_hash"]


async def test_one_tenant_cannot_read_another_tenants_incident() -> None:
    repo = InMemoryRepository()
    group = correlate([make_alert()], tenant_id="acme")[0]
    record, _ = await repo.create_incident_if_absent(group, "inc_1", "t")

    assert await repo.get_incident("acme", record.incident_id) is not None
    assert await repo.get_incident("evilcorp", record.incident_id) is None
    assert await repo.list_incidents("evilcorp") == []
