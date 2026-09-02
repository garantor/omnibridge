"""
Request-level budget for the reasoning core.

Two levels matter and they are easy to conflate. A per-invocation cap bounds one model
call; the retry edge can invoke the agent several times for a single user request, so a
ceiling has to sit above both.

Note what actually accrues: tokens, wall-clock, and RPC calls. Not gas — eth_call and
estimate_gas are read-only and spend nothing on chain.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class BudgetExceeded(RuntimeError):
    """A request hit its ceiling. Raised rather than silently truncating the work."""


@dataclass
class RequestBudget:
    """Accounting for one user request, across every agent attempt it triggers."""

    max_attempts: int = 3
    max_model_calls: int = 12
    max_input_tokens: int = 400_000
    max_output_tokens: int = 60_000
    max_wall_seconds: float = 180.0

    attempts: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def start_attempt(self) -> None:
        self.attempts += 1
        if self.attempts > self.max_attempts:
            raise BudgetExceeded(
                f"Retry budget exhausted after {self.max_attempts} attempts."
            )

    def record_usage(self, usage: Any) -> None:
        """Accumulate one model response's usage. Tolerates a missing field."""
        self.model_calls += 1
        for attr, target in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_input_tokens", "cache_read_tokens"),
        ):
            value = getattr(usage, attr, None) if usage is not None else None
            if isinstance(value, int):
                setattr(self, target, getattr(self, target) + value)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def check(self) -> None:
        """Raise if any ceiling has been passed. Called before each model call."""
        if self.model_calls >= self.max_model_calls:
            raise BudgetExceeded(
                f"Model call ceiling reached ({self.max_model_calls} calls)."
            )
        if self.input_tokens >= self.max_input_tokens:
            raise BudgetExceeded(
                f"Input token ceiling reached ({self.input_tokens:,} tokens)."
            )
        if self.output_tokens >= self.max_output_tokens:
            raise BudgetExceeded(
                f"Output token ceiling reached ({self.output_tokens:,} tokens)."
            )
        if self.elapsed_seconds >= self.max_wall_seconds:
            raise BudgetExceeded(
                f"Wall-clock ceiling reached ({self.elapsed_seconds:.0f}s)."
            )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "attempts": self.attempts,
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "limits": {
                "max_attempts": self.max_attempts,
                "max_model_calls": self.max_model_calls,
                "max_input_tokens": self.max_input_tokens,
                "max_output_tokens": self.max_output_tokens,
                "max_wall_seconds": self.max_wall_seconds,
            },
        }
