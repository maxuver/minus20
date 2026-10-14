"""Correctness checks for the incident-history UI.

CC-43 The page renders the whole hypothesis: cause, evidence, disproof, blast
      radius and next steps — the parts that make a hypothesis checkable.
CC-44 Filters are passed to the query as bound parameters, so a namespace from
      the query string cannot alter the SQL.
CC-45 A degraded incident still renders, showing why analysis did not run.
CC-46 An empty history renders a page rather than an error.
CC-47 On a fresh install the incidents table does not exist yet. The page
      still renders and readiness still passes, because the worker creates
      that table only when it stores its first incident.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import IncidentReader
from app.main import app

ROW = {
    "id": "abc123",
    "alertname": "KubePodCrashLooping",
    "namespace": "demo",
    "severity": "critical",
    "status": "analyzed",
    "root_cause": "Container OOMKilled: memory limit reached",
    "confidence": "high",
    "blast_radius": "single-pod",
    "evidence": ["OOMKilling event x3", "memory at limit 512Mi"],
    "disproof": "Check if memory stayed below the limit before the alert",
    "next_steps": ["Raise the memory limit", "Profile for a leak"],
    "backend": "ollama",
    "cost_usd": 0.0,
    "latency_ms": 1420,
    "failure_reason": None,
    "created_at": datetime(2026, 9, 2, 14, 30, tzinfo=UTC),
}

FAILED_ROW = {
    **ROW,
    "id": "def456",
    "status": "analysis_failed",
    "root_cause": None,
    "evidence": None,
    "disproof": None,
    "next_steps": None,
    "failure_reason": "timeout",
}


class UndefinedTableError(Exception):
    """Mirrors asyncpg's error for a table that does not exist yet."""


class FakeConn:
    def __init__(self, rows, missing_table=False):
        self.rows = rows
        self.calls = []
        self.missing_table = missing_table

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.missing_table and "incidents" in sql:
            raise UndefinedTableError("relation \"incidents\" does not exist")
        if "DISTINCT namespace" in sql:
            return [{"namespace": "demo"}]
        if "GROUP BY status" in sql:
            return [{"status": "analyzed", "n": len(self.rows), "cost": 0.0}]
        return self.rows


class FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, rows, missing_table=False):
        self.conn = FakeConn(rows, missing_table)

    def acquire(self):
        return FakeAcquire(self.conn)


def _client(rows, missing_table=False):
    pool = FakePool(rows, missing_table)
    client = TestClient(app)
    with client:
        client.app.state.reader = IncidentReader(Settings(), pool=pool)
        yield client, pool


def test_page_renders_the_whole_hypothesis():  # CC-43
    for client, _pool in _client([ROW]):
        body = client.get("/").text
        assert "KubePodCrashLooping" in body
        assert "Container OOMKilled" in body
        assert "single-pod" in body
        assert "OOMKilling event x3" in body
        assert "Check if memory stayed below the limit" in body
        assert "Raise the memory limit" in body
        assert "ollama" in body and "1420" in body


def test_filters_are_bound_parameters_not_interpolated():  # CC-44
    for client, pool in _client([ROW]):
        resp = client.get("/", params={"namespace": "'; DROP TABLE incidents; --"})
        assert resp.status_code == 200
        list_call = next(c for c in pool.conn.calls if "FROM incidents" in c[0] and "LIMIT" in c[0])
        sql, args = list_call
        # the hostile value travelled as a parameter, never into the statement
        assert "DROP TABLE" not in sql
        assert "'; DROP TABLE incidents; --" in args


def test_degraded_incident_still_renders():  # CC-45
    for client, _pool in _client([FAILED_ROW]):
        body = client.get("/").text
        assert "analysis_failed" in body
        assert "timeout" in body


def test_empty_history_renders():  # CC-46
    for client, _pool in _client([]):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "No incidents yet" in resp.text


def test_healthz():
    for client, _pool in _client([]):
        assert client.get("/healthz").json() == {"status": "ok"}


def test_fresh_install_without_the_table_still_serves():  # CC-47
    for client, _pool in _client([], missing_table=True):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "No incidents yet" in resp.text
        # readiness must not depend on a table the worker has not created yet
        assert client.get("/readyz").json() == {"status": "ready"}
