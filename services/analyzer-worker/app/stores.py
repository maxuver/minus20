"""Incident persistence (ADR-0002: redacted-only, retention-bounded).

Two adapters behind the same `save()` port:

- InMemoryStore: for tests and the offline demo.
- PostgresStore: the incident-history dataset. Only post-redaction data ever
  reaches it, because the Analyzer redacts before it builds the incident.

Selecting one is configuration (MINUS20_STORE), never code.
"""

from __future__ import annotations

from .config import Settings, settings
from .models import Incident

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id             TEXT PRIMARY KEY,
    fingerprint    TEXT,
    alertname      TEXT,
    namespace      TEXT,
    severity       TEXT,
    status         TEXT,
    root_cause     TEXT,
    confidence     TEXT,
    blast_radius   TEXT,
    evidence       TEXT[],
    disproof       TEXT,
    next_steps     TEXT[],
    backend        TEXT,
    cost_usd       DOUBLE PRECISION,
    latency_ms     INTEGER,
    failure_reason TEXT,
    created_at     TIMESTAMPTZ
)
"""

# Added after the first release, so an existing table gets them too rather than
# silently dropping the most useful part of a hypothesis.
_MIGRATIONS = (
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS evidence TEXT[]",
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS disproof TEXT",
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS next_steps TEXT[]",
    # Engineer feedback (ADR-0005). Written only by a human through the agent;
    # the worker never touches these columns but owns the schema so both
    # processes agree on it whichever starts first.
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS verdict TEXT",
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS resolution TEXT",
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ",
    # Audit trail: the redacted context the model was shown (ADR-0002 keeps
    # it redacted; a regulated team needs to see what the model saw).
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS context TEXT",
    # Alert fired → hypothesis ready, end to end; the business number.
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS time_to_hypothesis_ms INTEGER",
    # The one-line alert summary, so an incident can be replayed as a scenario.
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS alert_summary TEXT",
    # Storm members point at their leader; the size is the count at write time.
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS grouped_into TEXT",
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS storm_size INTEGER",
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS correlated_alerts TEXT[]",
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS revised_from TEXT",
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS incidents_created_at_idx ON incidents (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS incidents_namespace_idx ON incidents (namespace)",
)

_INSERT = """
INSERT INTO incidents (
    id, fingerprint, alertname, namespace, severity, status,
    root_cause, confidence, blast_radius, evidence, disproof, next_steps,
    backend, cost_usd, latency_ms, failure_reason, created_at, context,
    time_to_hypothesis_ms, alert_summary, grouped_into, storm_size, correlated_alerts
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
ON CONFLICT (id) DO NOTHING
"""

# The revision of a correlation leader (ADR-0006) is the one write that
# changes a hypothesis after the fact; the previous cause is kept.
_REVISE = """
UPDATE incidents SET
    root_cause = $2, confidence = $3, blast_radius = $4, evidence = $5, disproof = $6,
    next_steps = $7, backend = $8, cost_usd = cost_usd + $9, latency_ms = $10,
    correlated_alerts = $11, revised_from = coalesce(revised_from, $12), context = $13,
    status = 'analyzed', failure_reason = NULL
WHERE id = $1
"""


async def connect_pool(dsn: str, attempts: int = 30, delay: float = 3.0):  # pragma: no cover - real DB path
    """Create the asyncpg pool, waiting for the database to accept connections.

    On a fresh cluster Postgres comes up after the worker (and on EKS its
    volume can take minutes). Crash-looping until it is there works, but
    burns restarts and backoff; waiting up to ~90 s is the same outcome
    without the noise.
    """
    import asyncio
    import logging

    import asyncpg

    log = logging.getLogger("analyzer-worker.stores")
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await asyncpg.create_pool(dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            last = exc
            if i == 0:
                log.warning("postgres not ready (%s); waiting", type(exc).__name__)
            await asyncio.sleep(delay)
    raise RuntimeError(f"postgres never became ready: {last}")


async def ensure_incidents_schema(conn) -> None:
    """Create or migrate the incidents table. Idempotent; shared with the agent."""
    await conn.execute(_SCHEMA)
    for statement in _MIGRATIONS + _INDEXES:
        await conn.execute(statement)


class InMemoryStore:
    name = "memory"

    def __init__(self) -> None:
        self.saved: list[Incident] = []

    async def save(self, incident: Incident) -> None:
        self.saved.append(incident)

    async def revise(self, incident: Incident) -> None:
        """Replace the stored leader with its revised version (same id)."""
        self.saved = [incident if i.id == incident.id else i for i in self.saved]


class PostgresStore:
    """Persists the redacted incident record to Postgres via asyncpg."""

    name = "postgres"

    def __init__(self, dsn: str, pool=None) -> None:
        self._dsn = dsn
        self._pool = pool  # inject a fake pool in tests
        self._schema_ready = False

    async def _get_pool(self):
        if self._pool is None:  # pragma: no cover - real DB path
            self._pool = await connect_pool(self._dsn)
        return self._pool

    async def ensure_schema(self) -> None:
        """Create or migrate the table now rather than on the first save, so a
        read-only consumer (the web UI) never meets a column that a not-yet-
        received incident would have added."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await ensure_incidents_schema(conn)
        self._schema_ready = True

    async def save(self, incident: Incident) -> None:
        pool = await self._get_pool()
        h = incident.hypothesis
        async with pool.acquire() as conn:
            if not self._schema_ready:
                await ensure_incidents_schema(conn)
                self._schema_ready = True
            await conn.execute(
                _INSERT,
                incident.id,
                incident.fingerprint,
                incident.alertname,
                incident.namespace,
                incident.severity,
                incident.status.value,
                h.root_cause if h else None,
                h.confidence if h else None,
                h.blast_radius if h else None,
                h.evidence if h else None,
                h.disproof if h else None,
                h.next_steps if h else None,
                incident.backend,
                incident.cost_usd,
                incident.latency_ms,
                incident.failure_reason,
                incident.created_at,
                incident.context or None,
                incident.time_to_hypothesis_ms or None,
                incident.alert_summary or None,
                incident.grouped_into or None,
                incident.storm_size or None,
                incident.correlated_alerts or None,
            )

    async def root_cause(self, incident_id: str) -> str:
        """The cause on record for an incident (for revised_from, whichever replica revises)."""
        async with self._pool.acquire() as conn:
            value = await conn.fetchval("SELECT root_cause FROM incidents WHERE id = $1", incident_id)
        return value or ""

    async def revise(self, incident: Incident) -> None:
        h = incident.hypothesis
        if h is None:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                _REVISE,
                incident.id,
                h.root_cause, h.confidence, h.blast_radius, h.evidence, h.disproof, h.next_steps,
                incident.backend, incident.cost_usd, incident.latency_ms,
                incident.correlated_alerts or None, incident.revised_from or None, incident.context,
            )


def get_store(cfg: Settings = settings):
    if cfg.store.lower() == "postgres":
        return PostgresStore(cfg.postgres_dsn)
    return InMemoryStore()
