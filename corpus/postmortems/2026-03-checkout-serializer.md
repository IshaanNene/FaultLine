---
service: checkout-service
doc_type: postmortem
incident_date: 2026-03-04
severity: P1
last_verified: 2026-03-11
---

# Postmortem: checkout outage from an order serializer change (2026-03-04)

## Impact

47 minutes of failed checkouts. Roughly 12,000 orders could not be placed.

## Root cause

Release 2.9.0 of checkout-service added a `tax_breakdown` field to the order payload.
The serializer rejected unknown fields, so every order failed with
`failed to serialize order <id>: unknown field <name>`. The field was added behind
what the author believed was a disabled flag; the flag defaulted to on in production.

## Timeline

- 09:12 checkout-service 2.9.0 rolled out.
- 09:14 error rate crossed 5%. frontend alerted first.
- 09:21 on-call began investigating frontend, since that is where the page came from.
- 09:38 attention moved to checkout-service after someone checked deploy history.
- 09:59 rollback to 2.8.3 completed, error rate recovered.

## What went wrong in the response

26 of the 47 minutes were spent investigating frontend, which was never broken. The
deploy history for checkout-service would have pointed at the cause in the first two
minutes and nobody looked at it until 09:38.

## Action items

- Check recent changes for every anomalous service before investigating any of them.
- Make the serializer tolerant of unknown fields and log them instead.
