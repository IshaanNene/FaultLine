"""The Anthropic tier, driven end to end against a fake client.

No network and no API key: a fake stands in for `client.messages.parse` and
returns the schema objects the real SDK would. That covers everything up to the
wire -- request shape, cache placement, structured-output schema, response
adaptation, cost accounting, refusal and truncation handling -- which is the
part that can actually be wrong in a way tests catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from tests.conftest import make_alert

from faultline.gateway.registry import READ_TOOLS
from faultline.worker import responses as r
from faultline.worker.anthropic_model import (
    EFFORT,
    MAX_TOKENS,
    PRICING,
    SCHEMA,
    AnthropicModel,
    ModelRefused,
    TruncatedResponse,
    price,
)
from faultline.worker.models import TIER, Task, build_router


@dataclass
class FakeUsage:
    input_tokens: int = 1_000
    output_tokens: int = 200
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeMessage:
    parsed_output: Any
    stop_reason: str = "end_turn"
    stop_details: Any = None
    usage: FakeUsage = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = FakeUsage()


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    def __init__(self, result: Any) -> None:
        self.messages = FakeMessages(result)


def model_for(result: Any, name: str = "claude-opus-5") -> tuple[AnthropicModel, FakeClient]:
    client = FakeClient(result)
    return AnthropicModel(name, client=client), client


# -- request shape --------------------------------------------------------


async def test_the_system_prompt_is_sent_as_a_cached_block() -> None:
    """Prompt caching is the difference between paying once and paying every time."""
    model, client = model_for(
        FakeMessage(
            r.TriageResponse(
                classification=r.TriageClassification.ACTIONABLE,
                severity="P1",
                affected_services=["frontend"],
                summary="s",
            )
        )
    )
    await model.invoke(Task.TRIAGE, {"alerts": [make_alert()]})

    system = client.messages.calls[0]["system"]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}


async def test_nothing_incident_specific_leaks_into_the_cached_prefix() -> None:
    """Two different incidents must produce a byte-identical system block."""
    responses = [
        FakeMessage(
            r.TriageResponse(
                classification=r.TriageClassification.ACTIONABLE,
                severity="P1",
                affected_services=[],
                summary="s",
            )
        )
        for _ in range(2)
    ]
    systems = []
    for i, resp in enumerate(responses):
        model, client = model_for(resp)
        await model.invoke(Task.TRIAGE, {"alerts": [make_alert(service=f"svc-{i}")]})
        systems.append(client.messages.calls[0]["system"][0]["text"])

    assert systems[0] == systems[1]


async def test_the_request_asks_for_the_task_schema() -> None:
    model, client = model_for(FakeMessage(r.SummaryResponse(summary="done")))
    await model.invoke(Task.SUMMARIZE, {"report": None})

    call = client.messages.calls[0]
    assert call["output_format"] is r.SummaryResponse
    assert call["output_config"]["effort"] == EFFORT[Task.SUMMARIZE]
    assert call["max_tokens"] == MAX_TOKENS[Task.SUMMARIZE]


async def test_adaptive_thinking_only_where_the_model_supports_it() -> None:
    frontier, frontier_client = model_for(FakeMessage(r.SummaryResponse(summary="x")))
    await frontier.invoke(Task.SUMMARIZE, {"report": None})
    assert frontier_client.messages.calls[0]["thinking"] == {"type": "adaptive"}

    small, small_client = model_for(
        FakeMessage(r.SummaryResponse(summary="x")), name="claude-haiku-4-5"
    )
    await small.invoke(Task.SUMMARIZE, {"report": None})
    assert "thinking" not in small_client.messages.calls[0]


# -- configuration --------------------------------------------------------


@pytest.mark.parametrize("task", list(Task))
def test_every_task_has_a_schema_budget_and_effort(task: Task) -> None:
    assert task in SCHEMA
    assert task in MAX_TOKENS
    assert task in EFFORT


@pytest.mark.parametrize("model_id", sorted(PRICING))
def test_model_ids_are_never_date_suffixed(model_id: str) -> None:
    """A date-suffixed id is rejected as an unknown model."""
    tail = model_id.rsplit("-", 1)[-1]
    assert not (tail.isdigit() and len(tail) == 8), model_id


def test_the_tool_enum_matches_the_registry() -> None:
    """If a tool is added to the gateway but not the schema, the model can never
    ask for it -- and nothing else would notice."""
    assert {t.value for t in r.ReadTool} == set(READ_TOOLS)


def test_frontier_tasks_route_to_the_frontier_tier() -> None:
    assert TIER[Task.SYNTHESIZE] == "frontier"
    assert TIER[Task.TRIAGE] == "small"


def test_the_router_builds_both_tiers_with_fallbacks() -> None:
    router = build_router(
        "anthropic",
        frontier="claude-opus-5",
        small="claude-haiku-4-5",
        frontier_fallback="claude-sonnet-5",
        client=FakeClient(None),
    )
    assert [m.name for m in router._tiers["frontier"]] == [
        "anthropic:claude-opus-5",
        "anthropic:claude-sonnet-5",
    ]
    assert [m.name for m in router._tiers["small"]] == ["anthropic:claude-haiku-4-5"]


def test_a_fallback_identical_to_the_primary_is_not_duplicated() -> None:
    router = build_router(
        "anthropic",
        frontier="claude-opus-5",
        frontier_fallback="claude-opus-5",
        client=FakeClient(None),
    )
    assert len(router._tiers["frontier"]) == 1


def test_an_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown model provider"):
        build_router("openai")


# -- cost accounting ------------------------------------------------------


def test_uncached_usage_is_priced_from_the_table() -> None:
    usage = price("claude-opus-5", FakeUsage(input_tokens=1_000_000, output_tokens=0))
    assert usage.usd == pytest.approx(5.00)

    usage = price("claude-opus-5", FakeUsage(input_tokens=0, output_tokens=1_000_000))
    assert usage.usd == pytest.approx(25.00)


def test_cache_reads_are_billed_at_the_cache_rate() -> None:
    """The whole point of the stable prefix: the same tokens, an order of
    magnitude cheaper."""
    cold = price("claude-opus-5", FakeUsage(input_tokens=1_000_000, output_tokens=0))
    warm = price(
        "claude-opus-5",
        FakeUsage(input_tokens=0, output_tokens=0, cache_read_input_tokens=1_000_000),
    )
    assert warm.usd < cold.usd / 9
    assert warm.tokens == cold.tokens  # same context processed, different price


def test_cache_writes_cost_more_than_plain_input() -> None:
    write = price(
        "claude-opus-5",
        FakeUsage(input_tokens=0, output_tokens=0, cache_creation_input_tokens=1_000_000),
    )
    assert write.usd > 5.00


def test_an_unpriced_model_raises_rather_than_billing_zero() -> None:
    """Silently costing nothing would make every budget ceiling meaningless."""
    with pytest.raises(KeyError, match="no pricing"):
        price("claude-imaginary-9", FakeUsage())


async def test_usage_is_charged_from_the_api_response() -> None:
    model, _ = model_for(
        FakeMessage(
            r.SummaryResponse(summary="x"),
            usage=FakeUsage(input_tokens=12_345, output_tokens=678),
        )
    )
    _, usage = await model.invoke(Task.SUMMARIZE, {"report": None})
    assert usage.tokens == 13_023
    assert usage.model == "claude-opus-5"


# -- failure handling -----------------------------------------------------


async def test_a_refusal_is_raised_not_parsed() -> None:
    @dataclass
    class Details:
        category: str = "cyber"

    model, _ = model_for(FakeMessage(None, stop_reason="refusal", stop_details=Details()))
    with pytest.raises(ModelRefused, match="cyber"):
        await model.invoke(Task.SUMMARIZE, {"report": None})


async def test_a_truncated_response_is_not_half_parsed() -> None:
    """Half a report is worse than no report; let the router try elsewhere."""
    model, _ = model_for(FakeMessage(None, stop_reason="max_tokens"))
    with pytest.raises(TruncatedResponse):
        await model.invoke(Task.SUMMARIZE, {"report": None})


async def test_a_missing_parse_result_is_an_error() -> None:
    model, _ = model_for(FakeMessage(None))
    with pytest.raises(TruncatedResponse):
        await model.invoke(Task.SUMMARIZE, {"report": None})


async def test_the_router_fails_over_to_the_second_provider() -> None:
    primary = AnthropicModel("claude-opus-5", client=FakeClient(RuntimeError("503")))
    backup = AnthropicModel(
        "claude-sonnet-5", client=FakeClient(FakeMessage(r.SummaryResponse(summary="rescued")))
    )
    from faultline.worker.models import ModelRouter

    router = ModelRouter({"small": [primary, backup], "frontier": [primary, backup]})
    result, usage = await router.invoke(Task.SUMMARIZE, {"report": None})

    assert result == "rescued"
    assert usage.model == "claude-sonnet-5"
