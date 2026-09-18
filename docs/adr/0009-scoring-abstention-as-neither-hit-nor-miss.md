# 9. Scoring abstention as neither a hit nor a miss

Date: 2026-09-18 · Status: accepted

## Context

The blueprint calls evaluation with ground truth the flagship differentiator, and
the obvious metric is accuracy: how often did the agent name the right root cause?

That metric is actively harmful here. A system that names a cause on every
incident scores better on accuracy than one that declines when the evidence is
thin — and is far worse to put on call, because a confident wrong root cause
sends a responder to the wrong service in the middle of an outage. Optimising
accuracy alone would push every design decision in exactly the wrong direction,
starting with deleting the abstention path.

## Decision

Report accuracy and the wrong rate side by side, and score abstention as neither.

- `correct` — right service and fault class, or correctly declined a no-fault incident.
- `partial` — right service, wrong fault class. Useful to a responder; not a pass.
- `wrong` — a confident answer that was wrong. The outcome that costs something.
- `abstained` — declined. Counted in neither rate.
- `error` — the run did not finish. A bug in Faultline or the harness, not a verdict.

The CI gate therefore has two floors: a minimum accuracy, and a maximum wrong
rate that defaults to zero.

## Consequences

Abstention is now measurably better than guessing, which is what gives the graph
a reason to prefer it. Every mechanism built around that — the two-independent-
sources conclude rule, the seven verification checks, `strip_failed_claims` —
finally has a number attached, rather than being a design one has to take on
faith.

Two things had to be true for the metric to be honest.

**At least one capsule must have no fault to find.** Without it, a system that
always guesses cannot be distinguished from one that reasons, because every
capsule rewards naming something. `flapping_noise` — a resolved warning, every
metric in baseline, no changes — is scored `wrong` if the agent names a cause. It
also turned out to exercise a path nothing else did: all its alerts have
resolved, so correlation produces no group at all, and the harness has to treat
"no incident was opened" as the pass.

**The baseline must not ace it.** The stub scores 50%, and the two it misses are
instructive rather than arbitrary. It gets the right service but the wrong fault
class on resource exhaustion, because its fault-class heuristic only knows
"changed recently" and cannot tell a lowered memory limit from a bad image. It
abstains on the dependency failure, because its "new error template" rule
requires a zero baseline and that capsule's template went from 1 occurrence to
3,100 — a huge spike that is not, literally, new. Both are real limitations of a
deliberately simple baseline, and both are visible in the scorecard instead of
being hidden by a benchmark the baseline passes.

**A crash is not a verdict.** The first version of the scorer graded a run that
finished without naming a cause as `error`, which conflated "the agent declined"
with "the harness broke" and made two capsules look like infrastructure failures.
`completed` now separates them. Only a run that raised is an error.

**Synthetic usage is never printed as a measurement.** The stub assigns itself
plausible per-call token and dollar figures so the budget arithmetic is exercised
end to end. Rendering those in a cost column beside a real provider's would be a
fabricated comparison, so the stub's usage columns show `--`. It is a correctness
baseline, not a cost baseline.

The cost accepted: five outcomes is more than a single score to explain, and
`partial` in particular invites argument about where the line sits. That is
preferable to a single number that rewards the behaviour the system was built to
avoid.
