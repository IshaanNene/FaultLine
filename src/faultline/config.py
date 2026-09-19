"""Process configuration, loaded from the environment.

Every entrypoint (api, worker, gateway, ingest) reads the same settings object so
that a misconfigured backend fails the same way everywhere.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FAULTLINE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["local", "ci", "staging", "production"] = "local"
    log_level: str = "INFO"

    # Storage. "memory" runs the whole system with no infrastructure, which is what
    # the tests and `faultline demo` use.
    backend: Literal["memory", "live"] = "memory"
    postgres_dsn: str = "postgresql://faultline:faultline@localhost:5432/faultline"
    redis_url: str = "redis://localhost:6379/0"

    # Queue topology.
    alerts_stream: str = "faultline:alerts"
    jobs_stream: str = "faultline:jobs"
    consumer_group: str = "investigators"
    job_reclaim_idle_ms: int = 5 * 60 * 1000
    job_max_deliveries: int = 5

    # Tool gateway.
    gateway_url: str = "http://localhost:8081"
    gateway_signing_key: str = Field(
        default="dev-only-not-a-secret",
        description=(
            "HMAC key for capability and approval tokens. Injected from a "
            "secret store outside local development."
        ),
    )

    # Investigation budgets. These are ceilings, not targets; see docs/adr/0005.
    budget_max_tokens: int = 500_000
    budget_max_usd: float = 2.50
    budget_max_tool_calls: int = 40
    budget_deadline_seconds: int = 300
    max_iterations: int = 4

    # Correlation: how long an alert group stays open for new members to join.
    correlation_window_seconds: int = 120

    # Models. The skeleton ships a deterministic stub router; see worker/models.py.
    model_provider: Literal["stub", "anthropic", "ollama"] = "stub"
    # Bare ids, never date-suffixed: a suffixed id is rejected as an unknown model.
    model_frontier: str = "claude-opus-5"
    model_small: str = "claude-haiku-4-5"
    # The second provider for each tier, so one provider's outage is a slower
    # investigation rather than none. Empty disables failover.
    model_frontier_fallback: str = "claude-sonnet-5"
    model_small_fallback: str = ""

    # Ollama: local models, no API key, no per-token cost. The deadline rather
    # than the dollar ceiling is what bounds an investigation on this tier.
    ollama_host: str = "http://localhost:11434"
    ollama_frontier: str = "llama3.1:8b"
    ollama_small: str = "llama3.2:3b"

    # Retrieval corpus. Absent is a supported state: search_knowledge abstains
    # and logs a knowledge gap, exactly as it does when nothing is relevant.
    corpus_path: str = "corpus"
    embed_provider: Literal["none", "ollama"] = "ollama"
    embed_model: str = "nomic-embed-text"

    @property
    def is_live(self) -> bool:
        return self.backend == "live"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
