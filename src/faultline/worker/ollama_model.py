"""The Ollama model tier: local models, no API key, no per-token cost.

Implements the same `Model` protocol as the stub and the Anthropic tier, using
the same prompts and the same response schemas, so all three are directly
comparable on the same incidents -- which is exactly what the evaluation harness
will need.

Two things differ from a hosted tier and both are visible in the code:

**Cost is zero, time is not.** There is nothing to bill, so the dollar ceiling in
`core.budget` never binds and the *deadline* becomes the real constraint. Local
inference is slow, and a cold model load can take tens of seconds on its own.

**The models are much weaker.** Everything that keeps a strong model honest --
closed enums, typed arguments, no field for a proposed action, verification
against the ledger -- matters more here, not less. Ollama constrains generation
to the JSON Schema, so the output parses; whether it is *right* is what the
verify node is for.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError

from faultline.logging import get_logger
from faultline.worker.adapt import to_domain
from faultline.worker.models import TIER, Task, TruncatedResponse, Usage
from faultline.worker.prompts import PROMPT_VERSION, SYSTEM, render_user
from faultline.worker.responses import SCHEMA

log = get_logger(__name__)

DEFAULT_HOST = "http://localhost:11434"

# Output ceilings, mirroring the Anthropic tier's budgets. Ollama calls this
# num_predict; -1 would mean "until the model stops", which on a small model can
# mean a very long time.
NUM_PREDICT: dict[Task, int] = {
    Task.TRIAGE: 512,
    Task.HYPOTHESIZE: 2_048,
    Task.PLAN: 2_048,
    Task.ASSESS: 4_096,
    Task.SYNTHESIZE: 4_096,
    Task.ENTAIL: 256,
    Task.SUMMARIZE: 256,
}

# Context window. Local defaults are often 2-4k, which silently truncates a
# prompt carrying a full evidence ledger -- the failure looks like a bad answer
# rather than an error, so it is set explicitly.
NUM_CTX = 8_192

# A cold model load happens on the first call and is not a hang.
REQUEST_TIMEOUT_S = 300.0


class OllamaModel:
    """One tier backed by a locally served model."""

    def __init__(
        self,
        model: str,
        host: str = DEFAULT_HOST,
        client: httpx.AsyncClient | None = None,
        name: str | None = None,
    ) -> None:
        self.model = model
        self.name = name or f"ollama:{model}"
        self._host = host.rstrip("/")
        self._client = client
        self._owned_client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        return self._owned_client

    async def invoke(self, task: Task, context: dict[str, Any]) -> tuple[Any, Usage]:
        schema = SCHEMA[task]
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM[task]},
                {"role": "user", "content": render_user(task, context)},
            ],
            # Constrained decoding against the same schema the hosted tier asks
            # for. This is what makes a 3B model's output parseable at all.
            "format": schema.model_json_schema(),
            "stream": False,
            "options": {
                # Investigations should be reproducible: same evidence, same
                # answer. Temperature 0 is the closest local inference gets.
                "temperature": 0,
                "num_predict": NUM_PREDICT[task],
                "num_ctx": NUM_CTX,
            },
        }

        response = await self._http().post(f"{self._host}/api/chat", json=body)
        response.raise_for_status()
        payload = response.json()

        if payload.get("done_reason") == "length":
            raise TruncatedResponse(
                f"{task} hit the {NUM_PREDICT[task]}-token output limit on {self.model}"
            )

        content = payload.get("message", {}).get("content", "")
        if not content.strip():
            raise TruncatedResponse(f"{task} returned an empty response from {self.model}")

        try:
            parsed = schema.model_validate_json(content)
        except ValidationError as exc:
            # Constrained decoding makes this rare, but a model can still emit
            # schema-valid-shaped text that fails a Pydantic constraint. Raise so
            # the router can try the next model rather than adapting garbage.
            raise TruncatedResponse(
                f"{task} produced output that failed validation on {self.model}: {exc}"
            ) from exc

        usage = measure(self.model, payload)
        log.info(
            "model_call",
            task=task.value,
            model=self.model,
            tier=TIER[task],
            prompt_version=PROMPT_VERSION,
            tokens=usage.tokens,
            seconds=round(payload.get("total_duration", 0) / 1e9, 1),
        )
        return to_domain(task, parsed, context), usage

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None


def measure(model: str, payload: dict[str, Any]) -> Usage:
    """Token counts from an Ollama response.

    `usd` is genuinely zero -- the electricity is not metered per token -- so the
    dollar ceiling never binds on this tier and the deadline does the work
    instead. Tokens are still counted, because the token ceiling and the
    cost-comparison in the eval harness both need them.
    """
    return Usage(
        tokens=int(payload.get("prompt_eval_count", 0) or 0)
        + int(payload.get("eval_count", 0) or 0),
        usd=0.0,
        model=model,
    )


async def available(host: str = DEFAULT_HOST, timeout: float = 3.0) -> list[str]:
    """Models this Ollama instance has pulled. Empty if it is not reachable."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{host.rstrip('/')}/api/tags")
            response.raise_for_status()
            return sorted(m["name"] for m in response.json().get("models", []))
    except (httpx.HTTPError, KeyError, ValueError):
        return []
