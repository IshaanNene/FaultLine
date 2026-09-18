"""The evaluation harness.

Two things carry real weight here: capsules must be time-invariant (several of
Faultline's own rules are temporal, so a capsule replayed later must exercise the
same code), and the scorer must not reward guessing over abstaining.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from faultline.core.schemas import FaultClass, RCAReport, Status
from faultline.eval.capsule import CAPSULE_FORMAT_VERSION, Capsule, builtin, builtins
from faultline.eval.runner import (
    RegressionFailure,
    RunConfig,
    assert_no_regression,
    run_capsule,
    run_suite,
)
from faultline.eval.scoring import Outcome, Result, Scorecard, render, score
from faultline.gateway.backends.scenario import GroundTruth
from faultline.gateway.registry import ToolRegistry

STUB = RunConfig(provider="stub")


def _truth(service: str = "checkout-service", abstain: bool = False) -> GroundTruth:
    return GroundTruth(
        root_cause_service=service,
        fault_class=FaultClass.BAD_DEPLOY,
        mechanism="m",
        first_bad_at=datetime.now(UTC),
        expect_abstention=abstain,
    )


def _report(**overrides: object) -> RCAReport:
    base: dict[str, object] = {
        "root_cause_service": "checkout-service",
        "fault_class": FaultClass.BAD_DEPLOY,
        "mechanism": "m",
        "confidence": 0.9,
    }
    base.update(overrides)
    return RCAReport(**base)  # type: ignore[arg-type]


# -- capsules -------------------------------------------------------------


def test_every_builtin_capsule_is_self_contained() -> None:
    """A capsule that does not carry its own alerts is not replayable."""
    for capsule in builtins():
        assert capsule.alerts(), capsule.name
        assert capsule.scenario.topology, capsule.name
        assert capsule.scenario.metrics, capsule.name


@pytest.mark.parametrize("capsule", builtins(), ids=lambda c: c.name)
def test_capsules_round_trip_through_json(capsule: Capsule) -> None:
    restored = Capsule.from_dict(capsule.to_dict())
    assert restored.truth.root_cause_service == capsule.truth.root_cause_service
    assert restored.truth.fault_class == capsule.truth.fault_class
    assert len(restored.scenario.metrics) == len(capsule.scenario.metrics)
    assert len(restored.scenario.changes) == len(capsule.scenario.changes)
    assert len(restored.scenario.alerts) == len(capsule.scenario.alerts)


def test_a_capsule_is_rebased_onto_the_replay_time() -> None:
    """Faultline's bystander filter and temporal check both compare against the
    incident start, so absolute timestamps would drift a capsule into a
    different code path as it ages."""
    capsule = builtin("bad_deploy_checkout")
    data = capsule.to_dict()

    later = datetime.now(UTC) + timedelta(days=90)
    replayed = Capsule.from_dict(data, now=later)

    assert replayed.scenario.incident_start == later
    # The deploy stays the same distance before the incident, not 90 days before.
    original_gap = capsule.scenario.incident_start - capsule.scenario.changes[0].at
    replayed_gap = replayed.scenario.incident_start - replayed.scenario.changes[0].at
    assert original_gap == replayed_gap


def test_a_rebased_capsule_still_ranks_the_same_suspect() -> None:
    """The proof that rebasing preserves behaviour, not just timestamps."""
    capsule = builtin("bad_deploy_checkout")
    replayed = Capsule.from_dict(capsule.to_dict(), now=datetime.now(UTC) + timedelta(days=365))
    assert (
        ToolRegistry(replayed.scenario).rank_suspects()[0]["service"]
        == ToolRegistry(capsule.scenario).rank_suspects()[0]["service"]
    )


def test_a_capsule_from_an_unknown_format_is_refused() -> None:
    """Replaying an old capsule against changed rules would silently compare
    incomparable numbers."""
    data = builtin("bad_deploy_checkout").to_dict()
    data["format_version"] = CAPSULE_FORMAT_VERSION + 1
    with pytest.raises(ValueError, match="format"):
        Capsule.from_dict(data)


def test_an_unknown_capsule_name_lists_what_exists() -> None:
    with pytest.raises(KeyError, match="bad_deploy_checkout"):
        builtin("no_such_capsule")


def test_capsules_cover_distinct_fault_families() -> None:
    """A benchmark of four bad deploys measures one thing four times."""
    families = {c.truth.fault_class for c in builtins()}
    assert len(families) >= 3


def test_at_least_one_capsule_has_no_fault_to_find() -> None:
    """Without this, the benchmark rewards a system that always guesses."""
    assert any(c.truth.expect_abstention for c in builtins())


# -- scoring --------------------------------------------------------------


def test_the_right_service_and_class_is_correct() -> None:
    assert score(_report(), _truth()) is Outcome.CORRECT


def test_the_right_service_with_the_wrong_class_is_partial() -> None:
    outcome = score(_report(fault_class=FaultClass.NETWORK), _truth())
    assert outcome is Outcome.PARTIAL


def test_the_wrong_service_is_wrong() -> None:
    assert score(_report(root_cause_service="frontend"), _truth()) is Outcome.WRONG


def test_abstaining_is_neither_a_hit_nor_a_miss() -> None:
    outcome = score(_report(abstained=True), _truth())
    assert outcome is Outcome.ABSTAINED
    assert not outcome.is_hit
    assert not outcome.is_harmful


def test_finishing_without_a_report_is_an_abstention_not_an_error() -> None:
    """Running out of iterations and reporting honestly is a verdict; only a
    crash is an error."""
    assert score(None, _truth(), completed=True) is Outcome.ABSTAINED


def test_a_crashed_run_is_an_error() -> None:
    assert score(_report(), _truth(), completed=False) is Outcome.ERROR


def test_declining_a_no_fault_incident_is_correct() -> None:
    assert score(_report(abstained=True), _truth(abstain=True)) is Outcome.CORRECT
    assert score(None, _truth(abstain=True)) is Outcome.CORRECT


def test_closing_a_no_fault_incident_as_noise_is_correct() -> None:
    assert score(None, _truth(abstain=True), Status.CLOSED_NOISE) is Outcome.CORRECT


def test_inventing_a_cause_for_a_no_fault_incident_is_wrong() -> None:
    """The failure mode a benchmark without noise capsules cannot see."""
    assert score(_report(), _truth(abstain=True)) is Outcome.WRONG


def test_closing_a_real_incident_as_noise_is_an_abstention() -> None:
    assert score(_report(), _truth(), Status.CLOSED_NOISE) is Outcome.ABSTAINED


# -- scorecards -----------------------------------------------------------


def _result(outcome: Outcome, **kw: object) -> Result:
    return Result(
        capsule=str(kw.get("capsule", "c")),
        provider="p",
        outcome=outcome,
        expected="checkout-service",
        actual="",
        confidence=0.0,
        tokens=int(kw.get("tokens", 1000)),
        usd=float(kw.get("usd", 0.0)),
        seconds=float(kw.get("seconds", 1.0)),
        tool_calls=0,
        iterations=1,
    )


def test_accuracy_and_wrong_rate_are_reported_separately() -> None:
    """A system that abstains on everything has zero accuracy and does no harm;
    one that always guesses scores higher and is worse. One number cannot say
    that."""
    cautious = Scorecard("cautious")
    reckless = Scorecard("reckless")
    for _ in range(4):
        cautious.add(_result(Outcome.ABSTAINED))
    reckless.add(_result(Outcome.CORRECT))
    for _ in range(3):
        reckless.add(_result(Outcome.WRONG))

    assert cautious.accuracy == 0.0
    assert cautious.wrong_rate == 0.0
    assert reckless.accuracy == 0.25
    assert reckless.wrong_rate == 0.75


def test_an_empty_scorecard_does_not_divide_by_zero() -> None:
    card = Scorecard("empty")
    assert card.accuracy == 0.0
    assert card.wrong_rate == 0.0
    assert card.median_tokens == 0.0


def test_the_rendered_table_names_both_rates() -> None:
    card = Scorecard("stub")
    card.add(_result(Outcome.CORRECT))
    table = render([card])
    assert "correct" in table and "wrong" in table and "abstain" in table


# -- the gate -------------------------------------------------------------


def test_the_gate_passes_a_card_that_meets_its_floor() -> None:
    card = Scorecard("stub")
    card.add(_result(Outcome.CORRECT))
    card.add(_result(Outcome.ABSTAINED))
    assert_no_regression(card, min_accuracy=0.5)


def test_the_gate_fails_on_falling_accuracy() -> None:
    card = Scorecard("stub")
    card.add(_result(Outcome.ABSTAINED))
    with pytest.raises(RegressionFailure, match="accuracy"):
        assert_no_regression(card, min_accuracy=0.5)


def test_the_gate_has_zero_tolerance_for_confident_wrong_answers() -> None:
    """Even at perfect accuracy elsewhere, one wrong answer fails the build."""
    card = Scorecard("stub")
    card.add(_result(Outcome.CORRECT))
    card.add(_result(Outcome.WRONG, capsule="regressed"))
    with pytest.raises(RegressionFailure, match="regressed"):
        assert_no_regression(card, min_accuracy=0.5)


# -- the real thing -------------------------------------------------------


async def test_the_stub_solves_the_bad_deploy_capsule() -> None:
    result = await run_capsule(builtin("bad_deploy_checkout"), STUB)
    assert result.outcome is Outcome.CORRECT
    assert result.actual == "checkout-service"
    assert result.tokens > 0


async def test_the_stub_declines_the_no_fault_capsule() -> None:
    """A resolved alert produces no incident at all, which has to be runnable."""
    result = await run_capsule(builtin("flapping_noise"), STUB)
    assert result.outcome is Outcome.CORRECT
    assert result.actual == ""


async def test_the_stub_never_names_the_wrong_service() -> None:
    """The baseline may abstain, but it must not send anyone to the wrong place."""
    cards = await run_suite(["stub"])
    assert cards[0].wrong_rate == 0.0
    assert cards[0].n == len(builtins())


async def test_the_suite_leaves_headroom_above_the_baseline() -> None:
    """A benchmark the baseline aces measures nothing."""
    cards = await run_suite(["stub"])
    assert cards[0].accuracy < 1.0


def test_synthetic_usage_is_not_reported_as_a_measurement() -> None:
    """The stub invents its own token and dollar figures to exercise the budget
    path. Printing those in a cost column beside a real provider's would be a
    fabricated comparison."""
    stub = Scorecard("stub")
    stub.add(_result(Outcome.CORRECT, tokens=39_700, usd=0.70))
    real = Scorecard("ollama")
    real.add(_result(Outcome.CORRECT, tokens=9_822, usd=0.0))

    assert not stub.usage_is_real
    assert real.usage_is_real

    table = render([stub, real])
    assert "0.70" not in table
    assert "9,822" in table
