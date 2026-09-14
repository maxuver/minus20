"""Correctness checks for alert-storm grouping.

CC-43  Different pods, same alertname+namespace, inside the window: the first
       is the leader and is analysed; the rest are grouped, never analysed.
CC-44  Exactly one model call per storm, however many alerts arrive.
CC-45  The engineer is told about the storm at thresholds (2, 5, 10, ...),
       each message naming the leader; not on every member.
CC-46  A different namespace or alertname is a different storm.
CC-47  Redis and in-memory trackers agree, and the Redis one elects exactly
       one leader across replicas (HSETNX).
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.analyzer import Analyzer
from app.backends import StubBackend
from app.budget import InMemoryBudget
from app.collectors import StubCollector
from app.config import Settings
from app.models import IncidentStatus, StreamAlert
from app.notifiers import format_message, format_slack_blocks
from app.stores import InMemoryStore
from app.storm import NOTIFY_AT, InMemoryStormTracker, RedisStormTracker, storm_key


def _alert(pod: str, ns: str = "payments", name: str = "KubePodCrashLooping") -> StreamAlert:
    return StreamAlert(labels={"alertname": name, "namespace": ns, "pod": pod}, fingerprint=f"fp-{name}-{ns}-{pod}")


class CountingBackend(StubBackend):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def analyze(self, prompt):
        self.calls += 1
        return await super().analyze(prompt)


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent = []

    async def notify(self, incident):
        self.sent.append(incident)


def _analyzer(tracker):
    backend, notifier, store = CountingBackend(), RecordingNotifier(), InMemoryStore()
    a = Analyzer(
        collector=StubCollector(), backend=backend, notifier=notifier, store=store,
        budget=InMemoryBudget(1.0), storm_tracker=tracker,
    )
    return a, backend, notifier, store


@pytest.mark.parametrize("make_tracker", [lambda: InMemoryStormTracker(120), lambda: RedisStormTracker(fakeredis.aioredis.FakeRedis(decode_responses=True), 120)])
async def test_storm_of_thirty_pods_costs_one_model_call(make_tracker):  # CC-43, CC-44, CC-45
    a, backend, notifier, store = _analyzer(make_tracker())
    incidents = [await a.analyze(_alert(f"web-{i}")) for i in range(30)]

    assert incidents[0].status is IncidentStatus.ANALYZED
    assert all(i.status is IncidentStatus.GROUPED for i in incidents[1:])
    assert backend.calls == 1
    assert all(i.grouped_into == incidents[0].id for i in incidents[1:])
    assert incidents[-1].storm_size == 30
    # every alert is recorded ...
    assert len(store.saved) == 30
    # ... but the engineer hears: the leader, then the thresholds only
    sizes = [i.storm_size for i in notifier.sent if i.status is IncidentStatus.GROUPED]
    assert sizes == [n for n in NOTIFY_AT if n <= 30]
    assert notifier.sent[0].status is IncidentStatus.ANALYZED


async def test_other_namespace_or_alertname_is_another_storm():  # CC-46
    a, backend, _, _ = _analyzer(InMemoryStormTracker(120))
    await a.analyze(_alert("a", ns="payments"))
    await a.analyze(_alert("b", ns="checkout"))
    await a.analyze(_alert("c", ns="payments", name="KubePodNotReady"))
    assert backend.calls == 3
    assert storm_key(_alert("x", ns="payments")) != storm_key(_alert("x", ns="checkout"))


async def test_storm_window_expires(monkeypatch):
    tracker = InMemoryStormTracker(120)
    a, backend, _, _ = _analyzer(tracker)
    await a.analyze(_alert("a"))
    import time as _time

    import app.storm as storm_mod

    real = _time.monotonic()
    monkeypatch.setattr(storm_mod.time, "monotonic", lambda: real + 1000.0)  # far past the window
    await a.analyze(_alert("b"))
    assert backend.calls == 2  # a new storm, a new leader


async def test_redis_tracker_elects_one_leader_across_replicas():  # CC-47
    rds = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t1, t2 = RedisStormTracker(rds, 120), RedisStormTracker(rds, 120)
    s1 = await t1.track(_alert("a"), "leader-1")
    s2 = await t2.track(_alert("b"), "would-be-2")
    assert s1.leader and not s2.leader
    assert s2.leader_id == "leader-1" and s2.count == 2 and s2.pods == ["a", "b"]
    assert await rds.ttl(storm_key(_alert("a"))) > 0  # the storm expires


def test_storm_messages_name_the_leader_and_the_pods():
    from app.models import Incident

    inc = Incident(alertname="KubePodCrashLooping", namespace="payments", status=IncidentStatus.GROUPED,
                   grouped_into="abcdef1234567890", storm_size=10, storm_pods=[f"web-{i}" for i in range(10)])
    text = format_message(inc)
    assert "Alert storm" in text and "10 pods" in text and "#abcdef12" in text
    assert "web-0" in text and "+2 more" in text
    blocks = format_slack_blocks(inc)
    joined = str(blocks)
    assert "Alert storm" in joined and "#abcdef12" in joined


def test_settings_have_a_storm_window():
    assert Settings().storm_window_seconds == 120
