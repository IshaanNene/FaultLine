"""Correlation and fingerprinting: the cheap pass that runs before any model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.conftest import make_alert

from faultline.core.alerts import Alert, Severity, correlate


def test_fingerprint_ignores_pod_identity() -> None:
    """One crashlooping deployment is one alert, not one per pod."""
    a = Alert(labels={"alertname": "OOM", "service": "cart", "namespace": "shop", "pod": "cart-a"})
    b = Alert(labels={"alertname": "OOM", "service": "cart", "namespace": "shop", "pod": "cart-z"})
    assert a.fingerprint == b.fingerprint


def test_fingerprint_separates_different_alerts() -> None:
    a = Alert(labels={"alertname": "OOM", "service": "cart", "namespace": "shop"})
    b = Alert(labels={"alertname": "OOM", "service": "checkout", "namespace": "shop"})
    assert a.fingerprint != b.fingerprint


def test_correlate_collapses_a_storm_into_one_group() -> None:
    storm = [make_alert(name=f"Alert{i}", service=f"svc-{i}", minutes_ago=10) for i in range(50)]
    groups = correlate(storm, tenant_id="acme", window_seconds=120)
    assert len(groups) == 1
    assert len(groups[0].alerts) == 50


def test_correlate_splits_across_namespaces() -> None:
    groups = correlate(
        [make_alert(namespace="shop"), make_alert(namespace="payments")],
        tenant_id="acme",
    )
    assert len(groups) == 2


def test_correlate_splits_outside_the_window() -> None:
    groups = correlate(
        [make_alert(minutes_ago=60), make_alert(minutes_ago=1)],
        tenant_id="acme",
        window_seconds=120,
    )
    assert len(groups) == 2


def test_correlate_ignores_resolved_alerts() -> None:
    assert correlate([make_alert(status="resolved")], tenant_id="acme") == []


def test_group_key_is_deterministic() -> None:
    """The idempotency guarantee: the same alerts always produce the same key."""
    alerts = [make_alert(), make_alert(service="checkout-service")]
    first = correlate(alerts, tenant_id="acme")[0].group_key
    second = correlate(list(reversed(alerts)), tenant_id="acme")[0].group_key
    assert first == second


def test_group_severity_is_the_worst_member() -> None:
    group = correlate(
        [make_alert(severity="warning"), make_alert(severity="critical")], tenant_id="acme"
    )[0]
    assert group.severity is Severity.P1


def test_window_starts_before_the_first_alert() -> None:
    group = correlate([make_alert(minutes_ago=10)], tenant_id="acme")[0]
    window = group.window(lookback=timedelta(minutes=30))
    assert window.start < group.first_firing_at
    assert window.end >= datetime.now(UTC) - timedelta(seconds=5)


def test_window_clamps_to_a_maximum() -> None:
    group = correlate([make_alert(minutes_ago=600)], tenant_id="acme")[0]
    clamped = group.window().clamp(timedelta(hours=1))
    assert clamped.duration <= timedelta(hours=1)
