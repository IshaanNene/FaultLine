---
doc_type: architecture
owner: platform-infra
last_verified: 2026-09-01
---

# shop namespace: service catalog

## Call graph

- **frontend** calls checkout-service, product-catalog, cart-service. Outermost
  service; alerts here usually mean something below it is broken.
- **checkout-service** calls payment-service and cart-service. Owns order creation.
- **payment-service** calls the external payment provider. No internal dependencies.
- **cart-service** calls redis.
- **product-catalog** calls postgres.

## Reading the graph during an incident

The cause is usually the deepest anomalous service whose own dependencies are healthy.
A service that is anomalous because something it calls is anomalous is a victim, and
rolling it back will not help.
