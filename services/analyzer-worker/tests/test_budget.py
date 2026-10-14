"""Correctness checks for the daily budget cap (ADR-0003).

CC-4  (see test_analyzer) the pipeline skips the LLM when budget is gone.
CC-5  The counter is dated (UTC), so it resets each day on its own.
"""

from datetime import UTC, datetime

import fakeredis.aioredis

from app.budget import InMemoryBudget, RedisBudget, get_budget
from app.config import Settings


async def test_inmemory_budget_flips_at_cap():
    budget = InMemoryBudget(cap_usd=0.006)
    assert await budget.has_budget() is True
    await budget.add(0.006)
    assert await budget.has_budget() is False


async def test_inmemory_budget_is_dated():  # CC-5
    budget = InMemoryBudget(cap_usd=1.0)
    await budget.add(0.5)
    today = datetime.now(UTC).date().isoformat()
    assert any(today in key for key in budget._spent)


async def test_redis_budget_shared_counter():
    rds = fakeredis.aioredis.FakeRedis(decode_responses=True)
    budget = RedisBudget(rds, cap_usd=0.01)
    assert await budget.has_budget() is True
    await budget.add(0.006)
    await budget.add(0.006)  # total 0.012 > cap
    assert await budget.has_budget() is False


def test_factory_picks_redis_when_available():
    rds = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert isinstance(get_budget(rds, Settings()), RedisBudget)
    assert isinstance(get_budget(None, Settings()), InMemoryBudget)
