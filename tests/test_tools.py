"""The tool layer: compression, policy enforcement and the deterministic analytics."""

from __future__ import annotations

import pytest

from faultline.core.schemas import ActionKind, EvidenceKind
from faultline.gateway.policy import PolicyError, TokenSigner
from faultline.gateway.registry import ToolError, ToolRegistry


@pytest.fixture
def capability(signer: TokenSigner):
    return signer.read_capability(signer.mint_capability("acme", "inc_1"))


def test_suspect_ranking_blames_the_cause_not_the_loudest_victim(registry: ToolRegistry) -> None:
    """frontend is what alerts; checkout-service is what broke.

    This is the SREGym anchoring failure, and the ranking has to get it right
    before any model is involved.
    """
    ranked = registry.rank_suspects()
    assert ranked[0]["service"] == "checkout-service"
    assert [r["service"] for r in ranked].index("frontend") > 0


def test_ranking_ignores_anomalies_that_predate_the_incident(registry: ToolRegistry) -> None:
    """product-catalog has been slow for six hours. It is a bystander, not a cause."""
    assert "product-catalog" not in {r["service"] for r in registry.rank_suspects()}


def test_health_tool_returns_a_summary_not_raw_series(registry, capability) -> None:
    evidence = registry.call("get_service_health", {"service": "checkout-service"}, capability)
    assert evidence.kind is EvidenceKind.METRIC
    assert evidence.facts["anomalous"] is True
    assert len(evidence.summary) < 500  # compressed at the source


def test_healthy_service_reads_as_healthy(registry, capability) -> None:
    evidence = registry.call("get_service_health", {"service": "payment-service"}, capability)
    assert evidence.facts["anomalous"] is False


def test_log_search_returns_templates_with_a_baseline_diff(registry, capability) -> None:
    evidence = registry.call(
        "search_logs", {"service": "checkout-service", "level": "ERROR"}, capability
    )
    assert evidence.facts["new_templates"]
    assert "failed to serialize order" in evidence.facts["new_templates"][0]


def test_injection_in_a_log_line_is_flagged(registry, capability) -> None:
    """Attacker-controllable content arriving through telemetry is marked, kept
    as evidence, and never trusted."""
    evidence = registry.call("search_logs", {"service": "frontend"}, capability)
    assert evidence.injection_flagged


def test_k8s_state_never_returns_secret_values(registry, capability) -> None:
    evidence = registry.call("get_k8s_state", {"service": "checkout-service"}, capability)
    assert "DATABASE_URL" in evidence.summary  # the name is useful
    assert "postgres://" not in evidence.summary  # the value is not available at all


def test_change_evidence_is_tagged_with_its_service(registry, capability) -> None:
    """Otherwise the deploy that explains the outage never reaches the report."""
    evidence = registry.call(
        "get_recent_changes", {"service": "checkout-service", "window_minutes": 60}, capability
    )
    assert evidence.facts["service"] == "checkout-service"
    assert evidence.facts["changes"] == 1


def test_topology_depth_is_capped(registry, capability) -> None:
    evidence = registry.call("get_topology", {"service": "frontend", "depth": 99}, capability)
    assert "checkout-service" in evidence.facts["downstream"]["frontend"]


def test_knowledge_search_abstains_rather_than_inventing_a_runbook(registry, capability) -> None:
    evidence = registry.call("search_knowledge", {"query": "checkout 5xx"}, capability)
    assert evidence.facts["knowledge_gap"] is True
    assert evidence.facts["hits"] == 0


def test_unknown_tool_is_an_error_not_a_guess(registry, capability) -> None:
    with pytest.raises(ToolError, match="unknown tool"):
        registry.call("drop_database", {}, capability)


def test_write_tools_cannot_be_called_through_the_read_path(registry, capability) -> None:
    """The single most important boundary in the system."""
    with pytest.raises(PolicyError, match="approval token"):
        registry.call("rollback", {"service": "checkout-service"}, capability)


def test_a_capability_scoped_to_one_namespace_cannot_read_another(
    registry, signer: TokenSigner
) -> None:
    scoped = signer.read_capability(
        signer.mint_capability("acme", "inc_1", namespaces=("payments",))
    )
    with pytest.raises(PolicyError, match="does not allow"):
        registry.call("get_service_health", {"service": "checkout-service"}, scoped)


def test_execute_rejects_a_grant_for_a_different_action(registry, signer: TokenSigner) -> None:
    action = registry.propose_action("checkout-service", "bad_deploy")
    assert action is not None
    grant = signer.read_approval(
        signer.mint_approval("acme", "inc_1", "act_somethingelse", "rollback", action.target, "sre")
    )
    with pytest.raises(PolicyError, match="does not match"):
        registry.execute(action, grant, "key")


def test_execute_rejects_a_grant_for_a_different_target(registry, signer: TokenSigner) -> None:
    action = registry.propose_action("checkout-service", "bad_deploy")
    assert action is not None
    grant = signer.read_approval(
        signer.mint_approval("acme", "inc_1", action.id, "rollback", "shop/payment-service", "sre")
    )
    with pytest.raises(PolicyError, match="different target"):
        registry.execute(action, grant, "key")


def test_proposal_is_a_rollback_to_the_previous_revision(registry: ToolRegistry) -> None:
    action = registry.propose_action("checkout-service", "bad_deploy")
    assert action is not None
    assert action.kind is ActionKind.ROLLBACK
    assert action.arguments["to_revision"] == "2.13.4"
    assert action.approval_mode.value == "requires_approval"


def test_no_action_is_proposed_without_a_matching_catalog_entry(registry: ToolRegistry) -> None:
    assert registry.propose_action("payment-service", "network") is None


def test_a_successful_rollback_clears_the_victims_too(registry, signer, capability) -> None:
    action = registry.propose_action("checkout-service", "bad_deploy")
    assert action is not None
    grant = signer.read_approval(
        signer.mint_approval("acme", "inc_1", action.id, "rollback", action.target, "sre")
    )
    registry.execute(action, grant, "key")

    for service in ("checkout-service", "frontend"):
        evidence = registry.call("get_service_health", {"service": service}, capability)
        assert evidence.facts["anomalous"] is False, service
