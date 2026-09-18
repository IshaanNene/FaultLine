"""The LangGraph investigation state.

Two rules shape this shape:

1. It is a TypedDict, not a Pydantic model, because LangGraph merges partial
   updates returned by nodes. Pydantic models live *inside* the fields.
2. `schema_version` is checked on resume. A worker running new code must not
   silently misread a checkpoint written by old code mid-incident.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from faultline.core.alerts import Alert, TimeWindow
from faultline.core.budget import Budget
from faultline.core.schemas import (
    ActionProposal,
    ApprovalDecision,
    Check,
    Evidence,
    EvidenceGap,
    Hypothesis,
    RCAReport,
    Status,
    TriageVerdict,
    Verification,
)

SCHEMA_VERSION = 1


class PrefetchBundle(TypedDict, total=False):
    """Deterministic context gathered in parallel before any reasoning happens."""

    changes: list[dict[str, Any]]
    topology: dict[str, list[str]]
    suspects: list[dict[str, Any]]
    runbooks: list[dict[str, Any]]
    similar_incidents: list[dict[str, Any]]


def merge_by_id(existing: list[Hypothesis], incoming: list[Hypothesis]) -> list[Hypothesis]:
    """Reducer for hypotheses: later updates to the same id win, order is preserved.

    Needed because the assess node updates statuses while the hypothesize node may
    add new candidates in a later round.
    """
    merged = {h.id: h for h in existing}
    order = [h.id for h in existing]
    for h in incoming:
        if h.id not in merged:
            order.append(h.id)
        merged[h.id] = h
    return [merged[hid] for hid in order]


class InvestigationState(TypedDict, total=False):
    incident_id: str
    tenant_id: str
    schema_version: int

    alerts: list[Alert]
    window: TimeWindow
    triage: TriageVerdict | None
    prefetch: PrefetchBundle

    hypotheses: Annotated[list[Hypothesis], merge_by_id]
    evidence: Annotated[list[Evidence], operator.add]
    gaps: Annotated[list[EvidenceGap], operator.add]
    pending_checks: list[Check]

    budget: Budget
    iteration: int

    report: RCAReport | None
    verification: Verification | None
    proposed_action: ActionProposal | None
    approval: ApprovalDecision | None
    recovered: bool | None

    status: Status
    notes: Annotated[list[str], operator.add]


def initial_state(
    incident_id: str,
    tenant_id: str,
    alerts: list[Alert],
    window: TimeWindow,
    budget: Budget,
) -> InvestigationState:
    return InvestigationState(
        incident_id=incident_id,
        tenant_id=tenant_id,
        schema_version=SCHEMA_VERSION,
        alerts=alerts,
        window=window,
        triage=None,
        prefetch=PrefetchBundle(),
        hypotheses=[],
        evidence=[],
        gaps=[],
        pending_checks=[],
        budget=budget,
        iteration=0,
        report=None,
        verification=None,
        proposed_action=None,
        approval=None,
        recovered=None,
        status=Status.TRIAGE,
        notes=[],
    )


class SchemaVersionMismatch(RuntimeError):
    """Raised when a checkpoint predates the running code's state schema."""

    def __init__(self, found: int, expected: int) -> None:
        super().__init__(f"checkpoint uses state schema v{found}, this worker speaks v{expected}")
        self.found = found
        self.expected = expected


def check_schema_version(state: InvestigationState) -> None:
    found = state.get("schema_version", 0)
    if found != SCHEMA_VERSION:
        raise SchemaVersionMismatch(found, SCHEMA_VERSION)
