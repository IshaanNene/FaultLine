"""The Ollama tier, driven against a mock transport.

Real httpx, real request bodies, no daemon: the fake sits at the transport layer
so the request Ollama would receive is exactly what these tests inspect.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from faultline.core.schemas import Check, TriageVerdict
from faultline.worker import responses as r
from faultline.worker.models import Task, TruncatedResponse, build_router
from faultline.worker.ollama_model import (
    NUM_CTX,
    NUM_PREDICT,
    OllamaModel,
    available,
    measure,
)


def _reply(content: str, *, done_reason: str = "stop", **usage: Any) -> dict[str, Any]:
    return {
        "model": "llama3.1:8b",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": usage.get("prompt_eval_count", 100),
        "eval_count": usage.get("eval_count", 50),
        "total_duration": usage.get("total_duration", 5_000_000_000),
    }


def make_model(reply: dict[str, Any] | Exception, model: str = "llama3.1:8b"):
    """An OllamaModel whose transport returns `reply`, plus the captured requests."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json=reply)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaModel(model, client=client), seen


TRIAGE_JSON = json.dumps(
    {
        "classification": "actionable",
        "severity": "P1",
        "affected_services": ["frontend"],
        "summary": "frontend 5xx",
    }
)


# -- request shape --------------------------------------------------------


async def test_the_request_carries_the_json_schema() -> None:
    """Constrained decoding is what makes a small model's output parseable."""
    model, seen = make_model(_reply(TRIAGE_JSON))
    await model.invoke(Task.TRIAGE, {"alerts": []})

    body = seen[0]
    assert body["format"] == r.TriageResponse.model_json_schema()
    assert body["stream"] is False


async def test_generation_is_deterministic_and_bounded() -> None:
    model, seen = make_model(_reply(TRIAGE_JSON))
    await model.invoke(Task.TRIAGE, {"alerts": []})

    options = seen[0]["options"]
    assert options["temperature"] == 0
    assert options["num_predict"] == NUM_PREDICT[Task.TRIAGE]
    # An unset context window silently truncates a prompt carrying a full ledger.
    assert options["num_ctx"] == NUM_CTX


async def test_the_same_prompts_are_used_as_the_hosted_tier() -> None:
    """All three tiers must be comparable on the same incidents, or the eval
    harness is measuring the prompt rather than the model."""
    from faultline.worker.prompts import SYSTEM

    model, seen = make_model(_reply(TRIAGE_JSON))
    await model.invoke(Task.TRIAGE, {"alerts": []})

    messages = seen[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == SYSTEM[Task.TRIAGE]


async def test_the_host_is_configurable() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_reply(TRIAGE_JSON))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = OllamaModel("llama3.1:8b", host="http://gpu-box:11434/", client=client)
    await model.invoke(Task.TRIAGE, {"alerts": []})

    assert seen[0] == "http://gpu-box:11434/api/chat"


# -- adaptation -----------------------------------------------------------


async def test_a_response_becomes_a_domain_object() -> None:
    model, _ = make_model(_reply(TRIAGE_JSON))
    verdict, _usage = await model.invoke(Task.TRIAGE, {"alerts": []})

    assert isinstance(verdict, TriageVerdict)
    assert verdict.classification == "actionable"
    assert verdict.severity == "P1"


async def test_planned_checks_adapt_like_any_other_tier() -> None:
    content = json.dumps(
        {
            "checks": [
                {
                    "tool": "search_logs",
                    "arguments": {"service": "checkout-service", "level": "ERROR"},
                    "targets_hypotheses": ["hyp_1"],
                    "rationale": "new error templates would separate the two",
                }
            ]
        }
    )
    model, _ = make_model(_reply(content))
    checks, _ = await model.invoke(
        Task.PLAN, {"hypotheses": [], "already_run": set(), "remaining_tool_calls": 5}
    )
    assert isinstance(checks[0], Check)
    assert checks[0].tool == "search_logs"


# -- failure handling -----------------------------------------------------


async def test_hitting_the_output_limit_is_not_half_parsed() -> None:
    model, _ = make_model(_reply('{"classification": "action', done_reason="length"))
    with pytest.raises(TruncatedResponse, match="output limit"):
        await model.invoke(Task.TRIAGE, {"alerts": []})


async def test_an_empty_response_is_an_error() -> None:
    model, _ = make_model(_reply("   "))
    with pytest.raises(TruncatedResponse, match="empty"):
        await model.invoke(Task.TRIAGE, {"alerts": []})


async def test_output_failing_a_pydantic_constraint_is_rejected() -> None:
    """Constrained decoding guarantees the shape, not the constraints: a
    confidence of 4.2 is schema-shaped and still invalid."""
    content = json.dumps(
        {
            "assessments": [
                {
                    "hypothesis_id": "hyp_1",
                    "status": "supported",
                    "confidence": 4.2,
                    "supporting_evidence": [],
                    "refuting_evidence": [],
                    "reasoning": "",
                }
            ],
            "decision": "conclude",
        }
    )
    model, _ = make_model(_reply(content))
    with pytest.raises(TruncatedResponse, match="failed validation"):
        await model.invoke(Task.ASSESS, {"hypotheses": []})


async def test_a_daemon_that_is_down_raises_for_the_router_to_handle() -> None:
    model, _ = make_model(httpx.ConnectError("connection refused"))
    with pytest.raises(httpx.ConnectError):
        await model.invoke(Task.TRIAGE, {"alerts": []})


# -- accounting -----------------------------------------------------------


def test_tokens_are_counted_and_cost_is_genuinely_zero() -> None:
    """Local inference is not metered per token, so the dollar ceiling never
    binds on this tier -- the deadline is what bounds an investigation."""
    usage = measure("llama3.1:8b", _reply("{}", prompt_eval_count=1_200, eval_count=340))
    assert usage.tokens == 1_540
    assert usage.usd == 0.0
    assert usage.model == "llama3.1:8b"


def test_missing_counters_do_not_crash_accounting() -> None:
    assert measure("llama3.1:8b", {}).tokens == 0


# -- routing --------------------------------------------------------------


def test_the_router_builds_local_tiers() -> None:
    router = build_router("ollama", frontier="llama3.1:8b", small="llama3.2:3b")
    assert [m.name for m in router._tiers["frontier"]] == ["ollama:llama3.1:8b"]
    assert [m.name for m in router._tiers["small"]] == ["ollama:llama3.2:3b"]


def test_an_unknown_provider_names_all_three() -> None:
    with pytest.raises(ValueError, match="'stub', 'anthropic' or 'ollama'"):
        build_router("gpt4all")


async def test_availability_probe_is_quiet_when_nothing_is_listening() -> None:
    """Used to skip live tests; it must never raise."""
    assert await available("http://127.0.0.1:1", timeout=0.2) == []
