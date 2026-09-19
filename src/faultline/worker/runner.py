"""The worker process: claim a job, run the graph, ack.

Delivery semantics are the point of this file. A job is acked only after the
incident's terminal state is persisted, stuck jobs are reclaimed from the pending
entry list, and a job that has been delivered too many times is dead-lettered
rather than retried forever.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from faultline.config import Settings
from faultline.core.alerts import Alert, TimeWindow
from faultline.core.schemas import ApprovalDecision, Status
from faultline.core.state import SCHEMA_VERSION, initial_state
from faultline.gateway.policy import TokenSigner
from faultline.gateway.registry import ToolRegistry
from faultline.logging import get_logger
from faultline.ports import Bus, EventPublisher, Message, ProgressEvent, Repository
from faultline.worker.graph import build_graph
from faultline.worker.models import build_router
from faultline.worker.nodes import InvestigationNodes, fresh_budget
from faultline.worker.prompts import PROMPT_VERSION
from faultline.worker.serde import make_serializer

log = get_logger(__name__)

JOB_INVESTIGATE = "investigate"
JOB_RESUME = "resume"


class InvestigationWorker:
    def __init__(
        self,
        settings: Settings,
        bus: Bus,
        repository: Repository,
        publisher: EventPublisher,
        registry: ToolRegistry,
        checkpointer: Any | None = None,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._repo = repository
        self._publisher = publisher
        self._signer = TokenSigner(settings.gateway_signing_key)
        self._nodes = InvestigationNodes(
            router=build_router(**_router_kwargs(settings)),
            registry=registry,
            publisher=publisher,
            signer=self._signer,
            max_iterations=settings.max_iterations,
        )
        self._graph = build_graph(
            self._nodes, checkpointer or InMemorySaver(serde=make_serializer())
        )
        self._stopping = asyncio.Event()

    # -- lifecycle --------------------------------------------------------

    def stop(self) -> None:
        """SIGTERM handler. Finish the current node, then release the job."""
        self._stopping.set()

    async def run_forever(self, consumer: str = "worker-1") -> None:
        reclaimer = asyncio.create_task(self._reclaim_loop(consumer))
        try:
            while not self._stopping.is_set():
                async for message in self._bus.consume(
                    self._settings.jobs_stream,
                    self._settings.consumer_group,
                    consumer,
                    block_ms=2000,
                ):
                    await self._handle(message)
                    if self._stopping.is_set():
                        break
        finally:
            reclaimer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reclaimer

    async def _reclaim_loop(self, consumer: str) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(30)
            stale = await self._bus.reclaim(
                self._settings.jobs_stream,
                self._settings.consumer_group,
                consumer,
                self._settings.job_reclaim_idle_ms,
            )
            for message in stale:
                log.info("reclaimed_job", message_id=message.id, deliveries=message.deliveries)
                await self._handle(message)

    async def _handle(self, message: Message) -> None:
        if message.deliveries > self._settings.job_max_deliveries:
            await self._bus.dead_letter(message, "max deliveries exceeded")
            log.error("dead_lettered", message_id=message.id)
            return
        try:
            if message.payload.get("job") == JOB_RESUME:
                await self.resume(message.payload["incident_id"], message.payload["tenant_id"])
            else:
                await self.investigate(message.payload)
        except Exception as exc:
            log.exception("job_failed", message_id=message.id, error=str(exc))
            await self._publisher.publish(
                ProgressEvent(
                    incident_id=message.payload.get("incident_id", "?"),
                    kind="error",
                    data={"detail": str(exc)},
                )
            )
            return  # no ack: the pending entry is reclaimed and retried
        # Ack only after the terminal state is persisted.
        await self._bus.ack(self._settings.jobs_stream, self._settings.consumer_group, message.id)

    # -- job handlers -----------------------------------------------------

    async def investigate(self, payload: dict[str, str]) -> dict[str, Any]:
        incident_id = payload["incident_id"]
        tenant_id = payload["tenant_id"]
        alerts = [Alert.model_validate_json(a) for a in payload["alerts"].split("\x1e") if a]
        window = TimeWindow.model_validate_json(payload["window"])

        state = initial_state(
            incident_id=incident_id,
            tenant_id=tenant_id,
            alerts=alerts,
            window=window,
            budget=fresh_budget(
                self._settings.budget_max_tokens,
                self._settings.budget_max_usd,
                self._settings.budget_max_tool_calls,
                self._settings.budget_deadline_seconds,
            ),
        )
        # Recorded per investigation so any past result can be reproduced: which
        # prompts, which models, which state schema produced it.
        await self._repo.audit(incident_id, "system", "investigation_started", self.versions())
        return await self._drive(incident_id, tenant_id, state)

    def versions(self) -> dict[str, Any]:
        """The provenance stamp attached to every investigation."""
        return {
            "prompt_version": PROMPT_VERSION,
            "state_schema_version": SCHEMA_VERSION,
            "model_provider": self._settings.model_provider,
            "model_frontier": self._settings.model_frontier,
            "model_small": self._settings.model_small,
        }

    async def resume(self, incident_id: str, tenant_id: str) -> dict[str, Any]:
        """Continue a graph parked at the approval interrupt."""
        decision = await self._repo.get_approval(incident_id)
        if decision is None:
            raise RuntimeError(f"no approval recorded for {incident_id}")
        return await self._drive(incident_id, tenant_id, Command(resume=decision))

    async def _drive(self, incident_id: str, tenant_id: str, payload: Any) -> dict[str, Any]:
        config = {"configurable": {"thread_id": incident_id}}
        async for _mode, _chunk in self._graph.astream(payload, config, stream_mode=["updates"]):
            pass

        snapshot = await self._graph.aget_state(config)
        values: dict[str, Any] = snapshot.values
        status: Status = values.get("status", Status.ESCALATED)

        if snapshot.interrupts:
            # Parked for a human. Persist enough for the UI, then stop; the API's
            # approve endpoint enqueues the resume job.
            await self._repo.set_status(incident_id, Status.AWAITING_APPROVAL)
            if report := values.get("report"):
                await self._repo.save_report(incident_id, report)
            await self._publisher.publish(
                ProgressEvent(
                    incident_id=incident_id,
                    kind="awaiting_approval",
                    data={"interrupt": snapshot.interrupts[0].value},
                )
            )
            await self._repo.audit(
                incident_id,
                "system",
                "awaiting_approval",
                {"action": values.get("proposed_action") and values["proposed_action"].id},
            )
            return values

        if report := values.get("report"):
            await self._repo.save_report(incident_id, report)
        await self._repo.set_status(incident_id, status)
        await self._repo.audit(
            incident_id,
            "system",
            "investigation_finished",
            {"status": status.value, "budget": values["budget"].snapshot()},
        )
        return values


def _router_kwargs(settings: Settings) -> dict[str, Any]:
    """Model ids differ per provider, so the tier names are resolved here rather
    than making every provider share one pair of settings."""
    if settings.model_provider == "ollama":
        return {
            "provider": "ollama",
            "frontier": settings.ollama_frontier,
            "small": settings.ollama_small,
            "ollama_host": settings.ollama_host,
        }
    return {
        "provider": settings.model_provider,
        "frontier": settings.model_frontier,
        "small": settings.model_small,
        "frontier_fallback": settings.model_frontier_fallback,
        "small_fallback": settings.model_small_fallback,
    }


def make_registry(settings: Settings) -> ToolRegistry:
    """Backend selection. `live` will point at Prometheus/Loki/Tempo; today both
    paths read a scenario, which is also what the eval harness replays."""
    from faultline.gateway.backends.scenario import bad_deploy_scenario

    return ToolRegistry(bad_deploy_scenario(), retriever=build_retriever_sync(settings))


async def build_retriever_for(settings: Settings) -> Any | None:
    """Index the corpus once, or return None if there is none.

    Retrieval failing is a degraded investigation, not a dead worker: the
    knowledge tool abstains and every other evidence source still works. The
    failure is logged at error level rather than debug, because a silently
    missing corpus looks exactly like a corpus with nothing relevant in it.
    """
    from pathlib import Path

    from faultline.retrieval.dense import OllamaEmbedder
    from faultline.retrieval.ingest import build_retriever

    root = Path(settings.corpus_path)
    if not root.exists():
        log.info("corpus_absent", path=str(root))
        return None
    embedder = (
        OllamaEmbedder(settings.embed_model, host=settings.ollama_host)
        if settings.embed_provider == "ollama"
        else None
    )
    try:
        return await build_retriever(root, embedder=embedder)
    except Exception as exc:
        log.error("corpus_index_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def build_retriever_sync(settings: Settings) -> Any | None:
    """For start-up paths that are not already inside an event loop.

    Calling this from a running loop is a bug, not a degradation: it used to be
    caught by the broad handler above and silently disabled retrieval for the
    whole benchmark. Async callers use `build_retriever_for` directly.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(build_retriever_for(settings))
    raise RuntimeError(
        "build_retriever_sync called from a running event loop; await "
        "build_retriever_for(settings) instead"
    )


def approval_from(payload: dict[str, Any]) -> ApprovalDecision:
    return ApprovalDecision.model_validate(payload)
