"""Scoring an investigation against ground truth.

The headline number is not accuracy. A system that names a cause on every
incident scores better on accuracy than one that abstains when the evidence is
thin, and is far worse to put on call: a confident wrong root cause sends a
responder to the wrong service during an outage.

So every scorecard reports two rates side by side:

- **accuracy** -- how often it got the answer right.
- **wrong rate** -- how often it stated a confident answer that was wrong.

Abstaining is counted as neither. That is the whole point of making abstention a
first-class outcome in the graph: it has to be visibly better than guessing, or
nothing in the system has reason to prefer it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median

from faultline.core.schemas import RCAReport, Status
from faultline.gateway.backends.scenario import GroundTruth


class Outcome(StrEnum):
    CORRECT = "correct"
    """Right service, right fault class -- or correctly declined a no-fault incident."""

    PARTIAL = "partial"
    """Right service, wrong fault class. Useful to a responder; not a pass."""

    WRONG = "wrong"
    """A confident answer that was wrong. The outcome that actually costs something."""

    ABSTAINED = "abstained"
    """No confident answer. Honest, and not counted as either a hit or a miss."""

    ERROR = "error"
    """The investigation did not complete. A harness or system failure, not a verdict."""

    @property
    def is_hit(self) -> bool:
        return self is Outcome.CORRECT

    @property
    def is_harmful(self) -> bool:
        return self is Outcome.WRONG


def score(
    report: RCAReport | None,
    truth: GroundTruth,
    status: Status | None = None,
    completed: bool = True,
) -> Outcome:
    """Grade one investigation.

    Deliberately strict about service identity and lenient about prose: whether
    the mechanism reads well is a judge's question, and a judge that grades its
    own family of models is not evidence. Service and fault class are checkable
    without one.

    `completed` separates a crash from a verdict. An investigation that finished
    without naming a cause -- because it ran out of iterations, or closed the
    alert as noise -- has declined, which is a legitimate outcome. Only a run
    that failed to finish is an ERROR, and that is a bug in Faultline or the
    harness rather than a judgement about the incident.
    """
    if not completed:
        return Outcome.ERROR

    closed_without_cause = status in (Status.CLOSED_NOISE, Status.CLOSED_DUPLICATE)
    declined = report is None or report.abstained or closed_without_cause

    if truth.expect_abstention:
        # Nothing was wrong. Declining is the pass; naming a cause is the failure.
        return Outcome.CORRECT if declined else Outcome.WRONG
    if report is None or declined:
        return Outcome.ABSTAINED

    if report.root_cause_service != truth.root_cause_service:
        return Outcome.WRONG
    if report.fault_class != truth.fault_class:
        return Outcome.PARTIAL
    return Outcome.CORRECT


@dataclass(slots=True)
class Result:
    """One capsule run under one provider."""

    capsule: str
    provider: str
    outcome: Outcome
    expected: str
    actual: str
    confidence: float
    tokens: int
    usd: float
    seconds: float
    tool_calls: int
    iterations: int
    detail: str = ""


# Providers whose token and dollar figures are invented. The stub assigns itself
# plausible per-call usage so the budget arithmetic is exercised end to end; those
# numbers are not measurements and must never appear in a cost column as if they
# were. The stub is a correctness baseline, not a cost comparison.
SYNTHETIC_USAGE = frozenset({"stub"})


@dataclass(slots=True)
class Scorecard:
    provider: str
    results: list[Result] = field(default_factory=list)

    @property
    def usage_is_real(self) -> bool:
        return self.provider not in SYNTHETIC_USAGE

    def add(self, result: Result) -> None:
        self.results.append(result)

    @property
    def n(self) -> int:
        return len(self.results)

    def count(self, outcome: Outcome) -> int:
        return sum(1 for r in self.results if r.outcome is outcome)

    @property
    def accuracy(self) -> float:
        return self.count(Outcome.CORRECT) / self.n if self.n else 0.0

    @property
    def wrong_rate(self) -> float:
        """Confident and wrong. The number a team on call would actually care about."""
        return self.count(Outcome.WRONG) / self.n if self.n else 0.0

    @property
    def abstention_rate(self) -> float:
        return self.count(Outcome.ABSTAINED) / self.n if self.n else 0.0

    @property
    def median_tokens(self) -> float:
        return median(r.tokens for r in self.results) if self.n else 0.0

    @property
    def median_seconds(self) -> float:
        return median(r.seconds for r in self.results) if self.n else 0.0

    @property
    def total_usd(self) -> float:
        return round(sum(r.usd for r in self.results), 4)

    def row(self) -> str:
        tokens = f"{self.median_tokens:>8,.0f}" if self.usage_is_real else f"{'--':>8}"
        cost = f"${self.total_usd:.4f}" if self.usage_is_real else f"{'--':>8}"
        return (
            f"{self.provider:12} n={self.n:<3} "
            f"correct={self.accuracy:5.0%} wrong={self.wrong_rate:5.0%} "
            f"abstain={self.abstention_rate:5.0%} "
            f"tokens~{tokens} "
            f"{self.median_seconds:>6.1f}s  {cost}"
        )


def render(cards: list[Scorecard]) -> str:
    """A table you can paste into a README and defend in an interview."""
    lines = [
        "provider     n     correct wrong abstain   tokens~     med     cost",
        "-" * 74,
        *(c.row() for c in cards),
        "",
        "correct = right service and fault class (or correctly declined a no-fault incident)",
        "wrong   = a confident answer that was wrong -- the number that costs something",
        "abstain = declined to answer; counted as neither a hit nor a miss",
        "--      = usage is synthetic for this provider, so it is not reported as a measurement",
    ]
    return "\n".join(lines)


def render_detail(cards: list[Scorecard]) -> str:
    lines = []
    for card in cards:
        lines.append(f"\n{card.provider}:")
        for r in card.results:
            mark = {
                Outcome.CORRECT: "ok  ",
                Outcome.PARTIAL: "part",
                Outcome.WRONG: "WRONG",
                Outcome.ABSTAINED: "abst",
                Outcome.ERROR: "ERR ",
            }[r.outcome]
            lines.append(
                f"  {mark} {r.capsule:28} expected={r.expected or '(no fault)':22}"
                f" got={r.actual or '(declined)':22} conf={r.confidence:.2f}"
                f" {r.seconds:5.1f}s"
            )
            if r.detail:
                lines.append(f"       {r.detail}")
    return "\n".join(lines)
