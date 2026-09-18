"""The structured outputs of an investigation.

Everything the agent concludes is a typed object with references into the
evidence ledger. That is what makes verification mechanical rather than a matter
of reading prose: a claim with no `evidence_ids` cannot survive the verify node.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

EvidenceId = str
ServiceName = str


class FaultClass(StrEnum):
    BAD_DEPLOY = "bad_deploy"
    CONFIG_ERROR = "config_error"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    DEPENDENCY_FAILURE = "dependency_failure"
    NETWORK = "network"
    DATA_QUALITY = "data_quality"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):
    """Independent evidence types. The conclude rule requires two *different* kinds."""

    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    CHANGE = "change"
    K8S_STATE = "k8s_state"
    TOPOLOGY = "topology"
    KNOWLEDGE = "knowledge"
    PAST_INCIDENT = "past_incident"


class Evidence(BaseModel):
    """One compressed tool result, appended to the ledger and never mutated."""

    id: EvidenceId
    kind: EvidenceKind
    tool: str
    query: str
    window_start: datetime
    window_end: datetime
    summary: str
    facts: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    raw_ref: str | None = None
    injection_flagged: bool = False
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvidenceGap(BaseModel):
    """A source that could not be read. Reported, never silently dropped."""

    tool: str
    reason: str
    window_start: datetime | None = None
    window_end: datetime | None = None

    def __str__(self) -> str:
        return f"{self.tool}: {self.reason}"


class HypothesisStatus(StrEnum):
    OPEN = "open"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNKNOWN = "unknown"


class Hypothesis(BaseModel):
    id: str
    statement: str
    suspect_service: ServiceName
    fault_class: FaultClass
    refuting_test: str = Field(
        description="The check that would disprove this. Required, to force falsifiable hypotheses."
    )
    status: HypothesisStatus = HypothesisStatus.OPEN
    confidence: float = 0.0
    supporting_evidence: list[EvidenceId] = Field(default_factory=list)
    refuting_evidence: list[EvidenceId] = Field(default_factory=list)

    @property
    def evidence_kinds(self) -> set[str]:
        return set()


class Check(BaseModel):
    """A planned tool call, chosen for how well it separates competing hypotheses."""

    id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    targets_hypotheses: list[str] = Field(default_factory=list)
    rationale: str = ""


class Claim(BaseModel):
    text: str
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class RejectedHypothesis(BaseModel):
    statement: str
    reason: str
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class ActionKind(StrEnum):
    ROLLBACK = "rollback"
    SET_FLAG = "set_flag"
    SCALE = "scale"
    RESTART = "restart"
    NONE = "none"


class ApprovalMode(StrEnum):
    AUTO = "auto"
    REQUIRES_APPROVAL = "requires_approval"
    DENIED = "denied"


class ActionProposal(BaseModel):
    """A proposal only. Nothing in the graph can execute this without a token."""

    id: str
    kind: ActionKind
    target: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    reversible: bool = True
    blast_radius: list[ServiceName] = Field(default_factory=list)
    approval_mode: ApprovalMode = ApprovalMode.REQUIRES_APPROVAL


class RCAReport(BaseModel):
    root_cause_service: ServiceName
    fault_class: FaultClass
    mechanism: str
    causal_chain: list[Claim] = Field(default_factory=list)
    blast_radius: list[ServiceName] = Field(default_factory=list)
    first_bad_at: datetime | None = None
    confidence: float = 0.0
    rejected_hypotheses: list[RejectedHypothesis] = Field(default_factory=list)
    proposed_actions: list[ActionProposal] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    abstained: bool = False

    @model_validator(mode="after")
    def _confidence_in_range(self) -> RCAReport:
        self.confidence = max(0.0, min(1.0, self.confidence))
        return self


class VerificationFailure(BaseModel):
    check: str
    detail: str
    claim_text: str | None = None


class Verification(BaseModel):
    """Result of the deterministic + entailment checks over a drafted report."""

    passed: bool
    failures: list[VerificationFailure] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    attempt: int = 1

    @property
    def summary(self) -> str:
        if self.passed:
            return f"{len(self.checks_run)} checks passed"
        return "; ".join(f"{f.check}: {f.detail}" for f in self.failures)


class ApprovalDecision(BaseModel):
    action_id: str
    decision: str  # approve | reject | edit
    actor: str
    note: str | None = None
    edited_arguments: dict[str, Any] | None = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def approved(self) -> bool:
        return self.decision in ("approve", "edit")


class Status(StrEnum):
    TRIAGE = "triage"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    REMEDIATING = "remediating"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    CLOSED_NOISE = "closed_noise"
    CLOSED_DUPLICATE = "closed_duplicate"

    @property
    def is_terminal(self) -> bool:
        return self in (
            Status.RESOLVED,
            Status.ESCALATED,
            Status.CLOSED_NOISE,
            Status.CLOSED_DUPLICATE,
        )


class TriageVerdict(BaseModel):
    classification: str  # noise | duplicate | actionable
    severity: str
    affected_services: list[ServiceName] = Field(default_factory=list)
    summary: str
    duplicate_of: str | None = None
