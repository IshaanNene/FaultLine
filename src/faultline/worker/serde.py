"""Checkpoint serialization allowlist.

LangGraph's default serializer will deserialize any type it finds in a
checkpoint, and its own docstring notes that an attacker who can write to the
checkpoint database may be able to trigger code execution that way. Faultline
stores checkpoints in the same Postgres as everything else, so the blast radius
of a SQL injection elsewhere would include the investigation state.

Declaring the allowlist explicitly closes that: anything not on this list fails
to deserialize rather than being constructed. It also means adding a type to
graph state is a deliberate act -- you have to come here and say so.
"""

from __future__ import annotations

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from faultline.core import alerts, budget, schemas, state

# Every type that can appear inside InvestigationState.
ALLOWED_TYPES: tuple[type, ...] = (
    alerts.Alert,
    alerts.AlertGroup,
    alerts.TimeWindow,
    alerts.Severity,
    budget.Budget,
    schemas.ActionKind,
    schemas.ActionProposal,
    schemas.ApprovalDecision,
    schemas.ApprovalMode,
    schemas.Check,
    schemas.Claim,
    schemas.Evidence,
    schemas.EvidenceGap,
    schemas.EvidenceKind,
    schemas.FaultClass,
    schemas.Hypothesis,
    schemas.HypothesisStatus,
    schemas.RCAReport,
    schemas.RejectedHypothesis,
    schemas.Status,
    schemas.TriageVerdict,
    schemas.Verification,
    schemas.VerificationFailure,
    state.SchemaVersionMismatch,
)


def make_serializer() -> JsonPlusSerializer:
    """A serializer that accepts Faultline's own state types and nothing else new.

    Passed to the constructor rather than added via `with_msgpack_allowlist`:
    that helper is a no-op when the base allowlist is the permissive default, so
    it would silently leave deserialization wide open.

    LangGraph's own SAFE_MSGPACK_TYPES (datetimes, UUIDs, sets, and the Pregel
    internals) remain allowed regardless of what is listed here.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_TYPES)
