"""Report verification.

Six of the seven checks are deterministic. That is the point: hallucination
defense that depends on a model grading itself is not a defense, it is a second
opinion from the same source.

Order matters. Cheap structural checks run first and can reject a report before
the entailment judge is ever called.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

from faultline.core.ledger import EvidenceLedger
from faultline.core.schemas import (
    Claim,
    RCAReport,
    Verification,
    VerificationFailure,
)

# Numbers the model wrote into prose, checked back against the cited evidence.
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

# Identifier tokens -- ev_8f53057f10274c7d, hyp_1a2b, chk_9f0e. Models naturally
# name the evidence they are citing inside the prose, and the digit runs inside a
# hex id are not claimed figures. Without this, every inline citation produced a
# fistful of spurious numeric-fidelity failures and pushed a sound report into
# abstention.
_IDENTIFIER = re.compile(r"\b[a-z]+_[0-9a-f]{6,}\b", re.IGNORECASE)
NUMERIC_TOLERANCE = 0.02  # 2%, to survive rounding in a summary


async def verify_report(
    report: RCAReport,
    ledger: EvidenceLedger,
    known_services: Sequence[str],
    entails: Callable[[Claim], Awaitable[bool]],
    attempt: int = 1,
) -> Verification:
    failures: list[VerificationFailure] = []
    checks_run: list[str] = []

    # 1. Entity grounding -- a service that does not exist cannot be a root cause.
    checks_run.append("entity_grounding")
    catalog = set(known_services)
    if report.root_cause_service not in catalog and not report.abstained:
        failures.append(
            VerificationFailure(
                check="entity_grounding",
                detail=f"{report.root_cause_service!r} is not in the service catalog",
            )
        )
    for service in report.blast_radius:
        if service not in catalog:
            failures.append(
                VerificationFailure(
                    check="entity_grounding",
                    detail=f"blast radius names unknown service {service!r}",
                )
            )

    # 2. Citation integrity -- every claim points at evidence that exists.
    checks_run.append("citation_integrity")
    for claim in report.causal_chain:
        if not claim.evidence_ids:
            failures.append(
                VerificationFailure(
                    check="citation_integrity",
                    detail="claim carries no citation",
                    claim_text=claim.text,
                )
            )
            continue
        if missing := ledger.missing(claim.evidence_ids):
            failures.append(
                VerificationFailure(
                    check="citation_integrity",
                    detail=f"cites evidence not in the ledger: {', '.join(missing)}",
                    claim_text=claim.text,
                )
            )

    # 3. Numeric fidelity -- numbers in prose must appear in the cited evidence.
    checks_run.append("numeric_fidelity")
    for claim in report.causal_chain:
        cited_numbers = _numbers_in_evidence(claim, ledger)
        for value in _numbers(claim.text):
            if not _close_to_any(value, cited_numbers):
                failures.append(
                    VerificationFailure(
                        check="numeric_fidelity",
                        detail=f"{value:g} does not appear in the cited evidence",
                        claim_text=claim.text,
                    )
                )

    # 4. Temporal logic -- a cause precedes its effect.
    checks_run.append("temporal_logic")
    if report.first_bad_at is not None:
        for claim in report.causal_chain:
            for evidence_id in claim.evidence_ids:
                entry = ledger.get(evidence_id)
                if entry is None:
                    continue
                # Allow a small grace: a change event can land inside the same
                # scrape interval as the first bad sample.
                if entry.window_start > report.first_bad_at + timedelta(minutes=5):
                    failures.append(
                        VerificationFailure(
                            check="temporal_logic",
                            detail=(
                                f"evidence {evidence_id} starts after the first bad minute "
                                f"({entry.window_start:%H:%M} > {report.first_bad_at:%H:%M})"
                            ),
                            claim_text=claim.text,
                        )
                    )

    # 5. Quarantine -- a claim must not rest solely on flagged content.
    checks_run.append("injection_quarantine")
    flagged = {e.id for e in ledger.flagged()}
    for claim in report.causal_chain:
        if claim.evidence_ids and set(claim.evidence_ids) <= flagged:
            failures.append(
                VerificationFailure(
                    check="injection_quarantine",
                    detail="claim rests only on evidence flagged as possible prompt injection",
                    claim_text=claim.text,
                )
            )

    # 6. Action safety -- proposals must name a target inside the blast radius.
    checks_run.append("action_safety")
    for action in report.proposed_actions:
        target_service = action.target.split("/")[-1]
        if target_service not in catalog:
            failures.append(
                VerificationFailure(
                    check="action_safety",
                    detail=f"proposed action targets unknown service {target_service!r}",
                )
            )

    # 7. Entailment -- the expensive one, and only if the structure held.
    if not failures:
        checks_run.append("entailment")
        for claim in report.causal_chain:
            if not await entails(claim):
                failures.append(
                    VerificationFailure(
                        check="entailment",
                        detail="cited evidence does not support this claim",
                        claim_text=claim.text,
                    )
                )

    return Verification(
        passed=not failures, failures=failures, checks_run=checks_run, attempt=attempt
    )


def strip_failed_claims(report: RCAReport, verification: Verification) -> RCAReport:
    """Last resort after retries: drop the claims that failed and lower confidence.

    Reporting a weaker, honest conclusion beats reporting a confident wrong one.
    """
    bad = {f.claim_text for f in verification.failures if f.claim_text}
    survived = [c for c in report.causal_chain if c.text not in bad]
    penalty = 0.5 if survived else 0.25
    return report.model_copy(
        update={
            "causal_chain": survived,
            "confidence": report.confidence * penalty,
            "abstained": not survived,
            "evidence_gaps": [
                *report.evidence_gaps,
                *(f"unverified claim dropped: {f.detail}" for f in verification.failures),
            ],
        }
    )


def _numbers(text: str) -> list[float]:
    """Figures a claim asserts, ignoring digits that are part of an identifier."""
    return [float(m) for m in _NUMBER.findall(_IDENTIFIER.sub(" ", text))]


def _numbers_in_evidence(claim: Claim, ledger: EvidenceLedger) -> list[float]:
    values: list[float] = []
    for evidence_id in claim.evidence_ids:
        if entry := ledger.get(evidence_id):
            values.extend(_numbers(entry.summary))
            values.extend(_numbers(str(entry.facts)))
    return values


def _close_to_any(value: float, candidates: Sequence[float]) -> bool:
    for candidate in candidates:
        if value == candidate:
            return True
        scale = max(abs(value), abs(candidate), 1e-9)
        if abs(value - candidate) / scale <= NUMERIC_TOLERANCE:
            return True
    return False
