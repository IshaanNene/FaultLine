"""Model response -> domain object.

This is where model output becomes system state, so the tests are mostly about
what the adapter refuses to do: invent hypotheses, invent timestamps, propose
actions, or drop a hypothesis the model forgot to mention.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultline.core.schemas import (
    Check,
    FaultClass,
    Hypothesis,
    HypothesisStatus,
    RCAReport,
    TriageVerdict,
)
from faultline.worker import responses as r
from faultline.worker.adapt import to_domain
from faultline.worker.models import Task


def _hypothesis(hid: str, service: str = "checkout-service") -> Hypothesis:
    return Hypothesis(
        id=hid,
        statement=f"{service} broke",
        suspect_service=service,
        fault_class=FaultClass.BAD_DEPLOY,
        refuting_test="no change",
    )


def test_triage_becomes_a_verdict() -> None:
    parsed = r.TriageResponse(
        classification=r.TriageClassification.ACTIONABLE,
        severity="P1",
        affected_services=["frontend"],
        summary="frontend 5xx",
    )
    verdict = to_domain(Task.TRIAGE, parsed, {})
    assert isinstance(verdict, TriageVerdict)
    assert verdict.classification == "actionable"


def test_hypothesis_ids_are_minted_locally() -> None:
    """A model-supplied id could collide with one already in state."""
    parsed = r.HypothesesResponse(
        hypotheses=[
            r.HypothesisDraft(
                statement="a",
                suspect_service="checkout-service",
                fault_class=FaultClass.BAD_DEPLOY,
                refuting_test="t",
            ),
            r.HypothesisDraft(
                statement="b",
                suspect_service="frontend",
                fault_class=FaultClass.DEPENDENCY_FAILURE,
                refuting_test="t",
            ),
        ]
    )
    hypotheses = to_domain(Task.HYPOTHESIZE, parsed, {})
    assert len({h.id for h in hypotheses}) == 2
    assert all(h.id.startswith("hyp_") for h in hypotheses)
    assert all(h.status is HypothesisStatus.OPEN for h in hypotheses)


def test_planned_checks_drop_unset_arguments() -> None:
    parsed = r.PlanResponse(
        checks=[
            r.CheckRequest(
                tool=r.ReadTool.SEARCH_LOGS,
                arguments=r.CheckArguments(service="checkout-service", level="ERROR"),
                targets_hypotheses=["hyp_1"],
                rationale="why",
            )
        ]
    )
    checks = to_domain(Task.PLAN, parsed, {})
    assert isinstance(checks[0], Check)
    assert checks[0].arguments == {"service": "checkout-service", "level": "ERROR"}
    assert "depth" not in checks[0].arguments


def test_assessments_update_the_hypotheses_in_state() -> None:
    existing = [_hypothesis("hyp_1"), _hypothesis("hyp_2", "frontend")]
    parsed = r.AssessResponse(
        assessments=[
            r.Assessment(
                hypothesis_id="hyp_1",
                status=HypothesisStatus.SUPPORTED,
                confidence=0.9,
                supporting_evidence=["ev_1", "ev_2"],
                refuting_evidence=[],
                reasoning="deploy plus new error template",
            )
        ],
        decision=r.AssessDecision.CONCLUDE,
    )
    updated = to_domain(Task.ASSESS, parsed, {"hypotheses": existing})
    by_id = {h.id: h for h in updated}

    assert by_id["hyp_1"].status is HypothesisStatus.SUPPORTED
    assert by_id["hyp_1"].supporting_evidence == ["ev_1", "ev_2"]
    # The one the model did not rule on survives rather than vanishing.
    assert by_id["hyp_2"].status is HypothesisStatus.OPEN


def test_an_assessment_for_an_unknown_hypothesis_is_ignored() -> None:
    """Otherwise the model could conjure a hypothesis nobody proposed."""
    parsed = r.AssessResponse(
        assessments=[
            r.Assessment(
                hypothesis_id="hyp_invented",
                status=HypothesisStatus.SUPPORTED,
                confidence=1.0,
                supporting_evidence=[],
                refuting_evidence=[],
                reasoning="",
            )
        ],
        decision=r.AssessDecision.CONCLUDE,
    )
    updated = to_domain(Task.ASSESS, parsed, {"hypotheses": [_hypothesis("hyp_1")]})
    assert [h.id for h in updated] == ["hyp_1"]


def _synthesis(**overrides: object) -> r.SynthesisResponse:
    base: dict[str, object] = {
        "root_cause_service": "checkout-service",
        "fault_class": FaultClass.BAD_DEPLOY,
        "mechanism": "bad release",
        "causal_chain": [r.ClaimDraft(text="it changed", evidence_ids=["ev_1"])],
        "blast_radius": ["checkout-service", "frontend"],
        "first_bad_at": "2026-09-18T08:09:00Z",
        "confidence": 0.85,
        "rejected_hypotheses": [],
        "evidence_gaps": [],
        "abstained": False,
    }
    base.update(overrides)
    return r.SynthesisResponse(**base)  # type: ignore[arg-type]


def test_synthesis_becomes_a_report() -> None:
    report = to_domain(Task.SYNTHESIZE, _synthesis(), {})
    assert isinstance(report, RCAReport)
    assert report.root_cause_service == "checkout-service"
    assert report.causal_chain[0].evidence_ids == ["ev_1"]


def test_the_model_can_never_propose_an_action() -> None:
    """Remediation comes from the catalog. The schema has no field for it, and
    the adapter pins it empty regardless."""
    report = to_domain(Task.SYNTHESIZE, _synthesis(), {})
    assert report.proposed_actions == []
    assert "proposed_actions" not in r.SynthesisResponse.model_fields


def test_a_zulu_timestamp_parses_to_utc() -> None:
    report = to_domain(Task.SYNTHESIZE, _synthesis(), {})
    assert report.first_bad_at == datetime(2026, 9, 18, 8, 9, tzinfo=UTC)


def test_an_unparsable_timestamp_becomes_none_not_a_guess() -> None:
    report = to_domain(Task.SYNTHESIZE, _synthesis(first_bad_at="about ten minutes ago"), {})
    assert report.first_bad_at is None


def test_a_naive_timestamp_is_assumed_utc() -> None:
    report = to_domain(Task.SYNTHESIZE, _synthesis(first_bad_at="2026-09-18T08:09:00"), {})
    assert report.first_bad_at is not None
    assert report.first_bad_at.tzinfo is not None


def test_an_abstained_report_does_not_claim_a_fault_class() -> None:
    report = to_domain(Task.SYNTHESIZE, _synthesis(abstained=True), {})
    assert report.abstained is True
    assert report.fault_class is FaultClass.UNKNOWN


def test_confidence_is_clamped_by_the_domain_model() -> None:
    report = RCAReport(
        root_cause_service="x", fault_class=FaultClass.UNKNOWN, mechanism="m", confidence=4.2
    )
    assert report.confidence == 1.0


def test_entailment_and_summary_unwrap_to_primitives() -> None:
    assert to_domain(Task.ENTAIL, r.EntailmentResponse(supported=True, reason="r"), {}) is True
    assert to_domain(Task.SUMMARIZE, r.SummaryResponse(summary="done"), {}) == "done"


def test_an_unhandled_task_is_an_error_not_a_silent_none() -> None:
    with pytest.raises(ValueError, match="no adapter"):
        to_domain("not_a_task", None, {})  # type: ignore[arg-type]
