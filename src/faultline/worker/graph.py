"""Graph assembly.

The shape is the blueprint's: a bounded hypothesis-test-verify loop, structured
where the process is known and agentic only where the next step genuinely depends
on what the evidence said.

Only two cycles exist -- the evidence loop (assess -> plan_checks) and the "fix
did not work" loop (confirm_recovery -> hypothesize) -- and both are bounded by
the iteration cap and the budget.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from faultline.core.state import InvestigationState
from faultline.worker.nodes import InvestigationNodes


def build_graph(
    nodes: InvestigationNodes, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> Any:
    graph = StateGraph(InvestigationState)

    graph.add_node("triage", nodes.triage)
    graph.add_node("prefetch", nodes.prefetch)
    graph.add_node("hypothesize", nodes.hypothesize)
    graph.add_node("plan_checks", nodes.plan_checks)
    graph.add_node("run_checks", nodes.run_checks)
    graph.add_node("assess", nodes.assess)
    graph.add_node("synthesize", nodes.synthesize)
    graph.add_node("verify", nodes.verify)
    graph.add_node("propose", nodes.propose)
    graph.add_node("approval", nodes.approval)
    graph.add_node("execute", nodes.execute)
    graph.add_node("confirm_recovery", nodes.confirm_recovery)
    graph.add_node("report", nodes.report)

    graph.add_edge(START, "triage")
    graph.add_conditional_edges(
        "triage", nodes.route_after_triage, {"prefetch": "prefetch", "report": "report"}
    )
    graph.add_edge("prefetch", "hypothesize")
    graph.add_edge("hypothesize", "plan_checks")
    graph.add_edge("plan_checks", "run_checks")
    graph.add_edge("run_checks", "assess")
    graph.add_conditional_edges(
        "assess",
        nodes.route_after_assess,
        {"plan_checks": "plan_checks", "synthesize": "synthesize", "report": "report"},
    )
    graph.add_edge("synthesize", "verify")
    graph.add_conditional_edges(
        "verify", nodes.route_after_verify, {"assess": "assess", "propose": "propose"}
    )
    graph.add_conditional_edges(
        "propose", nodes.route_after_propose, {"approval": "approval", "report": "report"}
    )
    graph.add_conditional_edges(
        "approval", nodes.route_after_approval, {"execute": "execute", "report": "report"}
    )
    graph.add_conditional_edges(
        "execute",
        nodes.route_after_execute,
        {"confirm_recovery": "confirm_recovery", "report": "report"},
    )
    graph.add_conditional_edges(
        "confirm_recovery",
        nodes.route_after_recovery,
        {"hypothesize": "hypothesize", "report": "report"},
    )
    graph.add_edge("report", END)

    return graph.compile(checkpointer=checkpointer)
