# 6. Human approval is the feature

Date: 2026-09-18 · Status: accepted

## Context

Faultline can identify a bad deploy and knows the rollback command. Executing it
autonomously would cut minutes off an outage. No serious team allows that, and
the reason is not timidity — it is that the failure mode is unbounded. An agent
confidently rolling back the wrong service during an incident makes the incident
worse.

## Decision

No autonomous writes. Ever. The approval gate is a required node in the graph,
not a configurable one.

The mechanism, not just the policy:

- The investigation runs on read-only credentials, which it never holds directly
  — the tool gateway does.
- Write tools cannot be reached through the read path at all. Calling `rollback`
  with a capability token raises a policy error.
- Actions come from a closed catalog. The model selects from it; it never writes
  a command.
- Approval mints a separate token bound to one incident, one action, one target,
  one approver and an expiry — re-validated at execution against the action in
  hand.
- Execution is idempotent under a key derived from incident, action and approver,
  so a redelivered job cannot roll back twice.
- Every decision lands in a hash-chained, append-only audit log.

## Consequences

Faultline shortens the diagnosis, which is where the time actually goes, and
leaves the decision where the accountability is.

There is a design benefit too. Because a successful prompt injection cannot
produce a write, the impact of one is bounded to a misleading report — which the
verification layer is separately built to catch. Capability limits are what make
the injection defense credible; without them it would rest entirely on pattern
matching, which is not a security control.

The cost is that mean time to resolution includes human response time, and that
the approval path is extra machinery: an interrupt, a resume job, a decision
persisted before the resume is enqueued. `tests/test_end_to_end.py` covers the
approve path, the reject path, and the case where the fix does not work and the
investigation reopens.
