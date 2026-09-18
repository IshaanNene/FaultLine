"""Identifier and fingerprint helpers.

Faultline leans on deterministic identifiers in three places: alert fingerprints
(so a retried webhook cannot create a second incident), evidence content hashes
(so a claim can be tied to the exact bytes it was derived from), and idempotency
keys on write actions (so a redelivered job cannot roll back twice).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable


def new_id(prefix: str) -> str:
    """A sortable-enough opaque id. Prefixed so it is readable in logs and traces."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def content_hash(*parts: str | bytes) -> str:
    """Stable hash over content, used for evidence and cache keys."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8") if isinstance(part, str) else part)
        digest.update(b"\x1f")  # unit separator, so ("ab","c") != ("a","bc")
    return digest.hexdigest()


def fingerprint(labels: dict[str, str], keys: Iterable[str] | None = None) -> str:
    """Alertmanager-style fingerprint over a stable subset of labels.

    Alertmanager sends its own fingerprint, but we recompute rather than trust it:
    a replayed capsule or a hand-fired test alert may not carry one.
    """
    selected = (
        sorted(labels.items())
        if keys is None
        else sorted((k, labels[k]) for k in keys if k in labels)
    )
    return content_hash(*(f"{k}={v}" for k, v in selected))[:32]
