"""Model response -> domain object.

The one place where model output becomes system state. Keeping it in a single
function means every field that crosses that line is visible in one screen, and
that both tiers -- stub and live -- hand the graph identical types.

Adaptation is deliberately conservative: unknown hypothesis ids are dropped
rather than invented, and timestamps that will not parse become None rather than
guesses. A model that returns something odd should narrow the report, never
widen it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from faultline.core.schemas import (
    Check,
    Claim,
    FaultClass,
    Hypothesis,
    HypothesisStatus,
    RCAReport,
    RejectedHypothesis,
    TriageVerdict,
)
from faultline.ids import new_id
from faultline.worker import responses as r
from faultline.worker.models import Task


def to_domain(task: Task, parsed: Any, context: dict[str, Any]) -> Any:
    match task:
        case Task.TRIAGE:
            return _triage(parsed)
        case Task.HYPOTHESIZE:
            return _hypotheses(parsed)
        case Task.PLAN:
            return _checks(parsed)
        case Task.ASSESS:
            return _assessments(parsed, context)
        case Task.SYNTHESIZE:
            return _report(parsed)
        case Task.ENTAIL:
            return bool(parsed.supported)
        case Task.SUMMARIZE:
            return str(parsed.summary)
    raise ValueError(f"no adapter for task {task}")


def _triage(parsed: r.TriageResponse) -> TriageVerdict:
    return TriageVerdict(
        classification=parsed.classification.value,
        severity=parsed.severity,
        affected_services=parsed.affected_services,
        summary=parsed.summary,
    )


def _hypotheses(parsed: r.HypothesesResponse) -> list[Hypothesis]:
    # Ids are minted here, not by the model: an id the model invented could
    # collide with one already in state, and later nodes key on them.
    return [
        Hypothesis(
            id=new_id("hyp"),
            statement=h.statement,
            suspect_service=h.suspect_service,
            fault_class=h.fault_class,
            refuting_test=h.refuting_test,
        )
        for h in parsed.hypotheses
    ]


def _checks(parsed: r.PlanResponse) -> list[Check]:
    return [
        Check(
            id=new_id("chk"),
            tool=c.tool.value,
            arguments=c.arguments.as_dict(),
            targets_hypotheses=c.targets_hypotheses,
            rationale=c.rationale,
        )
        for c in parsed.checks
    ]


def _assessments(parsed: r.AssessResponse, context: dict[str, Any]) -> list[Hypothesis]:
    """Apply judgments to the hypotheses already in state.

    An assessment naming an id we do not hold is ignored rather than creating a
    hypothesis nobody proposed.
    """
    existing: dict[str, Hypothesis] = {h.id: h for h in context.get("hypotheses", [])}
    updated: list[Hypothesis] = []
    for assessment in parsed.assessments:
        hypothesis = existing.get(assessment.hypothesis_id)
        if hypothesis is None:
            continue
        updated.append(
            hypothesis.model_copy(
                update={
                    "status": assessment.status,
                    "confidence": assessment.confidence,
                    "supporting_evidence": assessment.supporting_evidence,
                    "refuting_evidence": assessment.refuting_evidence,
                }
            )
        )
    # Anything the model did not rule on stays as it was, rather than silently
    # vanishing from the differential.
    judged = {h.id for h in updated}
    updated.extend(h for h in existing.values() if h.id not in judged)
    return updated


def _report(parsed: r.SynthesisResponse) -> RCAReport:
    return RCAReport(
        root_cause_service=parsed.root_cause_service,
        fault_class=parsed.fault_class if not parsed.abstained else FaultClass.UNKNOWN,
        mechanism=parsed.mechanism,
        causal_chain=[Claim(text=c.text, evidence_ids=c.evidence_ids) for c in parsed.causal_chain],
        blast_radius=parsed.blast_radius,
        first_bad_at=_timestamp(parsed.first_bad_at),
        confidence=parsed.confidence,
        rejected_hypotheses=[
            RejectedHypothesis(statement=h.statement, reason=h.reason, evidence_ids=h.evidence_ids)
            for h in parsed.rejected_hypotheses
        ],
        # Proposals never come from the model; the propose node fills these from
        # the action catalog.
        proposed_actions=[],
        evidence_gaps=parsed.evidence_gaps,
        abstained=parsed.abstained,
    )


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # A timestamp we cannot read is a gap, not a reason to fail the report.
        # The verify node's temporal check simply has nothing to test.
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def status_of(value: str) -> HypothesisStatus:
    try:
        return HypothesisStatus(value)
    except ValueError:
        return HypothesisStatus.UNKNOWN
