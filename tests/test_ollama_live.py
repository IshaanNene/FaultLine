"""Live checks against a real Ollama daemon.

Skipped unless one is reachable with the configured models pulled, so CI and a
laptop with nothing running both stay green. What these cover is the one thing a
mock cannot: that a real local model, under constrained decoding, actually
produces output these schemas accept.

Run them with:  pytest -m ollama
"""

from __future__ import annotations

import pytest
from tests.conftest import make_alert

from faultline.core.schemas import FaultClass, Hypothesis, TriageVerdict
from faultline.worker.models import Task
from faultline.worker.ollama_model import DEFAULT_HOST, OllamaModel, available

pytestmark = pytest.mark.ollama

SMALL = "llama3.2:3b"
FRONTIER = "llama3.1:8b"


async def _require(model: str) -> None:
    pulled = await available(DEFAULT_HOST)
    if not pulled:
        pytest.skip("no Ollama daemon reachable on localhost:11434")
    if model not in pulled:
        pytest.skip(f"{model} not pulled (have: {', '.join(pulled[:4])})")


async def test_a_real_small_model_produces_a_valid_triage_verdict() -> None:
    await _require(SMALL)
    model = OllamaModel(SMALL)
    try:
        verdict, usage = await model.invoke(
            Task.TRIAGE,
            {"alerts": [make_alert(service="frontend"), make_alert(service="checkout-service")]},
        )
    finally:
        await model.aclose()

    assert isinstance(verdict, TriageVerdict)
    # The enum is the point: this field used to be a free string and a local
    # model answered "critical".
    assert verdict.severity in {"P1", "P2", "P3", "P4"}
    assert verdict.classification in {"noise", "duplicate", "actionable"}
    assert usage.tokens > 0
    assert usage.usd == 0.0


async def test_a_real_model_produces_competing_hypotheses() -> None:
    await _require(FRONTIER)
    model = OllamaModel(FRONTIER)
    try:
        hypotheses, _ = await model.invoke(
            Task.HYPOTHESIZE,
            {
                "suspects": [
                    {"service": "checkout-service", "score": 613.5, "why": "changed just before"},
                    {"service": "frontend", "score": 0.2, "why": "anomalous dependency"},
                ],
                "changed_services": ["checkout-service"],
                "alert_services": ["frontend", "checkout-service"],
                "existing": [],
            },
        )
    finally:
        await model.aclose()

    assert all(isinstance(h, Hypothesis) for h in hypotheses)
    # The prompt demands a differential, not a single answer.
    assert len(hypotheses) >= 2
    assert all(h.refuting_test.strip() for h in hypotheses)
    assert all(h.fault_class in set(FaultClass) for h in hypotheses)
