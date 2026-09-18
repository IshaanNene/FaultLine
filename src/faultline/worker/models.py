"""Model routing, with a deterministic stub tier.

Routing is tiered, as in the blueprint: a small model handles triage, query
writing, entailment and summaries; a frontier model handles hypothesize, plan,
assess and synthesize. Each tier declares a fallback provider, and a breaker
trips a provider out of rotation rather than retrying into a wall.

`StubModel` is not a mock returning canned strings. It applies the same
deterministic logic the real prompts ask for, so the walking skeleton produces a
genuine RCA with no API key, and so every non-model part of the system -- queue,
verification, approval, streaming -- can be tested without paying a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from faultline.core.ledger import EvidenceLedger
from faultline.core.schemas import (
    Check,
    Claim,
    Evidence,
    FaultClass,
    Hypothesis,
    HypothesisStatus,
    RCAReport,
    RejectedHypothesis,
    TriageVerdict,
)
from faultline.ids import new_id


class Task(StrEnum):
    TRIAGE = "triage"
    HYPOTHESIZE = "hypothesize"
    PLAN = "plan"
    ASSESS = "assess"
    SYNTHESIZE = "synthesize"
    ENTAIL = "entail"
    SUMMARIZE = "summarize"


# Which tier each task routes to. Getting this table wrong is the most expensive
# mistake available: triage runs on every alert, synthesize runs once.
TIER: dict[Task, str] = {
    Task.TRIAGE: "small",
    Task.HYPOTHESIZE: "frontier",
    Task.PLAN: "frontier",
    Task.ASSESS: "frontier",
    Task.SYNTHESIZE: "frontier",
    Task.ENTAIL: "small",
    Task.SUMMARIZE: "small",
}

# Rough per-call accounting for the stub, so budget arithmetic is exercised in
# tests. The live adapter reports real usage from the provider response.
STUB_USAGE: dict[Task, tuple[int, float]] = {
    Task.TRIAGE: (1_200, 0.001),
    Task.HYPOTHESIZE: (9_000, 0.045),
    Task.PLAN: (6_500, 0.032),
    Task.ASSESS: (8_000, 0.040),
    Task.SYNTHESIZE: (12_000, 0.060),
    Task.ENTAIL: (900, 0.001),
    Task.SUMMARIZE: (1_500, 0.002),
}


@dataclass(slots=True)
class Usage:
    tokens: int = 0
    usd: float = 0.0
    model: str = "stub"


class ModelUnavailable(RuntimeError):
    """Every provider for a tier is out. The graph degrades rather than fails."""


class Model(Protocol):
    name: str

    async def invoke(self, task: Task, context: dict[str, Any]) -> tuple[Any, Usage]: ...


class CircuitBreaker:
    """Open at 50% failures over a window, half-open probe after a cooldown."""

    def __init__(self, threshold: float = 0.5, window: int = 20, cooldown_s: float = 30.0) -> None:
        self._threshold = threshold
        self._window = window
        self._cooldown = cooldown_s
        self._outcomes: list[bool] = []
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if (datetime.now(UTC).timestamp() - self._opened_at) >= self._cooldown:
            self._opened_at = None  # half-open: let one call through
            self._outcomes.clear()
            return False
        return True

    def record(self, ok: bool) -> None:
        self._outcomes.append(ok)
        if len(self._outcomes) > self._window:
            self._outcomes.pop(0)
        if len(self._outcomes) >= self._window:
            failure_rate = 1 - (sum(self._outcomes) / len(self._outcomes))
            if failure_rate >= self._threshold:
                self._opened_at = datetime.now(UTC).timestamp()


class ModelRouter:
    """Picks a tier for the task, falls back to the second provider, tracks usage."""

    def __init__(self, tiers: dict[str, list[Model]]) -> None:
        self._tiers = tiers
        self._breakers: dict[str, CircuitBreaker] = {
            m.name: CircuitBreaker() for models in tiers.values() for m in models
        }
        self.usage: list[Usage] = []

    async def invoke(self, task: Task, context: dict[str, Any]) -> tuple[Any, Usage]:
        candidates = self._tiers.get(TIER[task], [])
        last_error: Exception | None = None
        for model in candidates:
            breaker = self._breakers[model.name]
            if breaker.is_open:
                continue
            try:
                result, usage = await model.invoke(task, context)
            except Exception as exc:  # provider error; try the next provider
                breaker.record(ok=False)
                last_error = exc
                continue
            breaker.record(ok=True)
            self.usage.append(usage)
            return result, usage
        raise ModelUnavailable(f"no provider available for {task}") from last_error

    @property
    def total(self) -> Usage:
        return Usage(
            tokens=sum(u.tokens for u in self.usage),
            usd=round(sum(u.usd for u in self.usage), 4),
        )


class StubModel:
    """Deterministic reasoning over the evidence ledger.

    Each branch mirrors what the corresponding prompt asks a real model to do, so
    swapping in `AnthropicModel` changes the quality of the judgment, not the
    shape of the graph.
    """

    name = "stub"

    async def invoke(self, task: Task, context: dict[str, Any]) -> tuple[Any, Usage]:
        tokens, usd = STUB_USAGE[task]
        handler = getattr(self, f"_{task.value}")
        return handler(context), Usage(tokens=tokens, usd=usd, model=self.name)

    # -- tasks ------------------------------------------------------------

    def _triage(self, ctx: dict[str, Any]) -> TriageVerdict:
        alerts = ctx["alerts"]
        firing = [a for a in alerts if a.is_firing]
        services = sorted({a.service for a in firing if a.service})
        if not firing:
            return TriageVerdict(
                classification="noise",
                severity="P4",
                affected_services=[],
                summary="All alerts in the group are resolved.",
            )
        severity = min((a.severity for a in firing), key=lambda s: s.rank)
        names = sorted({a.name for a in firing})
        return TriageVerdict(
            classification="actionable",
            severity=severity.value,
            affected_services=services,
            summary=(
                f"{len(firing)} firing alert(s) ({', '.join(names)}) "
                f"affecting {', '.join(services) or 'unknown services'}."
            ),
        )

    def _hypothesize(self, ctx: dict[str, Any]) -> list[Hypothesis]:
        """Competing, falsifiable hypotheses -- never a single leading candidate.

        Forcing at least two is the structural counter to anchoring: the plan node
        can only pick discriminating checks if there is something to discriminate.
        """
        suspects: list[dict[str, Any]] = ctx.get("suspects", [])
        changed: set[str] = set(ctx.get("changed_services", []))
        alert_services: list[str] = ctx.get("alert_services", [])
        existing: list[Hypothesis] = ctx.get("existing", [])
        seen = {h.suspect_service for h in existing}

        candidates = [s["service"] for s in suspects] or alert_services
        out: list[Hypothesis] = []
        for service in candidates[:3]:
            if service in seen:
                continue
            fault = FaultClass.BAD_DEPLOY if service in changed else FaultClass.DEPENDENCY_FAILURE
            statement = (
                f"A recent change to {service} is the root cause"
                if service in changed
                else f"{service} is failing because one of its dependencies degraded"
            )
            refuting = (
                f"A change to {service} landing after the first bad minute, or no change at all"
                if service in changed
                else f"All of {service}'s dependencies healthy through the window"
            )
            out.append(
                Hypothesis(
                    id=new_id("hyp"),
                    statement=statement,
                    suspect_service=service,
                    fault_class=fault,
                    refuting_test=refuting,
                )
            )
        if len(out) + len(existing) < 2 and candidates:
            out.append(
                Hypothesis(
                    id=new_id("hyp"),
                    statement=(
                        "The failure originates outside the traced system "
                        "(external dependency or network)"
                    ),
                    suspect_service=candidates[0],
                    fault_class=FaultClass.EXTERNAL,
                    refuting_test=(
                        "An internal change or internal error template explains the onset"
                    ),
                )
            )
        return out

    def _plan(self, ctx: dict[str, Any]) -> list[Check]:
        """Prefer checks whose outcome differs most between the top two hypotheses."""
        hypotheses: list[Hypothesis] = ctx["hypotheses"]
        already: set[str] = set(ctx.get("already_run", []))
        remaining: int = ctx.get("remaining_tool_calls", 10)

        open_hyps = [h for h in hypotheses if h.status == HypothesisStatus.OPEN][:2]
        checks: list[Check] = []
        for hypothesis in open_hyps:
            service = hypothesis.suspect_service
            plan: list[tuple[str, dict[str, Any]]] = [
                ("get_service_health", {"service": service, "window_minutes": 30}),
                ("get_recent_changes", {"service": service, "window_minutes": 60}),
                ("search_logs", {"service": service, "level": "ERROR", "window_minutes": 30}),
                ("get_k8s_state", {"service": service}),
                ("get_trace_summary", {"service": service, "window_minutes": 30}),
            ]
            for tool, args in plan:
                key = f"{tool}:{sorted(args.items())}"
                if key in already:
                    continue
                checks.append(
                    Check(
                        id=new_id("chk"),
                        tool=tool,
                        arguments=args,
                        targets_hypotheses=[hypothesis.id],
                        rationale=f"separates {hypothesis.statement!r} from its rivals",
                    )
                )
        return checks[:remaining]

    def _assess(self, ctx: dict[str, Any]) -> list[Hypothesis]:
        """Mark each hypothesis supported, refuted or unknown from the ledger alone."""
        hypotheses: list[Hypothesis] = ctx["hypotheses"]
        ledger: EvidenceLedger = ctx["ledger"]

        by_service: dict[str, list[Evidence]] = {}
        for entry in ledger:
            for service in evidence_services(entry):
                by_service.setdefault(service, []).append(entry)

        updated: list[Hypothesis] = []
        for hypothesis in hypotheses:
            entries = by_service.get(hypothesis.suspect_service, [])
            if not entries:
                updated.append(hypothesis.model_copy(update={"status": HypothesisStatus.UNKNOWN}))
                continue

            supporting = [e.id for e in entries if _is_supporting(e)]
            anomalous = any(e.facts.get("anomalous") is True for e in entries)
            changed = any(int(e.facts.get("changes", 0) or 0) > 0 for e in entries)
            victim = any(e.facts.get("error_downstreams") for e in entries)
            new_errors = any(e.facts.get("new_templates") for e in entries)

            if anomalous and (changed or new_errors) and not victim:
                status = HypothesisStatus.SUPPORTED
                confidence = 0.85 if changed and new_errors else 0.65
            elif victim or not anomalous:
                status = HypothesisStatus.REFUTED
                confidence = 0.1
            else:
                status = HypothesisStatus.UNKNOWN
                confidence = 0.4

            updated.append(
                hypothesis.model_copy(
                    update={
                        "status": status,
                        "confidence": confidence,
                        "supporting_evidence": supporting
                        if status == HypothesisStatus.SUPPORTED
                        else [],
                        "refuting_evidence": [e.id for e in entries]
                        if status == HypothesisStatus.REFUTED
                        else [],
                    }
                )
            )
        return updated

    def _synthesize(self, ctx: dict[str, Any]) -> RCAReport:
        hypotheses: list[Hypothesis] = ctx["hypotheses"]
        ledger: EvidenceLedger = ctx["ledger"]
        gaps: list[str] = ctx.get("gaps", [])
        topology: dict[str, list[str]] = ctx.get("topology", {})

        supported = [h for h in hypotheses if h.status == HypothesisStatus.SUPPORTED]
        supported.sort(key=lambda h: h.confidence, reverse=True)

        if not supported:
            # Abstention is a first-class outcome, not a failure to produce one.
            best = sorted(hypotheses, key=lambda h: h.confidence, reverse=True)[:2]
            return RCAReport(
                root_cause_service=best[0].suspect_service if best else "unknown",
                fault_class=FaultClass.UNKNOWN,
                mechanism="Insufficient evidence to identify a root cause.",
                confidence=0.2,
                abstained=True,
                rejected_hypotheses=[
                    RejectedHypothesis(statement=h.statement, reason="not established by evidence")
                    for h in best
                ],
                evidence_gaps=gaps,
            )

        lead = supported[0]
        service = lead.suspect_service
        entries = [e for e in ledger if service in evidence_services(e)]

        claims: list[Claim] = []
        change_entry = next((e for e in entries if e.tool == "get_recent_changes"), None)
        health_entry = next((e for e in entries if e.tool == "get_service_health"), None)
        log_entry = next((e for e in entries if e.tool == "search_logs"), None)

        if change_entry:
            claims.append(
                Claim(
                    text=f"{service} changed shortly before the first bad minute: "
                    f"{change_entry.summary.splitlines()[-1]}",
                    evidence_ids=[change_entry.id],
                )
            )
        if health_entry:
            claims.append(
                Claim(
                    text=f"{service} error rate and latency departed from baseline: "
                    f"{health_entry.summary}",
                    evidence_ids=[health_entry.id],
                )
            )
        if log_entry and log_entry.facts.get("new_templates"):
            new_template = str(log_entry.facts["new_templates"][0])
            claims.append(
                Claim(
                    text=f"{service} began emitting an error template it had never emitted "
                    f"before: {new_template}",
                    evidence_ids=[log_entry.id],
                )
            )

        victims = sorted(
            {
                str(e.facts["service"])
                for e in ledger
                if e.facts.get("error_downstreams")
                and service in list(e.facts["error_downstreams"])
            }
        )
        blast = sorted({service, *victims, *(s for s, deps in topology.items() if service in deps)})

        first_bad = min((e.window_start for e in entries), default=None)
        change_points = [
            datetime.fromisoformat(cp)
            for e in entries
            for cp in list(e.facts.get("change_points", []))
        ]
        if change_points:
            first_bad = min(change_points)

        rejected = [
            RejectedHypothesis(
                statement=h.statement,
                reason="refuted: anomaly is downstream of a failing dependency"
                if h.refuting_evidence
                else "not supported by evidence",
                evidence_ids=h.refuting_evidence,
            )
            for h in hypotheses
            if h.status != HypothesisStatus.SUPPORTED
        ]

        return RCAReport(
            root_cause_service=service,
            fault_class=lead.fault_class,
            mechanism=(
                f"{service} was changed at the onset of the incident and started "
                f"failing with a new error signature; its callers "
                f"({', '.join(victims) or 'downstream services'}) surface those "
                "failures to users."
            ),
            causal_chain=claims,
            blast_radius=blast,
            first_bad_at=first_bad,
            confidence=lead.confidence,
            rejected_hypotheses=rejected,
            evidence_gaps=gaps,
        )

    def _entail(self, ctx: dict[str, Any]) -> bool:
        """Cheap stand-in for the entailment judge: does the claim's content
        actually appear in the evidence it cites?"""
        claim: Claim = ctx["claim"]
        ledger: EvidenceLedger = ctx["ledger"]
        cited = " ".join(
            (e.summary + " " + str(e.facts)) for eid in claim.evidence_ids if (e := ledger.get(eid))
        ).lower()
        if not cited:
            return False
        tokens = [t for t in _significant_tokens(claim.text) if len(t) > 3]
        if not tokens:
            return True
        overlap = sum(1 for t in tokens if t in cited) / len(tokens)
        return overlap >= 0.5

    def _summarize(self, ctx: dict[str, Any]) -> str:
        report: RCAReport | None = ctx.get("report")
        if report is None or report.abstained:
            return "Investigation closed without a confirmed root cause."
        return (
            f"Root cause: {report.root_cause_service} ({report.fault_class}). "
            f"{report.mechanism} Confidence {report.confidence:.0%}."
        )


def evidence_services(entry: Evidence) -> set[str]:
    """Which services a piece of evidence speaks about.

    Most tools report a single `service`; `get_recent_changes` reports a list,
    because one query can cover several. Both shapes have to resolve, or evidence
    silently fails to attach to the hypothesis it supports.
    """
    services: set[str] = set()
    if single := entry.facts.get("service"):
        services.add(str(single))
    for many in list(entry.facts.get("services", []) or []):
        services.add(str(many))
    return services


def _is_supporting(entry: Evidence) -> bool:
    return bool(
        entry.facts.get("anomalous")
        or entry.facts.get("new_templates")
        or int(entry.facts.get("changes", 0) or 0) > 0
    )


def _significant_tokens(text: str) -> list[str]:
    stop = {
        "the",
        "and",
        "that",
        "with",
        "from",
        "into",
        "than",
        "this",
        "have",
        "began",
        "before",
        "never",
        "shortly",
        "departed",
        "error",
        "rate",
    }
    words = "".join(c.lower() if c.isalnum() or c in "-_." else " " for c in text).split()
    return [w for w in words if w not in stop]


def build_router(provider: str = "stub") -> ModelRouter:
    if provider == "stub":
        stub = StubModel()
        return ModelRouter({"small": [stub], "frontier": [stub]})
    raise NotImplementedError(
        f"provider {provider!r} is not wired yet; the Anthropic adapter implements "
        "Model.invoke by rendering each Task to a prompt and requesting the matching "
        "Pydantic schema as structured output"
    )
