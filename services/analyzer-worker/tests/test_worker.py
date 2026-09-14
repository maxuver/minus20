"""Correctness checks for the consumer loop (ADR-0003 queue isolation).

CC-12 The consumer group is created idempotently (BUSYGROUP tolerated).
CC-13 A successfully processed message is acknowledged (leaves the pending list).
CC-14 A message that keeps failing is dead-lettered after max attempts, not
      redelivered forever.
CC-15 An unparseable stream payload is dead-lettered, never crashes the loop.
"""

import json

import fakeredis.aioredis
import pytest

from app.config import Settings
from app.worker import Worker


@pytest.fixture
def cfg():
    return Settings(max_delivery_attempts=2, block_ms=50)


@pytest.fixture
def rds():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


class StubAnalyzer:
    def __init__(self, raises=False):
        self.raises = raises
        self.calls = 0
        self.skip_dedup_seen: list[bool] = []

    async def analyze(self, alert, *, skip_dedup=False):
        self.calls += 1
        self.skip_dedup_seen.append(skip_dedup)
        if self.raises:
            raise RuntimeError("processing blew up")


async def _pending_count(rds, cfg):
    info = await rds.xpending(cfg.alerts_stream, cfg.consumer_group)
    return info["pending"]


async def test_ensure_group_is_idempotent(rds, cfg):  # CC-12
    worker = Worker(rds, StubAnalyzer(), cfg)
    await worker.ensure_group()
    await worker.ensure_group()  # must not raise on BUSYGROUP
    groups = await rds.xinfo_groups(cfg.alerts_stream)
    assert any(g["name"] == cfg.consumer_group for g in groups)


async def test_successful_message_is_acked(rds, cfg, raw_payload):  # CC-13
    analyzer = StubAnalyzer()
    worker = Worker(rds, analyzer, cfg)
    await worker.ensure_group()
    await rds.xadd(cfg.alerts_stream, {"payload": json.dumps(raw_payload)})

    handled = await worker.run_once()

    assert handled == 1
    assert analyzer.calls == 1
    assert await _pending_count(rds, cfg) == 0  # acknowledged


async def test_unparseable_payload_is_dead_lettered(rds, cfg):  # CC-15
    analyzer = StubAnalyzer()
    worker = Worker(rds, analyzer, cfg)
    await worker.ensure_group()
    await rds.xadd(cfg.alerts_stream, {"payload": "{{{ not json"})

    status = await worker.process("0-0", {"payload": "{{{ not json"})

    assert status == "dead:parse"
    assert analyzer.calls == 0
    dead = await rds.xlen(cfg.dead_letter_stream)
    assert dead == 1


async def test_failing_message_retries_then_dead_letters(rds, cfg, raw_payload):  # CC-14
    analyzer = StubAnalyzer(raises=True)
    worker = Worker(rds, analyzer, cfg)
    await worker.ensure_group()
    await rds.xadd(cfg.alerts_stream, {"payload": json.dumps(raw_payload)})

    # Drain the stream: initial delivery + one app-level retry, then dead-letter.
    for _ in range(10):
        await worker.run_once()

    assert analyzer.calls == cfg.max_delivery_attempts  # 1 initial + 1 retry
    assert await rds.xlen(cfg.dead_letter_stream) == 1
    assert await _pending_count(rds, cfg) == 0  # nothing stuck pending


async def test_process_status_transitions(rds, cfg, raw_payload):
    """The retry branch increments attempts and re-enqueues; the last attempt dies."""
    worker = Worker(rds, StubAnalyzer(raises=True), cfg)
    await worker.ensure_group()
    payload = json.dumps(raw_payload)

    first = await worker.process("1-0", {"payload": payload})
    assert first == "retried"

    last = await worker.process("2-0", {"payload": payload, "_attempts": "1"})
    assert last == "dead:attempts"


async def test_message_left_pending_by_a_dead_consumer_is_reclaimed(rds, raw_payload):
    """The 2026-09-13 bug: an alert read by a pod killed mid-analysis stayed
    pending forever, because '>' only returns new entries."""
    cfg = Settings(block_ms=50, reclaim_idle_ms=0)  # idle threshold 0 so the test needs no sleep
    # A previous consumer reads the message and dies before acknowledging.
    dead = Worker(rds, StubAnalyzer(), Settings(consumer_name="analyzer-dead", block_ms=50))
    await dead.ensure_group()
    await rds.xadd(cfg.alerts_stream, {"payload": json.dumps(raw_payload)})
    await rds.xreadgroup(cfg.consumer_group, "analyzer-dead", {cfg.alerts_stream: ">"}, count=1)
    assert await _pending_count(rds, cfg) == 1

    # The replacement pod: '>' alone would never see it.
    analyzer = StubAnalyzer()
    worker = Worker(rds, analyzer, cfg)
    handled = await worker.run_once()

    assert handled == 1
    assert analyzer.calls == 1
    assert analyzer.skip_dedup_seen == [True]  # the dead consumer already set the dedup key
    assert await _pending_count(rds, cfg) == 0  # processed and acknowledged


async def test_reclaim_respects_the_idle_threshold(rds, raw_payload):
    """A slow-but-alive consumer keeps its message."""
    cfg = Settings(block_ms=50, reclaim_idle_ms=60_000)
    other = Worker(rds, StubAnalyzer(), Settings(consumer_name="analyzer-slow", block_ms=50))
    await other.ensure_group()
    await rds.xadd(cfg.alerts_stream, {"payload": json.dumps(raw_payload)})
    await rds.xreadgroup(cfg.consumer_group, "analyzer-slow", {cfg.alerts_stream: ">"}, count=1)

    analyzer = StubAnalyzer()
    assert await Worker(rds, analyzer, cfg).reclaim() == 0
    assert analyzer.calls == 0
    assert await _pending_count(rds, cfg) == 1  # still theirs


async def test_stop_finishes_the_message_in_hand_then_reads_no_more(rds, raw_payload):
    cfg = Settings(block_ms=50)
    analyzer = StubAnalyzer()
    worker = Worker(rds, analyzer, cfg)
    await worker.ensure_group()
    await rds.xadd(cfg.alerts_stream, {"payload": json.dumps(raw_payload)})
    await rds.xadd(cfg.alerts_stream, {"payload": json.dumps(raw_payload)})

    worker.stop()  # SIGTERM arrived
    handled = await worker.run_once()  # the iteration already in flight completes
    assert handled == 2 and analyzer.calls == 2
    assert await _pending_count(rds, cfg) == 0
    assert worker._stopping  # run() would not iterate again


async def test_dead_consumers_are_pruned_but_live_and_pending_ones_kept(rds, raw_payload):
    cfg = Settings(block_ms=50, reclaim_idle_ms=0, consumer_name="me")
    me = Worker(rds, StubAnalyzer(), cfg)
    await me.ensure_group()
    # a dead consumer with nothing pending, and one that still holds a message
    await rds.xadd(cfg.alerts_stream, {"payload": json.dumps(raw_payload)})
    await rds.xreadgroup(cfg.consumer_group, "ghost", {cfg.alerts_stream: ">"}, count=1)
    entries = await rds.xpending_range(cfg.alerts_stream, cfg.consumer_group, "-", "+", 10)
    await rds.xack(cfg.alerts_stream, cfg.consumer_group, entries[0]["message_id"])  # ghost finished, then died
    await rds.xadd(cfg.alerts_stream, {"payload": json.dumps(raw_payload)})
    await rds.xreadgroup(cfg.consumer_group, "busy", {cfg.alerts_stream: ">"}, count=1)  # busy holds one

    pruned = await me.prune_consumers()

    names = {c["name"] for c in await rds.xinfo_consumers(cfg.alerts_stream, cfg.consumer_group)}
    assert pruned == 1
    assert "ghost" not in names and "busy" in names
