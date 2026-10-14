"""Dead man's switch: prove the pipeline is alive to something outside the cluster.

Everything else in Minus20 reports through the cluster it runs in. If that
cluster, the node or the worker dies quietly, nobody is told: the silence
looks exactly like a quiet night. This job runs from a CronJob and checks
the two things that must hold for an alert to reach the engineer:

- ingest-api is ready (it answers /readyz, which pings Redis);
- at least one analyzer-worker has polled the alert stream recently (Redis
  tracks, per consumer, the time since its last read attempt).

Then it pings an external check URL (healthchecks.io or any service with
the same convention): the URL itself on success, `<url>/fail` with the
reasons on failure. If the whole server dies, the pings stop and the
external service raises the alarm, which is the case nothing inside the
cluster can report.

    python -m app.heartbeat
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger("minus20.heartbeat")

DEFAULT_MAX_IDLE_MS = 180_000  # a live worker polls every few seconds (block_ms)


async def worker_problem(redis, stream: str, group: str, max_idle_ms: int = DEFAULT_MAX_IDLE_MS) -> str | None:
    """None if some consumer of the group polled within max_idle_ms, else the reason."""
    try:
        consumers = await redis.xinfo_consumers(stream, group)
    except Exception as exc:  # noqa: BLE001 - no group yet, Redis down: both are failures
        return f"cannot read consumers of {stream}/{group}: {type(exc).__name__}"
    if not consumers:
        return f"no analyzer-worker has ever joined {group}"
    freshest = min(int(c.get("idle", 0)) for c in consumers)
    if freshest > max_idle_ms:
        return f"no analyzer-worker has polled for {freshest // 1000} s"
    return None


async def ingest_problem(client, ingest_url: str) -> str | None:
    try:
        resp = await client.get(f"{ingest_url.rstrip('/')}/readyz", timeout=10.0)
    except Exception as exc:  # noqa: BLE001 - unreachable is the failure being reported
        return f"ingest-api unreachable: {type(exc).__name__}"
    if resp.status_code != 200:
        return f"ingest-api not ready: HTTP {resp.status_code}"
    return None


async def run(redis, client, ingest_url: str, ping_url: str, stream: str, group: str,
              max_idle_ms: int = DEFAULT_MAX_IDLE_MS) -> list[str]:
    """Check, then report to the external URL. Returns the problems found."""
    problems = [p for p in (
        await ingest_problem(client, ingest_url),
        await worker_problem(redis, stream, group, max_idle_ms),
    ) if p]
    if ping_url:
        target = ping_url.rstrip("/") + ("/fail" if problems else "")
        try:
            await client.post(target, content="\n".join(problems) or "ok", timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - the missing ping is itself the alarm
            logger.warning("heartbeat ping failed: %s", type(exc).__name__)
    return problems


async def _main() -> int:  # pragma: no cover - real cluster path
    import httpx
    import redis.asyncio as aioredis

    from .config import settings
    from .logsafe import configure_logging

    configure_logging(settings.log_level)
    ingest_url = os.environ.get("MINUS20_HEARTBEAT_INGEST_URL", "http://localhost:8080")
    ping_url = os.environ.get("MINUS20_HEARTBEAT_URL", "")
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    async with httpx.AsyncClient() as client:
        problems = await run(redis, client, ingest_url, ping_url, settings.alerts_stream, settings.consumer_group)
    await redis.aclose()
    if problems:
        logger.warning("heartbeat: %s", "; ".join(problems))
        return 1
    logger.info("heartbeat: ingest ready, worker polling%s", "" if ping_url else " (no ping URL set)")
    return 0


def main() -> None:  # pragma: no cover - CLI entrypoint
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":  # pragma: no cover
    main()
