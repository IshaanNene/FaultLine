---
service: cart-service
doc_type: postmortem
incident_date: 2026-05-19
severity: P2
last_verified: 2026-05-26
---

# Postmortem: cart-service OOMKill loop after a limit change (2026-05-19)

## Impact

31 minutes of intermittent cart failures. No data loss; carts are recoverable from
Redis.

## Root cause

A cost-reduction change lowered cart-service's memory limit from 512Mi to 256Mi. The
service's steady-state working set was around 480Mi, so pods were OOMKilled within
minutes of the rollout and entered a restart loop. frontend surfaced the gaps as 503s.

No application code changed. The image tag was identical before and after, which is
why the first responder discounted a recent change.

## What went wrong in the response

The team looked for a bad deploy, found no new image, and concluded nothing had
changed. Config changes are changes. The limit edit was visible in the change feed the
whole time.

## Action items

- Treat config and limit edits as first-class change events, not just image rollouts.
- Alert when a container's memory limit is set below its observed p99 working set.
