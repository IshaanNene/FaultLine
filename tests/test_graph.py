"""Graph routing and node behavior.

Nodes are plain functions of state, so each decision is testable without
building the graph or touching a queue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_alert

from faultline.adapters.memory import InMemoryEventPublisher
from faultline.core.budget import Budget
from faultline.core.schemas import (
    Evidence,
    EvidenceKind,
    Hypothesis,
    HypothesisStatus,
    Status,
)
from faultline.core.state import (
    SCHEMA_VERSION,
    SchemaVersionMismatch,
    check_schema_version,
    initial_state,
    merge_by_id,
)
from faultline.worker.models import build_router
from faultline.worker.nodes import InvestigationNodes


@pytest.fixture
def nodes(registry, signer) -> InvestigationNodes:
    return InvestigationNodes(
        router=build_router("stub"),
        registry=registry,
        publisher=InMemoryEventPublisher(),
        signer=signer,
        max_iterations=4,
    )


def _state(budget: Budget, **overrides):
    alerts = [make_alert(), make_alert(service="checkout-service", severity="warning")]
    from faultline.core.alerts import correlate

    group = correlate(alerts, tenant_id="acme")[0]
    state = initial_state("inc_1", "acme", alerts, group.window(), budget)
    state.update(overrides)
    return state


def _hypothesis(service: str, status: HypothesisStatus, evidence: list[str], conf: float = 0.8):
    return Hypothesis(
        id=f"hyp_{service}",
        statement=f"{service} is the root cause",
        suspect_service=service,
        fault_class="bad_deploy",  # type: ignore[arg-type]
        refuting_test="t",
        status=status,
        confidence=conf,
        supporting_evidence=evidence,
    )


def _evidence(eid: str, kind: EvidenceKind) -> Evidence:
    now = datetime.now(UTC)
    return Evidence(
        id=eid,
        kind=kind,
        tool="t",
        query="q",
        window_start=now - timedelta(minutes=30),
        window_end=now,
        summary="s",
        content_hash="h",
    )


# -- state ---------------------------------------------------------------


def test_a_checkpoint_from_older_code_is_refused() -> None:
    """Better to fail loudly than to misread half an investigation."""
    with pytest.raises(SchemaVersionMismatch):
        check_schema_version({"schema_version": SCHEMA_VERSION - 1})  # type: ignore[arg-type]


def test_hypotheses_merge_by_id_preserving_order() -> None:
    a = _hypothesis("a", HypothesisStatus.OPEN, [])
    b = _hypothesis("b", HypothesisStatus.OPEN, [])
    updated_a = a.model_copy(update={"status": HypothesisStatus.REFUTED})

    merged = merge_by_id([a, b], [updated_a])
    assert [h.id for h in merged] == ["hyp_a", "hyp_b"]
    assert merged[0].status is HypothesisStatus.REFUTED


# -- triage --------------------------------------------------------------


async def test_triage_routes_actionable_alerts_to_prefetch(nodes, budget) -> None:
    state = _state(budget)
    state.update(await nodes.triage(state))
    assert state["status"] is Status.INVESTIGATING
    assert nodes.route_after_triage(state) == "prefetch"


async def test_triage_closes_a_group_of_resolved_alerts(nodes, budget) -> None:
    state = _state(budget, alerts=[make_alert(status="resolved")])
    state.update(await nodes.triage(state))
    assert state["status"] is Status.CLOSED_NOISE
    assert nodes.route_after_triage(state) == "report"


async def test_triage_charges_the_budget(nodes, budget) -> None:
    state = _state(budget)
    await nodes.triage(state)
    assert state["budget"].tokens_used > 0


# -- the conclude rule ---------------------------------------------------


def test_concluding_requires_two_independent_evidence_kinds(nodes, budget) -> None:
    """Three log queries are not two sources; a metric plus a deploy is."""
    state = _state(
        budget,
        iteration=1,
        hypotheses=[_hypothesis("checkout-service", HypothesisStatus.SUPPORTED, ["ev_1", "ev_2"])],
        evidence=[_evidence("ev_1", EvidenceKind.LOG), _evidence("ev_2", EvidenceKind.LOG)],
    )
    assert nodes.route_after_assess(state) == "plan_checks"

    state["evidence"] = [
        _evidence("ev_1", EvidenceKind.METRIC),
        _evidence("ev_2", EvidenceKind.CHANGE),
    ]
    assert nodes.route_after_assess(state) == "synthesize"


def test_an_unsettled_rival_blocks_concluding(nodes, budget) -> None:
    state = _state(
        budget,
        iteration=1,
        hypotheses=[
            _hypothesis("checkout-service", HypothesisStatus.SUPPORTED, ["ev_1", "ev_2"]),
            _hypothesis("frontend", HypothesisStatus.OPEN, []),
        ],
        evidence=[_evidence("ev_1", EvidenceKind.METRIC), _evidence("ev_2", EvidenceKind.CHANGE)],
    )
    assert nodes.route_after_assess(state) == "plan_checks"


def test_an_exhausted_budget_forces_a_report(nodes) -> None:
    spent = Budget(max_tokens=100, max_usd=1, max_tool_calls=10)
    spent.charge_model(tokens=200, usd=0.0)
    state = _state(spent, iteration=1, hypotheses=[], evidence=[])
    assert nodes.route_after_assess(state) == "report"


def test_the_iteration_cap_forces_a_report(nodes, budget) -> None:
    state = _state(budget, iteration=4, hypotheses=[], evidence=[])
    assert nodes.route_after_assess(state) == "report"


# -- tool execution ------------------------------------------------------


async def test_a_failing_tool_becomes_an_evidence_gap(nodes, budget, scenario) -> None:
    """A dead backend costs one gap, not the investigation."""
    scenario.unavailable_tools.add("search_logs")
    state = _state(budget)
    evidence, gaps = await nodes._run_tools(
        state,
        [("search_logs", {"service": "frontend"}), ("get_service_health", {"service": "frontend"})],
    )
    assert len(evidence) == 1
    assert len(gaps) == 1
    assert "unavailable" in gaps[0].reason


async def test_repeated_tool_calls_are_served_from_cache(nodes, budget) -> None:
    state = _state(budget)
    await nodes._run_tools(state, [("get_service_health", {"service": "frontend"})])
    charged_once = state["budget"].tool_calls_used
    await nodes._run_tools(state, [("get_service_health", {"service": "frontend"})])
    assert state["budget"].tool_calls_used == charged_once


async def test_tool_calls_stop_when_the_budget_runs_out(nodes) -> None:
    state = _state(Budget(max_tokens=10**9, max_usd=100, max_tool_calls=1))
    _, gaps = await nodes._run_tools(
        state,
        [
            ("get_service_health", {"service": "frontend"}),
            ("get_service_health", {"service": "checkout-service"}),
            ("get_service_health", {"service": "payment-service"}),
        ],
    )
    assert any("budget exhausted" in g.reason for g in gaps)


# -- downstream routing --------------------------------------------------


def test_a_failed_verification_retries_before_giving_up(nodes, budget) -> None:
    from faultline.core.schemas import Verification, VerificationFailure

    failed = Verification(
        passed=False, failures=[VerificationFailure(check="entailment", detail="d")], attempt=1
    )
    assert nodes.route_after_verify(_state(budget, verification=failed)) == "assess"

    exhausted = failed.model_copy(update={"attempt": 2})
    assert nodes.route_after_verify(_state(budget, verification=exhausted)) == "propose"


def test_no_proposed_action_skips_the_approval_gate(nodes, budget) -> None:
    assert nodes.route_after_propose(_state(budget, proposed_action=None)) == "report"


def test_a_rejected_action_is_never_executed(nodes, budget) -> None:
    from faultline.core.schemas import ApprovalDecision

    rejected = ApprovalDecision(action_id="act_1", decision="reject", actor="sre")
    assert nodes.route_after_approval(_state(budget, approval=rejected)) == "report"


def test_a_fix_that_did_not_work_reopens_the_investigation(nodes, budget) -> None:
    state = _state(budget, recovered=False, iteration=1)
    assert nodes.route_after_recovery(state) == "hypothesize"


def test_a_fix_that_worked_ends_the_investigation(nodes, budget) -> None:
    assert nodes.route_after_recovery(_state(budget, recovered=True)) == "report"


def test_a_failed_fix_still_stops_at_the_iteration_cap(nodes, budget) -> None:
    """The 'fix did not work' loop must be bounded like every other cycle."""
    state = _state(budget, recovered=False, iteration=4)
    assert nodes.route_after_recovery(state) == "report"


# -- termination ---------------------------------------------------------


class NeverConcludes:
    """A model that always finds the evidence inconclusive.

    Stands in for a weak model that keeps asking for one more check. The graph
    must still terminate: without a working iteration cap the evidence loop is
    bounded only by the token budget, which is hundreds of calls away.
    """

    name = "never-concludes"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, task, context):  # type: ignore[no-untyped-def]
        from faultline.core.schemas import Check, TriageVerdict
        from faultline.worker.models import Usage

        self.calls.append(task.value)
        usage = Usage(tokens=10, usd=0.0, model=self.name)
        match task.value:
            case "triage":
                return TriageVerdict(
                    classification="actionable",
                    severity="P1",
                    affected_services=["frontend"],
                    summary="s",
                ), usage
            case "hypothesize":
                return [_hypothesis("frontend", HypothesisStatus.OPEN, [])], usage
            case "plan":
                return [
                    Check(
                        id="chk_x",
                        tool="get_service_health",
                        arguments={"service": "frontend"},
                        rationale="r",
                    )
                ], usage
            case "assess":
                # Never supported, so route_after_assess always says "keep going".
                return [
                    h.model_copy(update={"status": HypothesisStatus.UNKNOWN})
                    for h in context["hypotheses"]
                ], usage
            case _:
                return "inconclusive", usage


async def test_the_evidence_loop_terminates_at_the_iteration_cap(registry, signer) -> None:
    """The regression that matters: assess routes back to plan_checks, never
    through hypothesize, so the counter has to advance in plan_checks."""
    from langgraph.checkpoint.memory import InMemorySaver

    from faultline.core.budget import Budget
    from faultline.worker.graph import build_graph
    from faultline.worker.models import ModelRouter
    from faultline.worker.serde import make_serializer

    model = NeverConcludes()
    nodes = InvestigationNodes(
        router=ModelRouter({"small": [model], "frontier": [model]}),
        registry=registry,
        publisher=InMemoryEventPublisher(),
        signer=signer,
        max_iterations=2,
    )
    graph = build_graph(nodes, InMemorySaver(serde=make_serializer()))

    # A budget far too large to be what stops this.
    state = _state(Budget(max_tokens=10**9, max_usd=10**6, max_tool_calls=10**6))
    config = {"configurable": {"thread_id": "inc_loop"}}

    async for _mode, _chunk in graph.astream(state, config, stream_mode=["updates"]):
        pass

    values = (await graph.aget_state(config)).values
    assert values["status"].is_terminal
    assert values["iteration"] == 2
    # Planning ran exactly max_iterations times, not until the tokens ran out.
    assert model.calls.count("plan") == 2


async def test_planning_advances_the_iteration_counter(nodes, budget) -> None:
    state = _state(budget, hypotheses=[_hypothesis("frontend", HypothesisStatus.OPEN, [])])
    update = await nodes.plan_checks(state)
    assert update["iteration"] == 1
