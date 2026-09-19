---
service: payment-service
owner: payments-platform
doc_type: runbook
last_verified: 2026-07-14
---

# payment-service: upstream authorisation timeouts

## Symptoms

payment-service `http_p99_latency_ms` climbs into the seconds and error rate rises,
with no deploy, no config change and no flag flip anywhere in the namespace. Logs show
`upstream authorisation timeout` in volume.

checkout-service and frontend will both alert. Neither is the cause. They are failing
because payment-service is failing.

## Why this is hard to spot

There is nothing to correlate against. The usual first question -- what changed? -- has
no answer, because nothing on our side changed. The signal is topological rather than
temporal: payment-service is anomalous and its own dependencies are healthy, while the
services above it are anomalous only in the ways payment-service explains.

## Diagnosis

1. Confirm payment-service is the deepest anomalous service in the call graph.
2. Check the provider's status page and the `PSP_ENDPOINT` latency dashboard.
3. Confirm `cart-service` and other checkout dependencies are within baseline, so the
   fault is isolated to the payment path.

## Resolution

There is no rollback for this. Options, in order of preference:

1. Wait, if the provider reports a known incident with an ETA.
2. Fail over to the secondary provider by setting `PSP_ENDPOINT` to the failover
   endpoint. This requires payments-platform approval; it changes how money moves.
3. Enable the queue-and-retry path so orders are accepted and authorised later.

Do not roll back payment-service. The running version is not the problem, and a
rollback adds a second variable during an active incident.
