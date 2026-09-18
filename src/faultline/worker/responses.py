"""Model-facing response schemas.

These are deliberately *not* the domain types from `core.schemas`. Two reasons:

1. Structured outputs need a closed JSON Schema. Domain types carry
   `dict[str, Any]` fields (`Evidence.facts`, `Check.arguments`) that cannot be
   expressed under `additionalProperties: false`.
2. A narrow schema is a guardrail. `CheckRequest.tool` is an enum, so the model
   cannot invent a tool; `CheckArguments` has typed fields, so it cannot invent
   an argument. The node adapts these into domain objects, which is the only
   place model output becomes system state.

Nothing here is ever trusted as authority -- the verify node still checks every
claim against the ledger.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from faultline.core.schemas import FaultClass, HypothesisStatus
from faultline.worker.models import Task


class ReadTool(StrEnum):
    """The tools an investigation may ask for.

    Kept as a literal enum rather than generated from the registry so the JSON
    Schema is byte-stable across processes -- a schema that reorders between
    runs would invalidate the prompt cache. `test_models_live.py` asserts it
    stays in step with the registry.
    """

    GET_SERVICE_HEALTH = "get_service_health"
    SEARCH_LOGS = "search_logs"
    GET_TRACE_SUMMARY = "get_trace_summary"
    GET_TOPOLOGY = "get_topology"
    GET_RECENT_CHANGES = "get_recent_changes"
    GET_K8S_STATE = "get_k8s_state"
    RANK_SUSPECTS = "rank_suspects"
    SEARCH_KNOWLEDGE = "search_knowledge"
    FIND_SIMILAR_INCIDENTS = "find_similar_incidents"


class SeverityLevel(StrEnum):
    """Constrained rather than described.

    This was a free string with a "P1, P2, P3 or P4" description until a local
    model answered "critical". A description is a request; an enum is a
    constraint, and the weaker the model the more that distinction matters.
    """

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class TriageClassification(StrEnum):
    NOISE = "noise"
    DUPLICATE = "duplicate"
    ACTIONABLE = "actionable"


class AssessDecision(StrEnum):
    CONTINUE = "continue"
    CONCLUDE = "conclude"
    STOP = "stop"


# -- triage ---------------------------------------------------------------


class TriageResponse(BaseModel):
    classification: TriageClassification
    severity: SeverityLevel
    affected_services: list[str]
    summary: str = Field(description="One sentence an on-call engineer can act on.")


# -- hypothesize ----------------------------------------------------------


class HypothesisDraft(BaseModel):
    statement: str
    suspect_service: str
    fault_class: FaultClass
    refuting_test: str = Field(description="A check whose outcome would disprove this hypothesis.")


class HypothesesResponse(BaseModel):
    hypotheses: list[HypothesisDraft] = Field(
        description="At least two competing explanations, most plausible first."
    )


# -- plan -----------------------------------------------------------------


class CheckArguments(BaseModel):
    """A closed set of tool arguments. Unset fields are omitted from the call."""

    service: str | None = None
    window_minutes: int | None = None
    level: str | None = None
    depth: int | None = None
    query: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class CheckRequest(BaseModel):
    tool: ReadTool
    arguments: CheckArguments
    targets_hypotheses: list[str] = Field(description="Ids of the hypotheses this check separates.")
    rationale: str


class PlanResponse(BaseModel):
    checks: list[CheckRequest]


# -- assess ---------------------------------------------------------------


class Assessment(BaseModel):
    hypothesis_id: str
    status: HypothesisStatus
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(description="Evidence ids that support it.")
    refuting_evidence: list[str] = Field(description="Evidence ids that argue against it.")
    reasoning: str


class AssessResponse(BaseModel):
    assessments: list[Assessment]
    decision: AssessDecision


# -- synthesize -----------------------------------------------------------


class ClaimDraft(BaseModel):
    text: str
    evidence_ids: list[str] = Field(description="Every claim must cite at least one.")


class RejectedDraft(BaseModel):
    statement: str
    reason: str
    evidence_ids: list[str]


class SynthesisResponse(BaseModel):
    """The drafted report.

    No `proposed_actions` field: remediation comes from the action catalog in the
    propose node, never from the model. Leaving it out of the schema means the
    model cannot suggest a command even if it wants to.
    """

    root_cause_service: str
    fault_class: FaultClass
    mechanism: str
    causal_chain: list[ClaimDraft]
    blast_radius: list[str]
    first_bad_at: str | None = Field(
        default=None, description="ISO-8601 timestamp of the first bad minute, or null."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    rejected_hypotheses: list[RejectedDraft]
    evidence_gaps: list[str]
    abstained: bool = Field(description="True when the evidence does not support any root cause.")


# -- entail ---------------------------------------------------------------


class EntailmentResponse(BaseModel):
    supported: bool
    reason: str


# -- summarize ------------------------------------------------------------


class SummaryResponse(BaseModel):
    summary: str


# Which schema each task parses into. Provider-agnostic: every live tier asks
# for the same structured output, so the graph cannot tell them apart.
SCHEMA: dict[Task, type[BaseModel]] = {
    Task.TRIAGE: TriageResponse,
    Task.HYPOTHESIZE: HypothesesResponse,
    Task.PLAN: PlanResponse,
    Task.ASSESS: AssessResponse,
    Task.SYNTHESIZE: SynthesisResponse,
    Task.ENTAIL: EntailmentResponse,
    Task.SUMMARIZE: SummaryResponse,
}
