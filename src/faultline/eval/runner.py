"""The benchmark runner: capsules x providers -> scorecards.

This drives the real graph, not a shortcut through it. Each run goes through
triage, prefetch, hypothesize, the evidence loop, synthesis and verification
exactly as a live incident would -- the only substitution is that the tool
gateway reads a capsule instead of Prometheus.

That substitution is the whole reason the gateway is a separate component with a
backend seam. The investigation cannot tell the difference, which is what makes
the score mean something.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from faultline.adapters.memory import InMemoryEventPublisher
from faultline.config import Settings
from faultline.core.alerts import TimeWindow, correlate
from faultline.core.state import initial_state
from faultline.eval.capsule import Capsule, builtins
from faultline.eval.scoring import Outcome, Result, Scorecard, score
from faultline.gateway.policy import TokenSigner
from faultline.gateway.registry import ToolRegistry
from faultline.logging import get_logger
from faultline.worker.graph import build_graph
from faultline.worker.models import build_router
from faultline.worker.nodes import InvestigationNodes, fresh_budget
from faultline.worker.runner import _router_kwargs

log = get_logger(__name__)


@dataclass(slots=True)
class RunConfig:
    provider: str = "stub"
    # Indexing the corpus costs seconds and the result is identical across
    # capsules, so the caller builds it once and passes it in.
    retriever: Any | None = None
    tenant_id: str = "eval"
    # Runs are scored on whether they found the answer, not on beating a clock,
    # so the deadline is generous. The token and tool ceilings still bind, and
    # are what the cost columns report against.
    deadline_seconds: int = 1800
    max_iterations: int = 4


async def run_capsule(capsule: Capsule, config: RunConfig) -> Result:
    """One capsule, one provider, one real investigation."""
    settings = Settings(
        backend="memory",
        model_provider=config.provider,  # type: ignore[arg-type]
        budget_deadline_seconds=config.deadline_seconds,
        max_iterations=config.max_iterations,
    )
    nodes = InvestigationNodes(
        router=build_router(**_router_kwargs(settings)),
        registry=ToolRegistry(capsule.scenario, retriever=config.retriever),
        publisher=InMemoryEventPublisher(),
        signer=TokenSigner(settings.gateway_signing_key),
        max_iterations=config.max_iterations,
    )
    # No checkpointer: a benchmark run is one pass and must not resume anything
    # from a previous capsule. The approval interrupt is never reached because
    # scoring stops at the report -- remediation is a human's decision, not a
    # thing to grade.
    graph = build_graph(nodes, checkpointer=None)

    alerts = capsule.alerts()
    groups = correlate(alerts, tenant_id=config.tenant_id)
    # A capsule whose alerts have all resolved produces no group at all, which is
    # the correct product behaviour and has to be a runnable case here: triage
    # closes it as noise, which is the pass on a no-fault capsule.
    window = (
        groups[0].window()
        if groups
        else TimeWindow(
            start=min(a.starts_at for a in alerts) - timedelta(minutes=30),
            end=datetime.now(UTC),
        )
    )
    state = initial_state(
        incident_id=f"eval_{capsule.name}",
        tenant_id=config.tenant_id,
        alerts=alerts,
        window=window,
        budget=fresh_budget(
            settings.budget_max_tokens,
            settings.budget_max_usd,
            settings.budget_max_tool_calls,
            config.deadline_seconds,
        ),
    )

    started = time.monotonic()
    detail = ""
    completed = True
    values: dict[str, Any] = {}
    try:
        values = await graph.ainvoke(state)
    except Exception as exc:
        completed = False
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("capsule_failed", capsule=capsule.name, provider=config.provider, error=detail)
    elapsed = time.monotonic() - started

    report = values.get("report")
    status = values.get("status")
    budget = values.get("budget")
    outcome = score(report, capsule.truth, status, completed=completed)

    if not detail and report is not None:
        verification = values.get("verification")
        if verification is not None and not verification.passed:
            detail = f"verification: {verification.summary[:120]}"

    return Result(
        capsule=capsule.name,
        provider=config.provider,
        outcome=outcome,
        expected=capsule.truth.root_cause_service,
        actual="" if report is None or report.abstained else report.root_cause_service,
        confidence=report.confidence if report else 0.0,
        tokens=budget.tokens_used if budget else 0,
        usd=budget.usd_used if budget else 0.0,
        seconds=round(elapsed, 2),
        tool_calls=budget.tool_calls_used if budget else 0,
        iterations=values.get("iteration", 0),
        detail=detail,
    )


async def run_suite(
    providers: list[str],
    capsules: list[Capsule] | None = None,
    config: RunConfig | None = None,
) -> list[Scorecard]:
    """Every capsule against every provider. The ablation table writes itself."""
    capsules = capsules if capsules is not None else builtins()
    base = config or RunConfig()
    cards: list[Scorecard] = []

    for provider in providers:
        card = Scorecard(provider=provider)
        for capsule in capsules:
            result = await run_capsule(
                capsule,
                RunConfig(
                    provider=provider,
                    retriever=base.retriever,
                    tenant_id=base.tenant_id,
                    deadline_seconds=base.deadline_seconds,
                    max_iterations=base.max_iterations,
                ),
            )
            log.info(
                "capsule_scored",
                capsule=capsule.name,
                provider=provider,
                outcome=result.outcome.value,
                seconds=result.seconds,
            )
            card.add(result)
        cards.append(card)
    return cards


class RegressionFailure(AssertionError):
    """A provider scored below its floor. Used as the CI gate."""


def assert_no_regression(card: Scorecard, min_accuracy: float, max_wrong: float = 0.0) -> None:
    """The gate: a change that makes the agent worse fails the build.

    Two thresholds, because they fail for different reasons. Accuracy dropping
    means the investigation got weaker; the wrong rate rising means it started
    asserting things -- which is the more dangerous regression and defaults to
    zero tolerance.
    """
    if card.accuracy < min_accuracy:
        raise RegressionFailure(
            f"{card.provider}: accuracy {card.accuracy:.0%} below floor {min_accuracy:.0%}"
        )
    if card.wrong_rate > max_wrong:
        wrong = [r.capsule for r in card.results if r.outcome is Outcome.WRONG]
        raise RegressionFailure(
            f"{card.provider}: wrong rate {card.wrong_rate:.0%} above ceiling "
            f"{max_wrong:.0%} on {', '.join(wrong)}"
        )
