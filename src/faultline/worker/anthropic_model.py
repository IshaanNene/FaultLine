"""The Anthropic model tier.

Implements the same `Model` protocol as `StubModel`, so swapping it in changes
the quality of the judgment and nothing about the shape of the graph.

Four things this file is responsible for:

- **Structured output.** Every task parses into a Pydantic schema from
  `worker.responses` via `messages.parse`, so a malformed answer is a validation
  error at the boundary rather than a surprise three nodes later.
- **Real cost accounting.** Usage comes from the API response, priced per model,
  with cache reads and writes charged at their own rates. The budget ceilings in
  `core.budget` are only meaningful if this number is true.
- **Cache-friendly request shape.** The system prompt is a stable, cached prefix;
  everything per-incident is in the user message.
- **Honest failure.** Transient errors raise so the router can fail over to
  another provider; a refusal or a validation failure is reported as what it is.
"""

from __future__ import annotations

from typing import Any

import anthropic
from pydantic import ValidationError

from faultline.logging import get_logger
from faultline.worker.adapt import to_domain
from faultline.worker.models import TIER, ModelRefused, Task, TruncatedResponse, Usage
from faultline.worker.prompts import PROMPT_VERSION, SYSTEM, render_user
from faultline.worker.responses import SCHEMA

log = get_logger(__name__)

# Output ceilings. Generous enough not to truncate a report, small enough that a
# runaway response cannot eat the incident's whole token budget.
MAX_TOKENS: dict[Task, int] = {
    Task.TRIAGE: 1_024,
    Task.HYPOTHESIZE: 4_096,
    Task.PLAN: 4_096,
    Task.ASSESS: 8_192,
    Task.SYNTHESIZE: 16_000,
    Task.ENTAIL: 512,
    Task.SUMMARIZE: 512,
}

# Effort per task: how hard the model should think. Triage runs on every alert
# and must be cheap; synthesis runs once and is the artifact a human reads.
EFFORT: dict[Task, str] = {
    Task.TRIAGE: "low",
    Task.HYPOTHESIZE: "high",
    Task.PLAN: "medium",
    Task.ASSESS: "high",
    Task.SYNTHESIZE: "high",
    Task.ENTAIL: "low",
    Task.SUMMARIZE: "low",
}

# USD per million tokens: (input, output, cache_write, cache_read).
# Cache writes cost ~1.25x input, reads ~0.1x.
PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
}

# Models that take adaptive thinking. Haiku 4.5 still uses the older
# budget_tokens form, so it simply runs without thinking here.
ADAPTIVE_THINKING = frozenset({"claude-opus-5", "claude-sonnet-5"})


class AnthropicModel:
    """One tier (frontier or small) backed by a real model."""

    def __init__(
        self,
        model: str,
        client: Any | None = None,
        name: str | None = None,
    ) -> None:
        self.model = model
        self.name = name or f"anthropic:{model}"
        # Injectable so tests can drive the whole adapter without a network call
        # or an API key. The zero-arg client resolves credentials from the
        # environment or an `ant auth login` profile.
        self._client = client or anthropic.AsyncAnthropic()

    async def invoke(self, task: Task, context: dict[str, Any]) -> tuple[Any, Usage]:
        schema = SCHEMA[task]
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS[task],
            # A list with cache_control, not a bare string: this is the prefix
            # that must stay cached across every investigation.
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM[task],
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": render_user(task, context)}],
            "output_format": schema,
            "output_config": {"effort": EFFORT[task]},
        }
        if self.model in ADAPTIVE_THINKING:
            request["thinking"] = {"type": "adaptive"}

        message = await self._client.messages.parse(**request)

        if message.stop_reason == "refusal":
            detail = getattr(message.stop_details, "category", None)
            raise ModelRefused(f"{self.model} declined this request ({detail})")
        if message.stop_reason == "max_tokens":
            # Structured output truncated mid-object is not recoverable; let the
            # router try elsewhere rather than parsing a half-written report.
            raise TruncatedResponse(f"{task} hit max_tokens on {self.model}")

        parsed = message.parsed_output
        if parsed is None:
            raise TruncatedResponse(f"{task} returned no parsable output")
        if not isinstance(parsed, schema):
            raise ValidationError.from_exception_data(schema.__name__, [])

        usage = price(self.model, message.usage)
        log.info(
            "model_call",
            task=task.value,
            model=self.model,
            tier=TIER[task],
            prompt_version=PROMPT_VERSION,
            tokens=usage.tokens,
            usd=usage.usd,
            cache_read=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
        )
        # Domain objects out, so nodes cannot tell which tier answered.
        return to_domain(task, parsed, context), usage


def price(model: str, usage: Any) -> Usage:
    """Turn an API usage object into billed tokens and dollars.

    Cached reads are counted in the token total -- they are real context the
    model processed -- but charged at the cache rate, which is the whole reason
    the stable-prefix design pays for itself.
    """
    rates = PRICING.get(model)
    if rates is None:
        # An unknown model is a config error, not a reason to bill nothing.
        raise KeyError(f"no pricing for model {model!r}; add it to PRICING")
    in_rate, out_rate, write_rate, read_rate = rates

    plain_in = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

    usd = (
        plain_in * in_rate + out * out_rate + cache_write * write_rate + cache_read * read_rate
    ) / 1_000_000
    return Usage(
        tokens=plain_in + out + cache_write + cache_read,
        usd=round(usd, 6),
        model=model,
    )
