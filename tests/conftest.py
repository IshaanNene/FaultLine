from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from faultline.config import Settings
from faultline.core.alerts import Alert
from faultline.core.budget import Budget
from faultline.gateway.backends.scenario import bad_deploy_scenario
from faultline.gateway.policy import TokenSigner
from faultline.gateway.registry import ToolRegistry


@pytest.fixture
def settings() -> Settings:
    return Settings(backend="memory", environment="ci", gateway_signing_key="test-key")


@pytest.fixture
def scenario():
    return bad_deploy_scenario()


@pytest.fixture
def registry(scenario) -> ToolRegistry:
    return ToolRegistry(scenario)


@pytest.fixture
def signer(settings) -> TokenSigner:
    return TokenSigner(settings.gateway_signing_key)


@pytest.fixture
def budget() -> Budget:
    return Budget(
        max_tokens=500_000,
        max_usd=2.5,
        max_tool_calls=40,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def make_alert(
    name: str = "HighErrorRate",
    service: str = "frontend",
    severity: str = "critical",
    namespace: str = "shop",
    minutes_ago: int = 10,
    status: str = "firing",
) -> Alert:
    return Alert(
        status=status,
        labels={
            "alertname": name,
            "service": service,
            "severity": severity,
            "namespace": namespace,
        },
        starts_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
