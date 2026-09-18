"""Investigation budgets.

Four independent ceilings: tokens, dollars, tool calls and wall-clock. Any one of
them exhausting forces the graph to stop and report honestly rather than loop.
The blueprint's target is ~0.5M tokens, roughly what SREGym measured for a
specialized SRE agent against 1.6-1.9M for general coding agents.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field


class BudgetExceeded(RuntimeError):
    """Raised only where a hard stop is correct; the graph normally checks `exhausted`."""

    def __init__(self, dimension: str, used: float, limit: float) -> None:
        super().__init__(f"budget exhausted on {dimension}: {used} of {limit}")
        self.dimension = dimension
        self.used = used
        self.limit = limit


class Budget(BaseModel):
    max_tokens: int = 500_000
    max_usd: float = 2.50
    max_tool_calls: int = 40
    deadline: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=5))

    tokens_used: int = 0
    usd_used: float = 0.0
    tool_calls_used: int = 0

    @classmethod
    def from_settings(
        cls,
        max_tokens: int,
        max_usd: float,
        max_tool_calls: int,
        deadline_seconds: int,
    ) -> Budget:
        return cls(
            max_tokens=max_tokens,
            max_usd=max_usd,
            max_tool_calls=max_tool_calls,
            deadline=datetime.now(UTC) + timedelta(seconds=deadline_seconds),
        )

    def charge_model(self, tokens: int, usd: float) -> None:
        self.tokens_used += tokens
        self.usd_used += usd

    def charge_tool(self, calls: int = 1) -> None:
        self.tool_calls_used += calls

    @property
    def seconds_remaining(self) -> float:
        return (self.deadline - datetime.now(UTC)).total_seconds()

    @property
    def exhausted_dimension(self) -> str | None:
        if self.tokens_used >= self.max_tokens:
            return "tokens"
        if self.usd_used >= self.max_usd:
            return "usd"
        if self.tool_calls_used >= self.max_tool_calls:
            return "tool_calls"
        if self.seconds_remaining <= 0:
            return "deadline"
        return None

    @property
    def exhausted(self) -> bool:
        return self.exhausted_dimension is not None

    def remaining_tool_calls(self) -> int:
        return max(0, self.max_tool_calls - self.tool_calls_used)

    def require(self) -> None:
        dim = self.exhausted_dimension
        if dim is None:
            return
        used, limit = {
            "tokens": (self.tokens_used, self.max_tokens),
            "usd": (self.usd_used, self.max_usd),
            "tool_calls": (self.tool_calls_used, self.max_tool_calls),
            "deadline": (0.0, 0.0),
        }[dim]
        raise BudgetExceeded(dim, used, limit)

    def snapshot(self) -> dict[str, float]:
        return {
            "tokens_used": self.tokens_used,
            "tokens_limit": self.max_tokens,
            "usd_used": round(self.usd_used, 4),
            "usd_limit": self.max_usd,
            "tool_calls_used": self.tool_calls_used,
            "tool_calls_limit": self.max_tool_calls,
            "seconds_remaining": round(self.seconds_remaining, 1),
        }
