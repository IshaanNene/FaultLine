---
service: "*"
owner: platform-infra
doc_type: runbook
last_verified: 2026-09-02
---

# Pods restarting: OOMKilled

## Symptoms

`PodRestartingFrequently` fires. Pods show a rising restart count and the last
terminated reason is `OOMKilled`. Error rates climb on the affected service and on
anything calling it, because requests in flight are dropped when the pod dies.

A crashlooping deployment is one incident, not one per pod.

## Diagnosis

1. `kubectl -n <namespace> describe pod <pod>` and read `Last State: Terminated`.
   `Reason: OOMKilled` is conclusive.
2. Compare `memory_working_set_mb` against the container's memory limit. Working set
   at or above the limit is the cause; well below it means look elsewhere.
3. Check whether the limit changed recently. A lowered limit is a far more common
   cause than a genuine memory leak, and it is much faster to confirm.

## Resolution

If the limit was lowered below the working set, restore the previous limit. This is a
config change, not a rollback of application code, and the image tag is irrelevant.

If the limit is unchanged and the working set has grown, you have a leak. Scale out to
buy time and page the owning team. Do not simply raise the limit and close the
incident; that hides the leak until it returns.

## What this is not

Frequent restarts with `Reason: Error` rather than `OOMKilled` are a crash, not memory
exhaustion. Restarts with readiness probe failures and no terminated reason are usually
a slow dependency, not the service itself.
