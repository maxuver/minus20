"""Memory of this cluster: retrieval over the team's own incidents and runbooks.

The third time a NetworkPolicy blocks DNS, the right answer is "same as 12
August". No prompt gives a model that; only the team's incident history does,
and it is the one thing a competitor cannot copy from this repository.

- Embeddings are computed locally through Ollama (nomic-embed-text), so memory
  obeys the same zero-egress rule as analysis (ADR-0002).
- Storage is pgvector in the Postgres the chart already deploys. If the
  extension is missing (plain postgres image) memory disables itself and the
  agent keeps working without it (ADR-0003).
- Only redacted data is ever indexed: incident rows are redacted before they
  are stored, and runbook text is redacted on the way in.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..config import Settings, settings
from ..redaction import redact
from ..stores import ensure_incidents_schema

logger = logging.getLogger("minus20.agent.memory")

_VECTOR_SCHEMA = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
    CREATE TABLE IF NOT EXISTS memory (
        id         TEXT PRIMARY KEY,
        kind       TEXT NOT NULL,
        ref        TEXT NOT NULL,
        title      TEXT,
        content    TEXT NOT NULL,
        embedding  vector(%(dim)d),
        created_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS memory_kind_ref_idx ON memory (kind, ref)",
)

_UPSERT = """
INSERT INTO memory (id, kind, ref, title, content, embedding)
VALUES ($1, $2, $3, $4, $5, $6::vector)
ON CONFLICT (id) DO UPDATE
SET title = EXCLUDED.title, content = EXCLUDED.content, embedding = EXCLUDED.embedding,
    created_at = now()
"""

_SEARCH = """
SELECT kind, ref, title, content, 1 - (embedding <=> $1::vector) AS score
FROM memory
ORDER BY embedding <=> $1::vector
LIMIT $2
"""

# Incidents not yet in memory, or whose resolution arrived after indexing.
# Stub-backend rows are placeholders, not knowledge: indexing them taught the
# agent, in the first live run, that "the root cause was a stub backend".
_UNINDEXED_INCIDENTS = """
SELECT i.id, i.alertname, i.namespace, i.severity, i.root_cause, i.evidence,
       i.disproof, i.next_steps, i.verdict, i.resolution, i.created_at
FROM incidents i
LEFT JOIN memory m ON m.kind = 'incident' AND m.ref = i.id
WHERE i.root_cause IS NOT NULL
  AND i.status = 'analyzed'
  AND i.backend <> 'stub'
  AND (m.id IS NULL OR (i.resolved_at IS NOT NULL AND i.resolved_at > m.created_at))
ORDER BY i.created_at DESC
LIMIT $1
"""


@dataclass
class Hit:
    kind: str
    ref: str
    title: str
    content: str
    score: float


@runtime_checkable
class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    """Local embeddings via Ollama's /api/embed. $0, no egress."""

    def __init__(self, cfg: Settings = settings, client=None) -> None:
        self._cfg = cfg
        self._client = client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        if not texts:
            return []
        client = self._client or httpx.AsyncClient(base_url=self._cfg.ollama_url, timeout=120.0)
        try:
            resp = await client.post(
                "/api/embed", json={"model": self._cfg.embed_model, "input": texts}
            )
            resp.raise_for_status()
            return resp.json().get("embeddings") or []
        finally:
            if self._client is None:
                await client.aclose()


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def incident_text(row: dict) -> tuple[str, str]:
    """Render an incident row as (title, content) for indexing.

    The engineer's verdict goes first: a wrong hypothesis with a real
    resolution attached is the most valuable memory there is.
    """
    title = f"{row.get('alertname') or 'Alert'} in {row.get('namespace') or '?'}"
    when = row.get("created_at")
    parts = [f"When: {when:%Y-%m-%d %H:%M}Z" if when else "", f"Severity: {row.get('severity')}"]
    verdict, resolution = row.get("verdict"), row.get("resolution")
    if verdict == "wrong" and resolution:
        parts.append(f"ACTUAL CAUSE (engineer): {resolution}")
        parts.append(f"Hypothesis at the time (WRONG): {row.get('root_cause')}")
    else:
        parts.append(f"Root cause: {row.get('root_cause')}")
        if verdict == "correct":
            parts.append("Confirmed correct by the engineer" + (f": {resolution}" if resolution else ""))
    if row.get("evidence"):
        parts.append("Evidence: " + "; ".join(row["evidence"]))
    if row.get("disproof"):
        parts.append(f"Disproof: {row['disproof']}")
    if row.get("next_steps"):
        parts.append("Next steps: " + "; ".join(row["next_steps"]))
    return title, "\n".join(p for p in parts if p)


def chunk_text(text: str, max_chars: int = 1_200) -> list[str]:
    """Split on paragraph boundaries into chunks small enough to embed well."""
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
        while len(current) > max_chars:  # a single oversized paragraph
            chunks.append(current[:max_chars])
            current = current[max_chars:]
    if current:
        chunks.append(current)
    return chunks


class Memory:
    def __init__(self, pool: Any, embedder: Embedder, cfg: Settings = settings) -> None:
        self._pool = pool
        self._embedder = embedder
        self._cfg = cfg
        self.available = False

    async def ensure_schema(self) -> bool:
        """Ensure the incidents table (with its feedback columns) always, and
        the vector store when pgvector exists. Returns False (memory disabled,
        feedback still works) otherwise."""
        async with self._pool.acquire() as conn:
            await ensure_incidents_schema(conn)
            try:
                for statement in _VECTOR_SCHEMA:
                    await conn.execute(statement % {"dim": self._cfg.embed_dim})
            except Exception as exc:  # noqa: BLE001 - missing extension is a config state
                logger.warning("memory disabled (is the Postgres image pgvector/pgvector?): %s", exc)
                self.available = False
                return False
        self.available = True
        return True

    async def _upsert(self, kind: str, ref: str, title: str, content: str) -> None:
        content = redact(content)
        [vec] = await self._embedder.embed([content])
        mid = hashlib.sha1(f"{kind}:{ref}".encode(), usedforsecurity=False).hexdigest()
        async with self._pool.acquire() as conn:
            await conn.execute(_UPSERT, mid, kind, ref, redact(title), content, _vec(vec))

    async def index_incidents(self, limit: int = 500) -> int:
        """Index incidents the reflex has stored since the last run."""
        if not self.available:
            return 0
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_UNINDEXED_INCIDENTS, limit)
        for row in rows:
            title, content = incident_text(dict(row))
            await self._upsert("incident", row["id"], title, content)
        return len(rows)

    async def index_documents(self, directory: str | Path) -> int:
        """Index every .md/.txt runbook under `directory`, chunked."""
        if not self.available or not directory:
            return 0
        root = Path(directory)
        if not root.is_dir():
            return 0
        n = 0
        for path in sorted(p for p in root.rglob("*") if p.suffix.lower() in (".md", ".txt")):
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(root).as_posix()
            for i, chunk in enumerate(chunk_text(text)):
                await self._upsert("doc", f"{rel}#{i}", rel, chunk)
                n += 1
        return n

    async def search(self, query: str, k: int = 5) -> list[Hit]:
        if not self.available:
            return []
        [vec] = await self._embedder.embed([redact(query)])
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_SEARCH, _vec(vec), k)
        return [
            Hit(r["kind"], r["ref"], r["title"] or "", r["content"], float(r["score"]))
            for r in rows
        ]

    async def record_feedback(self, incident_prefix: str, verdict: str, resolution: str) -> str | None:
        """Store the engineer's verdict on an incident and re-index it.

        Returns the full incident id, or None if no incident matched.
        """
        prefix = "".join(c for c in incident_prefix.lower() if c in "0123456789abcdef")
        if not prefix:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE incidents SET verdict = $2, resolution = $3, resolved_at = now() "
                "WHERE id = (SELECT id FROM incidents WHERE id LIKE $1 "
                "ORDER BY created_at DESC LIMIT 1) RETURNING id",
                prefix + "%",
                verdict,
                redact(resolution) or None,
            )
        if row is None:
            return None
        if self.available:
            await self.index_incidents()
        return row["id"]
