"""Per-day spend cap (ADR-0003).

When the day's spend reaches the cap, the pipeline stops making LLM calls and
degrades to raw delivery — so a runaway loop or an alert storm can never produce
a runaway bill. This is a soft cap: concurrent workers may overshoot by at most
one in-flight call each, which is acceptable for a cost guardrail.

The key is dated (UTC) and expires on its own, so the budget resets each day
with no scheduler.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import Settings, settings


def _today_key(prefix: str) -> str:
    return f"{prefix}:{datetime.now(timezone.utc).date().isoformat()}"


class InMemoryBudget:
    """Process-local budget — used in tests and single-replica runs."""

    def __init__(self, cap_usd: float) -> None:
        self._cap = cap_usd
        self._spent: dict[str, float] = {}

    async def has_budget(self) -> bool:
        return self._spent.get(_today_key("b"), 0.0) < self._cap

    async def add(self, usd: float) -> None:
        key = _today_key("b")
        self._spent[key] = self._spent.get(key, 0.0) + usd


class RedisBudget:
    """Shared budget across worker replicas, backed by a dated Redis counter."""

    def __init__(self, redis, cap_usd: float, prefix: str = "minus20:budget") -> None:
        self._redis = redis
        self._cap = cap_usd
        self._prefix = prefix

    async def has_budget(self) -> bool:
        spent = await self._redis.get(_today_key(self._prefix))
        return float(spent or 0.0) < self._cap

    async def add(self, usd: float) -> None:
        key = _today_key(self._prefix)
        await self._redis.incrbyfloat(key, usd)
        # Keep the key for two days so a UTC-midnight boundary is never lost.
        await self._redis.expire(key, 172_800)


def get_budget(redis=None, cfg: Settings = settings):
    if redis is not None:
        return RedisBudget(redis, cfg.daily_budget_usd)
    return InMemoryBudget(cfg.daily_budget_usd)
