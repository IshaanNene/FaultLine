---
service: frontend
owner: storefront
doc_type: runbook
last_verified: 2026-08-11
---

# frontend: elevated 5xx

## Read this first

frontend is the most-alerted service in the namespace and is almost never the cause.
It is the outermost service, so every failure underneath it surfaces here. Treat a
frontend alert as a signal that something below frontend is broken until you have
evidence otherwise.

## Diagnosis

1. Look at what frontend calls: checkout-service, product-catalog, cart-service.
2. For each, compare error rate and latency against baseline.
3. The cause is usually the deepest anomalous service whose own dependencies are
   healthy.
4. Only investigate frontend itself if every downstream service is within baseline.

## Genuine frontend causes

Rare, but real: a bad frontend release, an expired TLS certificate, or a misconfigured
ingress. All three show errors that do not correlate with any downstream service's
error rate.
