"""Budget circuit breaker (SPEC §5.4): tokens, dollars, steps, wall-clock, plus the global cap.

Per-run limits are evaluated against the run's projection (harness/store.py), so they count every
model turn, including replayed ones: a replay stops exactly where the recording stopped. The
global dollar cap counts only turns that were actually billed by the provider, across all runs.

`check_before_call` runs before every model request (harness/llm.py); `check_after_turn` runs
after a turn has been recorded, so the spend that tripped the breaker is always in the log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from harness.events import RunLimitsRecord

if TYPE_CHECKING:
    from harness.store import RunState

LimitName = Literal["global_usd", "run_usd", "run_tokens", "steps", "wall_clock"]


class BudgetExceeded(Exception):
    def __init__(self, limit: LimitName, detail: str) -> None:
        super().__init__(f"{limit}: {detail}")
        self.limit = limit
        self.detail = detail


@dataclass(frozen=True)
class RunLimits:
    max_usd: float
    max_tokens: int
    max_steps: int
    max_wall_s: float

    def to_record(self) -> RunLimitsRecord:
        return RunLimitsRecord(
            max_usd=self.max_usd,
            max_tokens=self.max_tokens,
            max_steps=self.max_steps,
            max_wall_s=self.max_wall_s,
        )

    @classmethod
    def from_record(cls, rec: RunLimitsRecord) -> RunLimits:
        return cls(rec.max_usd, rec.max_tokens, rec.max_steps, rec.max_wall_s)


class Budget:
    def __init__(
        self,
        limits: RunLimits,
        total_usd_cap: float,
        global_spent_before_run: float,
    ) -> None:
        """`global_spent_before_run` is billed spend of all runs, excluding this run's own turns."""
        self.limits = limits
        self.total_usd_cap = total_usd_cap
        self._global_other = global_spent_before_run

    def global_spent(self, state: RunState) -> float:
        return self._global_other + state.billed_usd

    def _check_money(self, state: RunState) -> None:
        if self.global_spent(state) >= self.total_usd_cap:
            raise BudgetExceeded(
                "global_usd",
                f"spent ${self.global_spent(state):.4f} of ${self.total_usd_cap:.2f} total cap",
            )
        if state.cost_usd >= self.limits.max_usd:
            raise BudgetExceeded(
                "run_usd", f"run cost ${state.cost_usd:.4f} >= ${self.limits.max_usd:.4f}"
            )

    def _check_tokens_and_time(self, state: RunState, now: datetime) -> None:
        if state.total_tokens >= self.limits.max_tokens:
            raise BudgetExceeded(
                "run_tokens", f"{state.total_tokens} tokens >= {self.limits.max_tokens}"
            )
        active = state.active_seconds(now)
        if active >= self.limits.max_wall_s:
            raise BudgetExceeded(
                "wall_clock", f"{active:.1f}s active >= {self.limits.max_wall_s:.1f}s"
            )

    def check_before_call(self, state: RunState, now: datetime | None = None) -> None:
        """Refuse to start a model request that the budget no longer allows."""
        self._check_money(state)
        self._check_tokens_and_time(state, now or datetime.now(UTC))
        if state.steps >= self.limits.max_steps:
            raise BudgetExceeded("steps", f"{state.steps} model turns >= {self.limits.max_steps}")

    def check_after_turn(self, state: RunState, now: datetime | None = None) -> None:
        """Stop after a turn whose cost pushed the run over a limit (it is already recorded)."""
        self._check_money(state)
        self._check_tokens_and_time(state, now or datetime.now(UTC))
