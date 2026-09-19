---
service: checkout-service
owner: payments-platform
doc_type: runbook
last_verified: 2026-08-30
---

# checkout-service: high error rate

## Symptoms

`HighErrorRate` fires on checkout-service, and usually on frontend a few seconds
later because frontend surfaces checkout failures as 502s. Look for `http_error_rate`
above 5% sustained for 5 minutes.

Frontend alerting alone is not evidence that frontend is broken. Check checkout-service
before touching frontend.

## Most common cause: a bad release

Roughly two thirds of checkout-service error spikes follow a rollout. The tell is a new
error template appearing in the logs within a minute or two of the deploy, most often
a serialization failure such as `failed to serialize order`.

### Triage

1. Check what changed: `kubectl -n shop rollout history deploy/checkout-service`.
2. Compare the current image tag against the last known-good tag.
3. Look for error templates that did not exist before the rollout.

### Rollback procedure

Do not run these steps partially. If you stop halfway the deployment is left at an
unknown revision.

1. Announce in `#incidents` that you are rolling back checkout-service.
2. `kubectl -n shop rollout undo deploy/checkout-service`
3. Watch `kubectl -n shop rollout status deploy/checkout-service` until all replicas
   report ready.
4. Confirm `http_error_rate` returns to baseline within 3 minutes.
5. If the error rate does not recover, the deploy was not the cause. Reopen the
   investigation rather than rolling back further.

## Less common: a failing dependency

If checkout-service error rate is elevated but nothing was deployed, check its
dependencies before blaming it. checkout-service calls payment-service and
cart-service. A service whose own dependencies are healthy is a candidate cause; one
whose dependency is failing is usually a victim.

## Escalation

Page the payments-platform on-call if the rollback does not restore the error rate, or
if order data may have been written in an inconsistent state.
