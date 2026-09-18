"""Investigation graph nodes.

Each node is a plain async function of state that returns a partial update, so
every one is unit-testable without building the graph. Shared dependencies (model
router, tool gateway, event publisher, token signer) hang off `InvestigationNodes`
rather than being imported, which is what lets the eval harness swap a live
backend for a recorded capsule.

One LangGraph rule shapes the whole file: an interrupted node restarts from its
beginning on resume. Side effects therefore live only in `execute`, behind an
idempotency key, and `approval` does nothing but call `interrupt()`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.types import interrupt

from faultline.core.alerts import Severity
from faultline.core.budget import Budget
from faultline.core.ledger import EvidenceLedger
from faultline.core.schemas import (
    ApprovalDecision,
    Check,
    Evidence,
    EvidenceGap,
    FaultClass,
    Hypothesis,
    HypothesisStatus,
    RCAReport,
    Status,
    TriageVerdict,
)
from faultline.core.state import InvestigationState, check_schema_version
from faultline.gateway.policy import PolicyError, TokenSigner
from faultline.gateway.registry import ToolError, ToolRegistry
from faultline.ids import content_hash
from faultline.logging import get_logger
from faultline.ports import EventPublisher, ProgressEvent
from faultline.worker.models import ModelRouter, ModelUnavailable, Task
from faultline.worker.verification import strip_failed_claims, verify_report

log = get_logger(__name__)

MAX_VERIFY_ATTEMPTS = 2
CHECK_TIMEOUT_S = 10.0


class InvestigationNodes:
    def __init__(
        self,
        router: ModelRouter,
        registry: ToolRegistry,
        publisher: EventPublisher,
        signer: TokenSigner,
        max_iterations: int = 4,
    ) -> None:
        self._router = router
        self._registry = registry
        self._publisher = publisher
        self._signer = signer
        self._max_iterations = max_iterations
        # Per-incident tool cache. The model asking the same question twice should
        # cost once; keyed by content so an equivalent call hits.
        self._tool_cache: dict[str, Evidence] = {}

    # -- helpers ----------------------------------------------------------

    async def _emit(self, incident_id: str, kind: str, **data: Any) -> None:
        await self._publisher.publish(ProgressEvent(incident_id=incident_id, kind=kind, data=data))

    async def _invoke(self, task: Task, state: InvestigationState, context: dict[str, Any]) -> Any:
        """Call a model and charge the budget. Budget accounting lives here so no
        node can forget it."""
        result, usage = await self._router.invoke(task, context)
        state["budget"].charge_model(usage.tokens, usage.usd)
        return result

    def _capability(self, state: InvestigationState) -> str:
        return self._signer.mint_capability(
            tenant_id=state["tenant_id"], incident_id=state["incident_id"]
        )

    # -- nodes ------------------------------------------------------------

    async def triage(self, state: InvestigationState) -> dict[str, Any]:
        """Classify before spending anything. Most alerts end here."""
        check_schema_version(state)
        await self._emit(state["incident_id"], "node_started", node="triage")
        try:
            verdict: TriageVerdict = await self._invoke(
                Task.TRIAGE, state, {"alerts": state["alerts"]}
            )
        except ModelUnavailable:
            # Degrade to alert labels rather than blocking: severity from the
            # labels is still actionable information for whoever is paged.
            firing = [a for a in state["alerts"] if a.is_firing]
            worst = min((a.severity for a in firing), key=lambda s: s.rank, default=Severity.P4)
            verdict = TriageVerdict(
                classification="actionable" if firing else "noise",
                severity=worst.value,
                affected_services=sorted({a.service for a in firing if a.service}),
                summary="Triage model unavailable; classified from alert labels.",
            )
        await self._emit(state["incident_id"], "triage", **verdict.model_dump())
        status = (
            Status.INVESTIGATING
            if verdict.classification == "actionable"
            else Status.CLOSED_NOISE
            if verdict.classification == "noise"
            else Status.CLOSED_DUPLICATE
        )
        return {"triage": verdict, "status": status}

    async def prefetch(self, state: InvestigationState) -> dict[str, Any]:
        """Deterministic context, gathered in parallel, before any reasoning.

        Every source fails independently: a dead logs backend costs one evidence
        gap, not the investigation.
        """
        await self._emit(state["incident_id"], "node_started", node="prefetch")
        services = state["triage"].affected_services if state["triage"] else []
        calls: list[tuple[str, dict[str, Any]]] = [
            ("rank_suspects", {}),
            ("get_recent_changes", {"window_minutes": 60}),
            ("find_similar_incidents", {}),
            ("search_knowledge", {"query": state["triage"].summary if state["triage"] else ""}),
        ]
        calls += [("get_topology", {"service": s, "depth": 2}) for s in services[:3]]

        evidence, gaps = await self._run_tools(state, calls)
        bundle = {
            "suspects": next(
                (list(e.facts.get("ranking", [])) for e in evidence if e.tool == "rank_suspects"),
                [],
            ),
            "changes": [e.facts for e in evidence if e.tool == "get_recent_changes"],
            "topology": _merge_topology(evidence),
            "runbooks": [e.facts for e in evidence if e.tool == "search_knowledge"],
            "similar_incidents": [e.facts for e in evidence if e.tool == "find_similar_incidents"],
        }
        await self._emit(
            state["incident_id"],
            "node_finished",
            node="prefetch",
            suspects=[s.get("service") for s in bundle["suspects"]],
        )
        return {"prefetch": bundle, "evidence": evidence, "gaps": gaps}

    async def hypothesize(self, state: InvestigationState) -> dict[str, Any]:
        """Competing hypotheses, each with a test that would refute it."""
        await self._emit(state["incident_id"], "node_started", node="hypothesize")
        prefetch = state.get("prefetch", {})
        changed = {
            service
            for change in prefetch.get("changes", [])
            for service in list(change.get("services", []))
        }
        try:
            new: list[Hypothesis] = await self._invoke(
                Task.HYPOTHESIZE,
                state,
                {
                    "suspects": prefetch.get("suspects", []),
                    "changed_services": sorted(changed),
                    "alert_services": state["triage"].affected_services if state["triage"] else [],
                    "existing": state.get("hypotheses", []),
                },
            )
        except ModelUnavailable:
            # The deterministic suspect ranking is a usable hypothesis list on its own.
            new = [
                Hypothesis(
                    id=content_hash(s["service"])[:12],
                    statement=f"{s['service']} is the root cause",
                    suspect_service=str(s["service"]),
                    fault_class=FaultClass.UNKNOWN,
                    refuting_test="dependencies of this service are also anomalous",
                )
                for s in prefetch.get("suspects", [])[:3]
            ]
        await self._emit(
            state["incident_id"],
            "node_finished",
            node="hypothesize",
            hypotheses=[h.statement for h in new],
        )
        return {"hypotheses": new, "iteration": state.get("iteration", 0) + 1}

    async def plan_checks(self, state: InvestigationState) -> dict[str, Any]:
        """Pick the checks that best separate the leading hypotheses."""
        await self._emit(state["incident_id"], "node_started", node="plan_checks")
        already = {f"{e.tool}:{e.query}" for e in state.get("evidence", [])}
        checks: list[Check] = await self._invoke(
            Task.PLAN,
            state,
            {
                "hypotheses": state.get("hypotheses", []),
                "already_run": already,
                "remaining_tool_calls": state["budget"].remaining_tool_calls(),
            },
        )
        return {"pending_checks": checks}

    async def run_checks(self, state: InvestigationState) -> dict[str, Any]:
        """Fan out the planned checks concurrently, with per-tool timeouts."""
        checks = state.get("pending_checks", [])
        if not checks:
            return {"pending_checks": []}
        await self._emit(state["incident_id"], "node_started", node="run_checks", count=len(checks))
        evidence, gaps = await self._run_tools(state, [(c.tool, c.arguments) for c in checks])
        for entry in evidence:
            await self._emit(
                state["incident_id"],
                "evidence",
                id=entry.id,
                tool=entry.tool,
                summary=entry.summary,
                flagged=entry.injection_flagged,
            )
        return {"evidence": evidence, "gaps": gaps, "pending_checks": []}

    async def assess(self, state: InvestigationState) -> dict[str, Any]:
        """Score every hypothesis against the ledger and decide what happens next."""
        await self._emit(state["incident_id"], "node_started", node="assess")
        ledger = EvidenceLedger(state.get("evidence", []))
        updated: list[Hypothesis] = await self._invoke(
            Task.ASSESS,
            state,
            {"hypotheses": state.get("hypotheses", []), "ledger": ledger},
        )
        await self._emit(
            state["incident_id"],
            "node_finished",
            node="assess",
            statuses={h.suspect_service: h.status.value for h in updated},
        )
        return {"hypotheses": updated}

    async def synthesize(self, state: InvestigationState) -> dict[str, Any]:
        await self._emit(state["incident_id"], "node_started", node="synthesize")
        ledger = EvidenceLedger(state.get("evidence", []))
        report: RCAReport = await self._invoke(
            Task.SYNTHESIZE,
            state,
            {
                "hypotheses": state.get("hypotheses", []),
                "ledger": ledger,
                "gaps": [str(g) for g in state.get("gaps", [])],
                "topology": state.get("prefetch", {}).get("topology", {}),
            },
        )
        return {"report": report}

    async def verify(self, state: InvestigationState) -> dict[str, Any]:
        """Deterministic checks first, entailment last, abstention if it all fails."""
        await self._emit(state["incident_id"], "node_started", node="verify")
        report = state.get("report")
        if report is None:
            return {"verification": None}
        ledger = EvidenceLedger(state.get("evidence", []))
        previous = state.get("verification")
        attempt = (previous.attempt + 1) if previous else 1

        async def entails(claim: Any) -> bool:
            return bool(await self._invoke(Task.ENTAIL, state, {"claim": claim, "ledger": ledger}))

        verification = await verify_report(
            report=report,
            ledger=ledger,
            known_services=_known_services(state),
            entails=entails,
            attempt=attempt,
        )
        await self._emit(
            state["incident_id"],
            "node_finished",
            node="verify",
            passed=verification.passed,
            detail=verification.summary,
        )
        if verification.passed or attempt < MAX_VERIFY_ATTEMPTS:
            return {"verification": verification}
        # Out of retries: report what survived, with confidence lowered to match.
        return {
            "verification": verification,
            "report": strip_failed_claims(report, verification),
        }

    async def propose(self, state: InvestigationState) -> dict[str, Any]:
        """Map the root cause to a catalog action. The model never writes a command."""
        report = state.get("report")
        if report is None or report.abstained:
            return {"proposed_action": None, "status": Status.ESCALATED}
        action = self._registry.propose_action(report.root_cause_service, report.fault_class.value)
        if action is None:
            return {"proposed_action": None, "status": Status.ESCALATED}
        updated = report.model_copy(update={"proposed_actions": [action]})
        await self._emit(
            state["incident_id"],
            "report",
            root_cause=updated.root_cause_service,
            confidence=updated.confidence,
            action=action.model_dump(mode="json"),
        )
        return {
            "proposed_action": action,
            "report": updated,
            "status": Status.AWAITING_APPROVAL,
        }

    async def approval(self, state: InvestigationState) -> dict[str, Any]:
        """Pause for a human.

        Nothing above the `interrupt()` call may have a side effect: on resume
        LangGraph re-executes this node from its first line.
        """
        action = state.get("proposed_action")
        report = state.get("report")
        decision = interrupt(
            {
                "incident_id": state["incident_id"],
                "report": report.model_dump(mode="json") if report else None,
                "action": action.model_dump(mode="json") if action else None,
            }
        )
        parsed = (
            decision
            if isinstance(decision, ApprovalDecision)
            else ApprovalDecision.model_validate(decision)
        )
        return {
            "approval": parsed,
            "status": Status.REMEDIATING if parsed.approved else Status.ESCALATED,
        }

    async def execute(self, state: InvestigationState) -> dict[str, Any]:
        """The only node with a side effect, and the only one holding a write token."""
        action = state.get("proposed_action")
        approval = state.get("approval")
        if action is None or approval is None or not approval.approved:
            return {"notes": ["execute skipped: no approved action"]}

        # Derived, not random: a redelivered job recomputes the same key, and the
        # gateway rejects the second attempt.
        idempotency_key = content_hash(state["incident_id"], action.id, approval.actor)[:24]
        grant_token = self._signer.mint_approval(
            tenant_id=state["tenant_id"],
            incident_id=state["incident_id"],
            action_id=action.id,
            tool=action.kind.value,
            target=action.target,
            approver=approval.actor,
        )
        try:
            grant = self._signer.read_approval(grant_token)
            result = self._registry.execute(action, grant, idempotency_key)
        except PolicyError as exc:
            await self._emit(state["incident_id"], "error", detail=str(exc))
            return {"notes": [f"execute denied: {exc}"], "status": Status.ESCALATED}
        await self._emit(state["incident_id"], "node_finished", node="execute", result=result)
        return {"notes": [result]}

    async def confirm_recovery(self, state: InvestigationState) -> dict[str, Any]:
        """Watch the SLO after a fix. Not recovering is new evidence, not an error."""
        await self._emit(state["incident_id"], "node_started", node="confirm_recovery")
        report = state.get("report")
        if report is None:
            return {"recovered": False}
        evidence, gaps = await self._run_tools(
            state, [("get_service_health", {"service": report.root_cause_service})]
        )
        recovered = bool(evidence) and not any(e.facts.get("anomalous") for e in evidence)
        return {
            "evidence": evidence,
            "gaps": gaps,
            "recovered": recovered,
            "status": Status.RESOLVED if recovered else Status.INVESTIGATING,
        }

    async def report(self, state: InvestigationState) -> dict[str, Any]:
        """Terminal node. Always produces something, even out of budget."""
        summary = await self._invoke(Task.SUMMARIZE, state, {"report": state.get("report")})
        status = state.get("status", Status.ESCALATED)
        if not status.is_terminal:
            status = Status.RESOLVED if state.get("recovered") else Status.ESCALATED
        await self._emit(
            state["incident_id"],
            "done",
            summary=summary,
            status=status.value,
            budget=state["budget"].snapshot(),
        )
        return {"notes": [summary], "status": status}

    # -- tool execution ---------------------------------------------------

    async def _run_tools(
        self, state: InvestigationState, calls: list[tuple[str, dict[str, Any]]]
    ) -> tuple[list[Evidence], list[EvidenceGap]]:
        capability_token = self._capability(state)
        capability = self._signer.read_capability(capability_token)
        budget: Budget = state["budget"]

        async def one(tool: str, arguments: dict[str, Any]) -> Evidence | EvidenceGap:
            cache_key = content_hash(tool, str(sorted(arguments.items())))
            if cached := self._tool_cache.get(cache_key):
                return cached
            if budget.remaining_tool_calls() <= 0:
                return EvidenceGap(tool=tool, reason="tool-call budget exhausted")
            budget.charge_tool()
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._registry.call, tool, arguments, capability),
                    timeout=CHECK_TIMEOUT_S,
                )
            except TimeoutError:
                return EvidenceGap(tool=tool, reason=f"timed out after {CHECK_TIMEOUT_S:g}s")
            except (ToolError, PolicyError) as exc:
                return EvidenceGap(tool=tool, reason=str(exc))
            self._tool_cache[cache_key] = result
            return result

        results = await asyncio.gather(*(one(tool, args) for tool, args in calls))
        evidence = [r for r in results if isinstance(r, Evidence)]
        gaps = [r for r in results if isinstance(r, EvidenceGap)]
        if gaps:
            log.warning(
                "evidence_gaps", incident_id=state["incident_id"], gaps=[str(g) for g in gaps]
            )
        return evidence, gaps

    # -- routing decisions ------------------------------------------------

    def route_after_triage(self, state: InvestigationState) -> str:
        verdict = state.get("triage")
        if verdict is None or verdict.classification != "actionable":
            return "report"
        return "prefetch"

    def route_after_assess(self, state: InvestigationState) -> str:
        """Conclude, continue or stop.

        Concluding requires a supported hypothesis backed by at least two
        independent evidence kinds -- a metric change point plus a deploy
        correlation counts, three log queries do not.
        """
        if state["budget"].exhausted:
            return "report"
        if state.get("iteration", 0) >= self._max_iterations:
            return "report"

        ledger = EvidenceLedger(state.get("evidence", []))
        supported = [
            h for h in state.get("hypotheses", []) if h.status == HypothesisStatus.SUPPORTED
        ]
        if not supported:
            return "plan_checks"
        lead = max(supported, key=lambda h: h.confidence)
        rivals_settled = all(
            h.status in (HypothesisStatus.REFUTED, HypothesisStatus.UNKNOWN)
            for h in state.get("hypotheses", [])
            if h.id != lead.id
        )
        if ledger.independent_support(lead.supporting_evidence) and rivals_settled:
            return "synthesize"
        return "plan_checks"

    def route_after_verify(self, state: InvestigationState) -> str:
        verification = state.get("verification")
        if verification is None or verification.passed:
            return "propose"
        if verification.attempt < MAX_VERIFY_ATTEMPTS:
            return "assess"
        return "propose"

    def route_after_propose(self, state: InvestigationState) -> str:
        return "approval" if state.get("proposed_action") else "report"

    def route_after_approval(self, state: InvestigationState) -> str:
        approval = state.get("approval")
        return "execute" if approval and approval.approved else "report"

    def route_after_execute(self, state: InvestigationState) -> str:
        return "confirm_recovery" if state.get("approval") else "report"

    def route_after_recovery(self, state: InvestigationState) -> str:
        if state.get("recovered"):
            return "report"
        if state["budget"].exhausted or state.get("iteration", 0) >= self._max_iterations:
            return "report"
        return "hypothesize"


def _known_services(state: InvestigationState) -> list[str]:
    """The live service catalog for this investigation, from the trace-derived topology."""
    topology: dict[str, list[str]] = state.get("prefetch", {}).get("topology", {})
    services = set(topology)
    for downstream in topology.values():
        services.update(downstream)
    if verdict := state.get("triage"):
        services.update(verdict.affected_services)
    return sorted(services)


def _merge_topology(evidence: list[Evidence]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for entry in evidence:
        if entry.tool != "get_topology":
            continue
        downstream_map: dict[str, Any] = entry.facts.get("downstream") or {}
        for service, downstream in downstream_map.items():
            merged.setdefault(str(service), list(downstream))
        for upstream in entry.facts.get("upstream") or []:
            merged.setdefault(str(upstream), []).append(str(entry.facts.get("service", "")))
    return {k: sorted(set(v)) for k, v in merged.items()}


def fresh_budget(
    max_tokens: int, max_usd: float, max_tool_calls: int, deadline_seconds: int
) -> Budget:
    return Budget(
        max_tokens=max_tokens,
        max_usd=max_usd,
        max_tool_calls=max_tool_calls,
        deadline=datetime.now(UTC) + timedelta(seconds=deadline_seconds),
    )
