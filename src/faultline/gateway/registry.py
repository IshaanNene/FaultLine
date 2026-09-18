"""Tool definitions and dispatch.

Four principles, from the blueprint, are enforced here rather than requested in a
prompt:

1. Intent-shaped tools, not raw query languages. Models write invalid PromQL;
   these tools do the querying and the statistics in Python.
2. Compress at the source. Every tool returns a summary and a handful of facts.
   Raw payloads go to object storage and are referenced by hash, never inlined.
3. Every result becomes one ledger entry with an id, a normalized query, a time
   range and a content hash.
4. Statistics in code, judgment in the model. Change points, baseline comparison
   and suspect ranking are deterministic; what they mean is the model's job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from faultline.core.alerts import TimeWindow
from faultline.core.schemas import (
    ActionKind,
    ActionProposal,
    ApprovalMode,
    Evidence,
    EvidenceKind,
)
from faultline.gateway.backends.scenario import Scenario
from faultline.gateway.policy import (
    ApprovalGrant,
    Capability,
    PolicyError,
    looks_like_injection,
    redact,
)
from faultline.ids import content_hash, new_id

# Read-only tools the investigation may call, and the evidence kind each yields.
READ_TOOLS: dict[str, EvidenceKind] = {
    "get_service_health": EvidenceKind.METRIC,
    "search_logs": EvidenceKind.LOG,
    "get_trace_summary": EvidenceKind.TRACE,
    "get_topology": EvidenceKind.TOPOLOGY,
    "get_recent_changes": EvidenceKind.CHANGE,
    "get_k8s_state": EvidenceKind.K8S_STATE,
    "rank_suspects": EvidenceKind.METRIC,
    "search_knowledge": EvidenceKind.KNOWLEDGE,
    "find_similar_incidents": EvidenceKind.PAST_INCIDENT,
}

# Write tools. These never execute from the investigation loop; they produce a
# proposal, and only the execute node with an approval grant can run them.
WRITE_TOOLS = {"rollback", "set_flag", "scale", "restart"}

MAX_WINDOW = timedelta(hours=6)
MAX_LOG_TEMPLATES = 8
MAX_TOPOLOGY_DEPTH = 3


class ToolError(RuntimeError):
    """A tool could not answer. Becomes an evidence gap, never a silent hole."""


class ToolRegistry:
    """Dispatches tool calls against a backend and returns compressed evidence."""

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario

    # -- dispatch ---------------------------------------------------------

    def call(self, tool: str, arguments: dict[str, Any], capability: Capability) -> Evidence:
        if tool in WRITE_TOOLS:
            raise PolicyError(f"{tool} is a write action and needs an approval token")
        if tool not in READ_TOOLS:
            raise ToolError(f"unknown tool: {tool}")
        namespace = str(arguments.get("namespace") or self._scenario.namespace)
        if not capability.allows(tool, namespace):
            raise PolicyError(f"capability does not allow {tool} in {namespace}")
        if tool in self._scenario.unavailable_tools:
            raise ToolError(f"{tool} backend is unavailable")

        handler = getattr(self, f"_tool_{tool}")
        summary, facts = handler(arguments)
        return self._to_evidence(tool, arguments, summary, facts)

    def _to_evidence(
        self, tool: str, arguments: dict[str, Any], summary: str, facts: dict[str, Any]
    ) -> Evidence:
        window = self._window(arguments)
        # Redaction runs on everything before it can reach a prompt, and the
        # injection heuristic runs on the redacted text so a flag is never lost
        # to a redaction.
        summary = redact(summary)
        facts = _redact_facts(facts)
        flagged = looks_like_injection(summary) or any(
            looks_like_injection(str(v)) for v in facts.values()
        )
        query = _normalize_query(tool, arguments)
        return Evidence(
            id=new_id("ev"),
            kind=READ_TOOLS[tool],
            tool=tool,
            query=query,
            window_start=window.start,
            window_end=window.end,
            summary=summary,
            facts=facts,
            content_hash=content_hash(tool, query, summary, str(sorted(facts.items()))),
            raw_ref=f"s3://faultline-capsules/{self._scenario.name}/{tool}/{query}",
            injection_flagged=flagged,
        )

    def _window(self, arguments: dict[str, Any]) -> TimeWindow:
        end = datetime.now(UTC)
        minutes = int(arguments.get("window_minutes", 30))
        return TimeWindow(start=end - timedelta(minutes=minutes), end=end).clamp(MAX_WINDOW)

    # -- read tools -------------------------------------------------------

    def _tool_get_service_health(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        service = str(args.get("service", ""))
        series = [m for m in self._scenario.effective_metrics() if m.service == service]
        if not series:
            raise ToolError(f"no metrics for service {service!r}")
        anomalies = [m for m in series if m.anomalous]
        if not anomalies:
            return (
                f"{service}: rate, errors and duration all within baseline.",
                {"service": service, "anomalous": False},
            )
        parts = [
            f"{m.name} {m.baseline:g}{m.unit} -> {m.current:g}{m.unit} "
            f"({m.delta_ratio:+.0%}, change point {m.change_point:%H:%M} UTC)"
            for m in anomalies
        ]
        return (
            f"{service}: " + "; ".join(parts),
            {
                "service": service,
                "anomalous": True,
                "change_points": [m.change_point.isoformat() for m in anomalies if m.change_point],
                "worst_delta_ratio": max(abs(m.delta_ratio) for m in anomalies),
            },
        )

    def _tool_search_logs(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        service = str(args.get("service", ""))
        level = str(args.get("level", "")).upper()
        templates = [
            t
            for t in self._scenario.logs
            if t.service == service and (not level or t.level == level)
        ]
        if not templates:
            return (f"{service}: no log templates matched.", {"service": service, "templates": 0})
        templates.sort(key=lambda t: (not t.is_new, -t.count_window))
        shown = templates[:MAX_LOG_TEMPLATES]
        lines = [
            f"{'NEW ' if t.is_new else ''}{t.level} x{t.count_window} "
            f"(baseline {t.count_baseline}): {t.template} | e.g. {t.example}"
            for t in shown
        ]
        return (
            f"{service}: {len(shown)} templates.\n" + "\n".join(lines),
            {
                "service": service,
                "templates": len(shown),
                "new_templates": [t.template for t in shown if t.is_new],
                "examples": [t.example for t in shown],
            },
        )

    def _tool_get_trace_summary(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        service = str(args.get("service", ""))
        downstream = self._scenario.downstreams(service)
        errored = [
            d
            for d in downstream
            if any(m.service == d and m.anomalous for m in self._scenario.effective_metrics())
        ]
        latency = next(
            (
                m
                for m in self._scenario.effective_metrics()
                if m.service == service and "latency" in m.name and m.anomalous
            ),
            None,
        )
        if not errored and latency is None:
            return (
                f"{service}: no error spans or latency outliers above baseline.",
                {"service": service, "error_downstreams": []},
            )
        detail = f"slowest critical path {service} -> {errored[0]}" if errored else "self time"
        return (
            f"{service}: error spans concentrated in calls to "
            f"{', '.join(errored) or 'none'}; {detail}"
            + (
                f"; p99 {latency.current:g}ms vs {latency.baseline:g}ms baseline" if latency else ""
            ),
            {"service": service, "error_downstreams": errored},
        )

    def _tool_get_topology(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        service = str(args.get("service", ""))
        depth = min(int(args.get("depth", 1)), MAX_TOPOLOGY_DEPTH)
        seen: dict[str, list[str]] = {}
        frontier = [service]
        for _ in range(depth):
            nxt: list[str] = []
            for node in frontier:
                if node in seen:
                    continue
                seen[node] = self._scenario.downstreams(node)
                nxt.extend(seen[node])
            frontier = nxt
        upstreams = self._scenario.upstreams(service)
        return (
            f"{service} calls {', '.join(seen.get(service, [])) or 'nothing'}; "
            f"called by {', '.join(upstreams) or 'nothing'}.",
            {"service": service, "downstream": seen, "upstream": upstreams},
        )

    def _tool_get_recent_changes(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        window = self._window(args)
        service = args.get("service")
        changes = [
            c
            for c in self._scenario.changes
            if window.start <= c.at <= window.end and (not service or c.service == service)
        ]
        if not changes:
            return ("No deploys, flag flips or config changes in the window.", {"changes": 0})
        changes.sort(key=lambda c: c.at)
        lines = [f"{c.at:%H:%M} UTC {c.kind} {c.service}: {c.description}" for c in changes]
        facts: dict[str, Any] = {
            "changes": len(changes),
            "services": sorted({c.service for c in changes}),
            "earliest": changes[0].at.isoformat(),
            "detail": [c.detail for c in changes],
        }
        # A service-scoped query tags the evidence with that service, so the
        # synthesizer can find the deploy that explains a specific suspect. Without
        # this the strongest signal in the whole investigation -- "it changed right
        # before it broke" -- never reaches the report.
        if service:
            facts["service"] = str(service)
        return (f"{len(changes)} change(s) in window:\n" + "\n".join(lines), facts)

    def _tool_get_k8s_state(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        service = str(args.get("service", ""))
        workload = next((w for w in self._scenario.workloads if w.service == service), None)
        if workload is None:
            raise ToolError(f"no workload found for {service!r}")
        return (
            f"{service}: {workload.replicas_ready}/{workload.replicas_desired} ready, "
            f"{workload.restarts} restarts, image {workload.image}"
            + (
                f", last terminated {workload.last_terminated_reason}"
                if workload.last_terminated_reason
                else ""
            )
            # Names only. Reading a value would mean reading a Secret, and the
            # gateway's RBAC deliberately cannot.
            + f". Env keys: {', '.join(workload.env_keys) or 'none'}",
            {
                "service": service,
                "image": workload.image,
                "restarts": workload.restarts,
                "healthy": workload.replicas_ready == workload.replicas_desired,
            },
        )

    def _tool_rank_suspects(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        ranked = self.rank_suspects()
        lines = [
            f"{i + 1}. {s['service']} (score {s['score']:.1f}) {s['why']}"
            for i, s in enumerate(ranked[:5])
        ]
        return (
            "Deterministic suspect ranking:\n" + "\n".join(lines),
            {"ranking": ranked[:5]},
        )

    def _tool_search_knowledge(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        # Placeholder until the hybrid BM25 + pgvector retriever lands. It abstains
        # rather than inventing a runbook, which is the behavior the real one must
        # keep when its rerank score falls below threshold.
        query = str(args.get("query", ""))
        return (
            f"No runbook section passed the relevance threshold for {query!r}. "
            "Knowledge gap recorded.",
            {"query": query, "hits": 0, "knowledge_gap": True},
        )

    def _tool_find_similar_incidents(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return (
            "No confirmed past incident matches this signature.",
            {"hits": 0},
        )

    # -- deterministic analytics -----------------------------------------

    def rank_suspects(self) -> list[dict[str, Any]]:
        """Blame flows toward dependencies.

        A service that is anomalous while everything it calls is healthy is a
        cause. A service that is anomalous because something it calls is
        anomalous is a victim, so its score is divided by its worst downstream.
        This is the cheap structural answer to the anchoring failure: the loudest
        alert is usually the victim.
        """
        incident_start = self._scenario.incident_start
        anomaly: dict[str, float] = {}
        for metric in self._scenario.effective_metrics():
            # Bystander anomalies -- out of baseline since long before the first
            # alert -- are excluded by the scenario's shared rule.
            if not self._scenario.is_incident_anomaly(metric):
                continue
            anomaly[metric.service] = max(anomaly.get(metric.service, 0.0), abs(metric.delta_ratio))

        changed = {
            c.service
            for c in self._scenario.changes
            if c.at >= incident_start - timedelta(minutes=30)
        }

        ranked: list[dict[str, Any]] = []
        for service, score in anomaly.items():
            worst_downstream = max(
                (anomaly.get(d, 0.0) for d in self._scenario.downstreams(service)), default=0.0
            )
            adjusted = score * (1.5 if service in changed else 1.0) / (1.0 + worst_downstream)
            why = []
            if service in changed:
                why.append("changed just before the incident")
            if worst_downstream > 0:
                why.append("anomalous dependency, likely downstream victim")
            if not why:
                why.append("anomalous with healthy dependencies")
            ranked.append({"service": service, "score": adjusted, "why": "; ".join(why)})

        ranked.sort(key=lambda r: r["score"], reverse=True)
        return ranked

    # -- write path -------------------------------------------------------

    def propose_action(self, root_cause_service: str, fault_class: str) -> ActionProposal | None:
        """Map a root cause to a catalog action. The catalog is closed by design:
        the model picks from it, it never writes a command."""
        change = next(
            (
                c
                for c in self._scenario.changes
                if c.service == root_cause_service and c.kind == "deploy"
            ),
            None,
        )
        if fault_class == "bad_deploy" and change is not None:
            return ActionProposal(
                id=new_id("act"),
                kind=ActionKind.ROLLBACK,
                target=f"{self._scenario.namespace}/{root_cause_service}",
                arguments={
                    "to_revision": change.detail.get("previous", "previous"),
                    "from_revision": change.detail.get("image", "current"),
                },
                rationale=(
                    f"Roll {root_cause_service} back to "
                    f"{change.detail.get('previous', 'the previous revision')}, "
                    "the last revision running before the first bad minute."
                ),
                reversible=True,
                blast_radius=[root_cause_service, *self._scenario.upstreams(root_cause_service)],
                approval_mode=ApprovalMode.REQUIRES_APPROVAL,
            )
        return None

    def execute(self, action: ActionProposal, grant: ApprovalGrant, idempotency_key: str) -> str:
        """Run an approved write. Every argument is re-checked against the grant:
        a token for one action must not execute a different one."""
        if grant.action_id != action.id:
            raise PolicyError("approval grant does not match this action")
        if grant.target != action.target:
            raise PolicyError("approval grant is bound to a different target")
        if grant.expires_at <= datetime.now(UTC):
            raise PolicyError("approval grant expired")
        # The skeleton applies the change to the scenario; the live adapter issues
        # the rollout undo against the cluster. Either way `confirm_recovery` has
        # to read the world back rather than assume the fix worked.
        if action.kind == ActionKind.ROLLBACK:
            self._scenario.remediate(action.target.split("/")[-1])
        return (
            f"executed {action.kind} on {action.target} "
            f"(approved by {grant.approver}, idempotency key {idempotency_key})"
        )


def _normalize_query(tool: str, arguments: dict[str, Any]) -> str:
    """Stable rendering of a call, used for the per-incident tool cache key and
    for making two identical calls visibly identical in the ledger."""
    items = sorted((k, v) for k, v in arguments.items() if v is not None)
    return f"{tool}(" + ", ".join(f"{k}={v}" for k, v in items) + ")"


def _redact_facts(facts: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in facts.items():
        if isinstance(value, str):
            out[key] = redact(value)
        elif isinstance(value, list):
            out[key] = [redact(v) if isinstance(v, str) else v for v in value]
        else:
            out[key] = value
    return out
