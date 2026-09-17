"""Correctness checks for cross-alert correlation (ADR-0006).

CC-54  Different alerts in one namespace inside the window: the first is the
       leader and is analysed at once (no delay on the first hypothesis); the
       rest are attached, their context collected and stored, not analysed.
CC-55  After the settle period exactly ONE revision call runs over the leader
       and every attached alert; the leader's hypothesis is replaced, the
       previous cause is kept, the engineer gets one message listing all alerts.
CC-56  The revision prompt carries every alert's context and asks for one
       cause; it is stored as the leader's audit context.
CC-57  A storm member (same alertname) is still a storm, never a correlation;
       another namespace is another incident.
CC-58  Redis and in-memory trackers agree; only one replica claims a revision
       per settle period; a failed or over-budget revision changes nothing.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.analyzer import Analyzer
from app.backends import StubBackend
from app.budget import InMemoryBudget
from app.collectors import StubCollector
from app.config import Settings
from app.correlation import InMemoryCorrelationTracker, RedisCorrelationTracker, alert_line
from app.models import Hypothesis, IncidentStatus, StreamAlert
from app.notifiers import format_message, format_slack_blocks
from app.ports import BackendError
from app.stores import InMemoryStore
from app.storm import InMemoryStormTracker


def _alert(name: str, pod: str, ns: str = "payments") -> StreamAlert:
    return StreamAlert(labels={"alertname": name, "namespace": ns, "pod": pod}, fingerprint=f"fp-{name}-{ns}-{pod}")


class RecordingBackend(StubBackend):
    """Counts calls and keeps the prompts; can be told to fail."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self.fail = False

    async def analyze(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise BackendError("HTTP 503 from provider")
        result = await super().analyze(prompt)
        if "ALERTS FIRING TOGETHER" in prompt:
            result.hypothesis = Hypothesis(
                root_cause="postgres is down; every alert in payments follows from it",
                confidence="high", evidence=["all three alerts started within 40 s"],
                disproof="check postgres pod status", next_steps=["restore postgres"], blast_radius="service",
            )
        return result


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent = []

    async def notify(self, incident):
        self.sent.append(incident)


def _analyzer(tracker=None, budget=1.0, settle=0.0):
    backend, notifier, store = RecordingBackend(), RecordingNotifier(), InMemoryStore()
    a = Analyzer(
        collector=StubCollector(), backend=backend, notifier=notifier, store=store,
        budget=InMemoryBudget(budget), storm_tracker=InMemoryStormTracker(120),
        correlation_tracker=tracker or InMemoryCorrelationTracker(180),
        correlation_settle_seconds=settle, correlation_window_seconds=180,
    )
    return a, backend, notifier, store


@pytest.mark.parametrize("make_tracker", [
    lambda: InMemoryCorrelationTracker(180),
    lambda: RedisCorrelationTracker(fakeredis.aioredis.FakeRedis(decode_responses=True), 180),
])
async def test_three_alerts_one_namespace_one_revised_hypothesis(make_tracker):  # CC-54, CC-55, CC-56, CC-58
    a, backend, notifier, store = _analyzer(make_tracker())

    leader = await a.analyze(_alert("KubePodCrashLooping", "billing-api-1"))
    assert leader.status is IncidentStatus.ANALYZED and backend.prompts and "ALERT" in backend.prompts[0]
    first_cause = leader.hypothesis.root_cause

    m1 = await a.analyze(_alert("KubeDeploymentReplicasMismatch", "billing-api"))
    m2 = await a.analyze(_alert("HighErrorRate", "checkout-7"))
    for m in (m1, m2):
        assert m.status is IncidentStatus.CORRELATED and m.grouped_into == leader.id
        assert m.context  # collected and redacted, on record for the revision
    assert m2.correlated_alerts == ["KubeDeploymentReplicasMismatch pod=billing-api", "HighErrorRate pod=checkout-7"]
    assert len(backend.prompts) == 1  # members cost no model call of their own

    await a.drain()  # settle=0: revisions run now

    assert len(backend.prompts) == 2  # exactly one revision for two members
    revision = backend.prompts[1]
    assert "ALERTS FIRING TOGETHER in namespace payments" in revision
    assert "1. KubePodCrashLooping" in revision and "3. HighErrorRate" in revision
    assert revision.count("CONTEXT for alert") == 3

    revised = next(i for i in store.saved if i.id == leader.id)
    assert revised.hypothesis.root_cause.startswith("postgres is down")
    assert revised.revised_from == first_cause
    assert revised.correlated_alerts == [alert_line(_alert("KubeDeploymentReplicasMismatch", "billing-api")), "HighErrorRate pod=checkout-7"]
    assert revised.context == revision  # audit trail of the revision
    assert len(store.saved) == 3  # leader (revised in place) + two members, no fourth row

    # the engineer hears: leader, two short attach notices, one revised hypothesis
    statuses = [i.status for i in notifier.sent]
    assert statuses == [IncidentStatus.ANALYZED, IncidentStatus.CORRELATED, IncidentStatus.CORRELATED, IncidentStatus.ANALYZED]
    text = format_message(notifier.sent[-1])
    assert "Correlated" in text and "3 alerts" in text and "Revised from" in text and "postgres is down" in text
    assert "HighErrorRate pod=checkout-7" in text
    attach = format_message(notifier.sent[1])
    assert f"Attached to #{leader.id[:8]}" in attach
    assert "Correlated" in str(format_slack_blocks(notifier.sent[-1]))


async def test_storm_stays_a_storm_and_other_namespace_is_separate():  # CC-57
    a, backend, _, _store = _analyzer()
    await a.analyze(_alert("KubePodCrashLooping", "web-0"))
    same = await a.analyze(_alert("KubePodCrashLooping", "web-1"))  # same alertname: storm member
    other_ns = await a.analyze(_alert("HighErrorRate", "api-0", ns="checkout"))  # other namespace: its own leader
    assert same.status is IncidentStatus.GROUPED
    assert other_ns.status is IncidentStatus.ANALYZED
    await a.drain()
    assert len(backend.prompts) == 2  # two leaders, no revision (no correlated members)


async def test_revision_is_claimed_once_and_survives_failures():  # CC-58
    rds = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t1, t2 = RedisCorrelationTracker(rds, 180), RedisCorrelationTracker(rds, 180)
    a1, b1, _n1, _ = _analyzer(t1)
    a2, b2, n2, _ = _analyzer(t2)
    await a1.analyze(_alert("KubePodCrashLooping", "billing-api-1"))
    await a2.analyze(_alert("HighErrorRate", "checkout-7"))  # member lands on another replica
    await a2.drain()
    await a1.drain()
    assert len(b2.prompts) == 1 and b1.prompts and len(b1.prompts) == 1  # replica 2 revised, replica 1 did not
    assert n2.sent[-1].status is IncidentStatus.ANALYZED and n2.sent[-1].correlated_alerts == ["HighErrorRate pod=checkout-7"]
    # revised_from is empty here only because each replica has its own in-memory store;
    # the Postgres store answers root_cause(id) for any replica (test_stores)
    # a second claim inside the hold period is refused
    assert await t1.claim_revision(_alert("X", "p"), 30) is False

    # provider failure: the leader keeps its hypothesis, nothing is sent
    a, backend, notifier, store = _analyzer()
    leader = await a.analyze(_alert("KubePodCrashLooping", "billing-api-1"))
    backend.fail = True
    await a.analyze(_alert("HighErrorRate", "checkout-7"))
    await a.drain()
    kept = next(i for i in store.saved if i.id == leader.id)
    assert kept.hypothesis == leader.hypothesis and not kept.revised_from
    assert notifier.sent[-1].status is IncidentStatus.CORRELATED

    # no budget: no revision call
    a, backend, _, _ = _analyzer(budget=0.0)
    assert (await a.analyze(_alert("KubePodCrashLooping", "p"))).status is IncidentStatus.BUDGET_EXCEEDED
    await a.analyze(_alert("HighErrorRate", "q"))
    await a.drain()
    assert backend.prompts == []


def test_settings_have_correlation_knobs():
    s = Settings()
    assert s.correlation_window_seconds == 180 and s.correlation_settle_seconds == 20.0
