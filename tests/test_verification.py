"""Verification is the hallucination defense. Six of seven checks are deterministic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from faultline.core.ledger import EvidenceLedger
from faultline.core.schemas import (
    ActionKind,
    ActionProposal,
    Claim,
    Evidence,
    EvidenceKind,
    FaultClass,
    RCAReport,
)
from faultline.worker.verification import strip_failed_claims, verify_report

SERVICES = ["frontend", "checkout-service", "payment-service"]
NOW = datetime.now(UTC)


def _evidence(eid: str, summary: str, *, flagged: bool = False, start_offset: int = 30) -> Evidence:
    return Evidence(
        id=eid,
        kind=EvidenceKind.METRIC,
        tool="get_service_health",
        query="q",
        window_start=NOW - timedelta(minutes=start_offset),
        window_end=NOW,
        summary=summary,
        facts={"service": "checkout-service"},
        content_hash="h",
        injection_flagged=flagged,
    )


def _report(**overrides: object) -> RCAReport:
    base: dict[str, object] = {
        "root_cause_service": "checkout-service",
        "fault_class": FaultClass.BAD_DEPLOY,
        "mechanism": "bad release",
        "causal_chain": [Claim(text="checkout-service error rate rose", evidence_ids=["ev_1"])],
        "blast_radius": ["checkout-service", "frontend"],
        "first_bad_at": NOW - timedelta(minutes=12),
        "confidence": 0.8,
    }
    base.update(overrides)
    return RCAReport(**base)  # type: ignore[arg-type]


async def _always(_claim: Claim) -> bool:
    return True


async def _never(_claim: Claim) -> bool:
    return False


async def test_a_well_formed_report_passes() -> None:
    ledger = EvidenceLedger([_evidence("ev_1", "checkout-service error rate rose")])
    result = await verify_report(_report(), ledger, SERVICES, _always)
    assert result.passed, result.summary


async def test_an_invented_service_is_caught() -> None:
    """The model cannot name a root cause that does not exist in the catalog."""
    ledger = EvidenceLedger([_evidence("ev_1", "error rate rose")])
    result = await verify_report(
        _report(root_cause_service="billing-service"), ledger, SERVICES, _always
    )
    assert not result.passed
    assert any(f.check == "entity_grounding" for f in result.failures)


async def test_an_uncited_claim_is_caught() -> None:
    ledger = EvidenceLedger([_evidence("ev_1", "error rate rose")])
    result = await verify_report(
        _report(causal_chain=[Claim(text="the database ran out of disk", evidence_ids=[])]),
        ledger,
        SERVICES,
        _always,
    )
    assert any(f.check == "citation_integrity" for f in result.failures)


async def test_a_citation_to_nonexistent_evidence_is_caught() -> None:
    ledger = EvidenceLedger([_evidence("ev_1", "error rate rose")])
    result = await verify_report(
        _report(causal_chain=[Claim(text="something", evidence_ids=["ev_fabricated"])]),
        ledger,
        SERVICES,
        _always,
    )
    assert any("not in the ledger" in f.detail for f in result.failures)


async def test_a_fabricated_number_is_caught() -> None:
    """The classic hallucination: a plausible figure that is in no evidence."""
    ledger = EvidenceLedger([_evidence("ev_1", "error rate rose from 0.1% to 18%")])
    result = await verify_report(
        _report(causal_chain=[Claim(text="error rate hit 94%", evidence_ids=["ev_1"])]),
        ledger,
        SERVICES,
        _always,
    )
    assert any(f.check == "numeric_fidelity" for f in result.failures)


async def test_rounding_is_tolerated() -> None:
    ledger = EvidenceLedger([_evidence("ev_1", "p99 latency 3100 ms")])
    result = await verify_report(
        _report(causal_chain=[Claim(text="p99 latency 3120 ms", evidence_ids=["ev_1"])]),
        ledger,
        SERVICES,
        _always,
    )
    assert not any(f.check == "numeric_fidelity" for f in result.failures), result.summary


async def test_a_cause_cannot_postdate_its_effect() -> None:
    late = _evidence("ev_1", "error rate rose", start_offset=0)
    result = await verify_report(
        _report(first_bad_at=NOW - timedelta(hours=2)),
        EvidenceLedger([late]),
        SERVICES,
        _always,
    )
    assert any(f.check == "temporal_logic" for f in result.failures)


async def test_a_claim_resting_only_on_flagged_content_is_quarantined() -> None:
    """A successful prompt injection must not become a cited finding."""
    ledger = EvidenceLedger([_evidence("ev_1", "checkout-service error rate rose", flagged=True)])
    result = await verify_report(_report(), ledger, SERVICES, _always)
    assert any(f.check == "injection_quarantine" for f in result.failures)


async def test_an_action_against_an_unknown_service_is_caught() -> None:
    ledger = EvidenceLedger([_evidence("ev_1", "checkout-service error rate rose")])
    result = await verify_report(
        _report(
            proposed_actions=[
                ActionProposal(
                    id="act_1",
                    kind=ActionKind.ROLLBACK,
                    target="shop/not-a-real-service",
                    rationale="r",
                )
            ]
        ),
        ledger,
        SERVICES,
        _always,
    )
    assert any(f.check == "action_safety" for f in result.failures)


async def test_entailment_runs_only_after_the_structure_holds() -> None:
    """The expensive check must not be spent on a report that already failed."""
    ledger = EvidenceLedger([_evidence("ev_1", "error rate rose")])
    result = await verify_report(
        _report(root_cause_service="ghost-service"), ledger, SERVICES, _never
    )
    assert "entailment" not in result.checks_run


async def test_unsupported_claims_fail_entailment() -> None:
    ledger = EvidenceLedger([_evidence("ev_1", "checkout-service error rate rose")])
    result = await verify_report(_report(), ledger, SERVICES, _never)
    assert any(f.check == "entailment" for f in result.failures)


async def test_stripping_failed_claims_lowers_confidence() -> None:
    ledger = EvidenceLedger([_evidence("ev_1", "checkout-service error rate rose")])
    report = _report()
    verification = await verify_report(report, ledger, SERVICES, _never)
    reduced = strip_failed_claims(report, verification)
    assert reduced.causal_chain == []
    assert reduced.abstained is True
    assert reduced.confidence < report.confidence


@pytest.mark.parametrize("abstained", [True, False])
async def test_abstention_skips_the_entity_check_for_the_root_cause(abstained: bool) -> None:
    """An abstaining report names 'unknown', which is not in any catalog."""
    ledger = EvidenceLedger([])
    result = await verify_report(
        _report(
            root_cause_service="unknown",
            abstained=abstained,
            causal_chain=[],
            blast_radius=[],
        ),
        ledger,
        SERVICES,
        _always,
    )
    assert result.passed is abstained


async def test_an_inline_evidence_id_is_not_read_as_a_claimed_figure() -> None:
    """Models name the evidence they cite inside the prose. The digit runs in a
    hex id are not figures, and treating them as such failed every such claim."""
    ledger = EvidenceLedger([_evidence("ev_8f53057f10274c7d", "checkout-service deployed 2.14.0")])
    result = await verify_report(
        _report(
            causal_chain=[
                Claim(
                    text="checkout-service was changed (ev_8f53057f10274c7d).",
                    evidence_ids=["ev_8f53057f10274c7d"],
                )
            ]
        ),
        ledger,
        SERVICES,
        _always,
    )
    assert result.passed, result.summary


async def test_a_fabricated_number_is_still_caught_alongside_an_inline_id() -> None:
    """Ignoring identifier digits must not blind the check to real figures."""
    ledger = EvidenceLedger([_evidence("ev_8f53057f10274c7d", "error rate rose to 18%")])
    result = await verify_report(
        _report(
            causal_chain=[
                Claim(
                    text="error rate hit 94% (ev_8f53057f10274c7d).",
                    evidence_ids=["ev_8f53057f10274c7d"],
                )
            ]
        ),
        ledger,
        SERVICES,
        _always,
    )
    assert any(f.check == "numeric_fidelity" and "94" in f.detail for f in result.failures)
