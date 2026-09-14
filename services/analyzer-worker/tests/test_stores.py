"""Correctness checks for incident persistence (ADR-0002).

CC-29 The Postgres store creates its schema once, then inserts the incident
      fields (redacted data only, since the Analyzer redacts before building it).
CC-30 The store is selected by config, not code.
"""

from app.config import Settings
from app.models import Hypothesis, Incident, IncidentStatus
from app.stores import InMemoryStore, PostgresStore, get_store


class FakeConn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


class FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    def acquire(self):
        return FakeAcquire(self.conn)


def _incident():
    return Incident(
        fingerprint="fp1",
        alertname="KubePodCrashLooping",
        namespace="demo",
        severity="warning",
        status=IncidentStatus.ANALYZED,
        hypothesis=Hypothesis(
            root_cause="OOMKilled",
            confidence="high",
            blast_radius="single-pod",
            evidence=["OOMKilling event x3"],
            disproof="check memory below limit at alert time",
            next_steps=["raise the limit"],
        ),
        backend="stub",
        cost_usd=0.006,
        latency_ms=42,
    )


async def test_postgres_store_creates_schema_then_inserts():  # CC-29
    pool = FakePool()
    store = PostgresStore("postgresql://x", pool=pool)

    await store.save(_incident())

    sqls = [c[0] for c in pool.conn.calls]
    assert any("CREATE TABLE IF NOT EXISTS incidents" in s for s in sqls)
    insert = next(c for c in pool.conn.calls if "INSERT INTO incidents" in c[0])
    args = insert[1]
    assert "KubePodCrashLooping" in args  # alertname persisted
    assert "OOMKilled" in args  # hypothesis root_cause persisted
    assert 0.006 in args  # cost recorded
    # the useful half of a hypothesis must survive too, or the incident
    # history and the UI lose the evidence and the disproof
    assert ["OOMKilling event x3"] in args
    assert "check memory below limit at alert time" in args
    assert ["raise the limit"] in args
    assert len(args) == 20  # …the redacted context, alert→hypothesis time, and the alert summary


async def test_postgres_store_creates_schema_only_once():
    pool = FakePool()
    store = PostgresStore("postgresql://x", pool=pool)
    await store.save(_incident())
    await store.save(_incident())
    creates = [c for c in pool.conn.calls if "CREATE TABLE" in c[0]]
    assert len(creates) == 1  # schema ensured once, not per save
    alters = [c for c in pool.conn.calls if "ADD COLUMN IF NOT EXISTS" in c[0]]
    assert alters, "column migrations must run so an existing table gains them"


async def test_failed_incident_persists_without_hypothesis():
    pool = FakePool()
    store = PostgresStore("postgresql://x", pool=pool)
    inc = Incident(alertname="X", status=IncidentStatus.ANALYSIS_FAILED, failure_reason="timeout")
    await store.save(inc)
    insert = next(c for c in pool.conn.calls if "INSERT INTO incidents" in c[0])
    assert "timeout" in insert[1]  # failure_reason persisted, no hypothesis needed


def test_get_store_selects_by_config():  # CC-30
    assert isinstance(get_store(Settings(store="postgres")), PostgresStore)
    assert isinstance(get_store(Settings(store="memory")), InMemoryStore)
