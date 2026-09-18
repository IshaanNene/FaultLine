"""Budgets and the evidence ledger -- the two things that bound an investigation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from faultline.core.budget import Budget, BudgetExceeded
from faultline.core.ledger import EvidenceLedger
from faultline.core.schemas import Evidence, EvidenceKind


def _evidence(eid: str, kind: EvidenceKind, flagged: bool = False) -> Evidence:
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
        injection_flagged=flagged,
    )


def test_each_dimension_can_exhaust_independently(budget: Budget) -> None:
    assert not budget.exhausted
    budget.charge_model(tokens=500_000, usd=0.0)
    assert budget.exhausted_dimension == "tokens"


def test_dollar_ceiling_is_separate_from_tokens() -> None:
    budget = Budget(max_tokens=10**9, max_usd=0.10, max_tool_calls=100)
    budget.charge_model(tokens=1000, usd=0.11)
    assert budget.exhausted_dimension == "usd"


def test_deadline_exhausts_even_when_nothing_was_spent() -> None:
    budget = Budget(deadline=datetime.now(UTC) - timedelta(seconds=1))
    assert budget.exhausted_dimension == "deadline"


def test_require_raises_with_the_dimension_named(budget: Budget) -> None:
    budget.charge_tool(calls=budget.max_tool_calls)
    with pytest.raises(BudgetExceeded) as exc:
        budget.require()
    assert exc.value.dimension == "tool_calls"


def test_remaining_tool_calls_never_goes_negative(budget: Budget) -> None:
    budget.charge_tool(calls=budget.max_tool_calls + 5)
    assert budget.remaining_tool_calls() == 0


def test_ledger_reports_missing_citations() -> None:
    ledger = EvidenceLedger([_evidence("ev_1", EvidenceKind.METRIC)])
    assert ledger.missing(["ev_1", "ev_nope"]) == ["ev_nope"]


def test_independent_support_requires_two_kinds() -> None:
    """A metric change point plus a deploy correlation counts.
    Three log queries do not -- they can all be downstream of one wrong idea."""
    ledger = EvidenceLedger(
        [
            _evidence("ev_1", EvidenceKind.LOG),
            _evidence("ev_2", EvidenceKind.LOG),
            _evidence("ev_3", EvidenceKind.LOG),
        ]
    )
    assert not ledger.independent_support(["ev_1", "ev_2", "ev_3"])

    mixed = EvidenceLedger(
        [_evidence("ev_1", EvidenceKind.METRIC), _evidence("ev_2", EvidenceKind.CHANGE)]
    )
    assert mixed.independent_support(["ev_1", "ev_2"])


def test_flagged_evidence_is_surfaced() -> None:
    ledger = EvidenceLedger(
        [
            _evidence("ev_1", EvidenceKind.LOG, flagged=True),
            _evidence("ev_2", EvidenceKind.LOG),
        ]
    )
    assert [e.id for e in ledger.flagged()] == ["ev_1"]
