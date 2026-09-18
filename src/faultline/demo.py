"""The walking skeleton, end to end, in one process.

This drives the *real* HTTP API over an in-process ASGI transport, the real
correlator, the real outbox, the real queue semantics, the real LangGraph
investigation and the real approval interrupt. Only the infrastructure is
in-memory and only the model tier is a stub.

    faultline demo --approve
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx

from faultline.adapters.memory import InMemoryBus, InMemoryEventPublisher, InMemoryRepository
from faultline.api.app import create_app
from faultline.api.deps import build_container
from faultline.config import Settings
from faultline.core.schemas import Status
from faultline.gateway.backends.scenario import bad_deploy_scenario
from faultline.gateway.registry import ToolRegistry
from faultline.worker.runner import InvestigationWorker

TENANT = "acme"
HEADERS = {
    "X-Tenant-Id": TENANT,
    "X-Roles": "responder,approver",
    "Authorization": "Bearer oncall@acme",
}


def _alertmanager_payload() -> dict[str, Any]:
    """What Alertmanager actually posts when checkout breaks.

    Note that `frontend` is the service that alerts. It is not the service that
    broke -- that is the whole point of the scenario.
    """
    now = datetime.now(UTC)
    started = (now - timedelta(minutes=12)).isoformat()
    return {
        "version": "4",
        "groupKey": '{}:{alertname="HighErrorRate"}',
        "status": "firing",
        "receiver": "faultline",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "namespace": "shop",
                    "service": "frontend",
                    "severity": "critical",
                },
                "annotations": {"summary": "frontend 5xx rate above 5% for 5m"},
                "startsAt": started,
            },
            {
                "status": "firing",
                "labels": {
                    "alertname": "LatencySLOBurn",
                    "namespace": "shop",
                    "service": "frontend",
                    "severity": "critical",
                },
                "annotations": {"summary": "frontend p99 latency budget burning fast"},
                "startsAt": started,
            },
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "namespace": "shop",
                    "service": "checkout-service",
                    "severity": "warning",
                },
                "annotations": {"summary": "checkout-service 5xx rate above 5% for 5m"},
                "startsAt": started,
            },
        ],
    }


async def run_demo(approve: bool = True, verbose: bool = True) -> dict[str, Any]:
    settings = Settings(backend="memory", environment="local")
    container = build_container(settings)
    # The demo reads the fakes' introspection helpers (queue backlog, event
    # history, audit chain), which the ports deliberately do not expose.
    bus = cast(InMemoryBus, container.bus)
    publisher = cast(InMemoryEventPublisher, container.publisher)
    repository = cast(InMemoryRepository, container.repository)

    app = create_app(settings)
    app.state.container = container  # share one container between API and worker

    worker = InvestigationWorker(
        settings=settings,
        bus=container.bus,
        repository=container.repository,
        publisher=container.publisher,
        registry=ToolRegistry(bad_deploy_scenario()),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://faultline") as client:
        _say(verbose, "\n[1] Alertmanager fires three alerts on the shop namespace")
        response = await client.post(
            "/webhook/alertmanager", json=_alertmanager_payload(), headers=HEADERS
        )
        response.raise_for_status()
        incident_id = response.json()["incidents"][0]["incident_id"]
        _say(verbose, f"    -> accepted as {incident_id}")

        _say(verbose, "[2] Alertmanager retries the same delivery")
        retry = await client.post(
            "/webhook/alertmanager", json=_alertmanager_payload(), headers=HEADERS
        )
        _say(verbose, f"    -> deduplicated: {retry.json()['deduplicated']} (no second incident)")

        job = _next_job(bus, settings.jobs_stream)
        _say(verbose, "[3] Worker claims the job and runs the investigation graph")
        await worker.investigate(job)

        detail = (await client.get(f"/incidents/{incident_id}", headers=HEADERS)).json()
        report = detail["report"]
        _say(verbose, f"    -> status: {detail['status']}")
        _say(
            verbose,
            f"    -> root cause: {report['root_cause_service']} "
            f"({report['fault_class']}, confidence {report['confidence']:.0%})",
        )
        for claim in report["causal_chain"]:
            _say(verbose, f"       - {claim['text'][:110]}  [{', '.join(claim['evidence_ids'])}]")
        for rejected in report["rejected_hypotheses"]:
            _say(verbose, f"       x rejected: {rejected['statement']} -- {rejected['reason']}")

        flagged = [
            e
            for e in publisher.history[incident_id]
            if e.kind == "evidence" and e.data.get("flagged")
        ]
        if flagged:
            _say(
                verbose,
                f"    -> {len(flagged)} evidence item(s) flagged as possible prompt injection "
                "and quarantined",
            )

        if not approve:
            _say(verbose, "[4] Parked awaiting approval. Re-run with --approve to continue.")
            return {"incident_id": incident_id, "report": report, "status": detail["status"]}

        action = report["proposed_actions"][0]
        _say(verbose, f"[4] Proposed action: {action['kind']} {action['target']}")
        _say(verbose, "    -> waiting for a human; nothing has touched the cluster")

        approval = await client.post(
            f"/incidents/{incident_id}/approve",
            json={
                "action_id": action["id"],
                "decision": "approve",
                "note": "confirmed in #incidents",
            },
            headers=HEADERS,
        )
        approval.raise_for_status()
        _say(verbose, "[5] Approved. Resuming the parked graph from its checkpoint")

        _next_job(bus, settings.jobs_stream)  # drain the resume job
        await worker.resume(incident_id, TENANT)

        final = (await client.get(f"/incidents/{incident_id}", headers=HEADERS)).json()
        _say(verbose, f"    -> final status: {final['status']}")
        _say(
            verbose,
            f"    -> audit trail: {len(repository.audit_log)} hash-chained entries",
        )

    return {
        "incident_id": incident_id,
        "report": final["report"],
        "status": final["status"],
        "audit_entries": len(repository.audit_log),
    }


def _next_job(bus: InMemoryBus, stream: str) -> dict[str, str]:
    """Pop the next queued job. The real worker gets this from XREADGROUP."""
    entries = bus._streams[stream]
    if not entries:
        raise RuntimeError(f"no job queued on {stream}")
    return entries.pop(0)[1]


def _say(verbose: bool, message: str) -> None:
    if verbose:
        print(message)


def assert_found_root_cause(result: dict[str, Any], expected: str = "checkout-service") -> None:
    """Used by the end-to-end test and, later, by the eval harness scorer."""
    actual = result["report"]["root_cause_service"]
    if actual != expected:
        raise AssertionError(f"expected root cause {expected}, got {actual}")
    if result["status"] != Status.RESOLVED.value:
        raise AssertionError(f"expected resolved, got {result['status']}")
