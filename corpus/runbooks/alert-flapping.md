---
service: "*"
owner: platform-infra
doc_type: runbook
last_verified: 2026-06-20
---

# Warning alerts that resolve on their own

## Symptoms

A warning-severity alert fires and resolves within a few minutes. Metrics are inside
their baseline range. Nothing was deployed or configured.

Common offenders: `DiskUsageWarning` during a compaction, brief latency spikes during
a scheduled batch job, and threshold alerts set slightly too tight.

## What to do

Close it. An alert that resolved on its own, with every metric inside baseline and no
change in the window, is not an incident.

Do not go looking for a root cause. There is often a plausible-looking anomaly
somewhere in a large namespace, and attaching it to a resolved warning produces a
confident wrong answer that wastes the next responder's time.

## When it is not noise

Escalate if the same warning has flapped more than five times in a day, or if it
resolves only because the alert's own evaluation window rolled over rather than because
the underlying metric recovered. Those are threshold bugs worth fixing.
