"""Correctness checks for the dead man's switch.

CC-66  The heartbeat pings the external URL only when ingest-api is ready AND a
       worker has polled the stream recently; otherwise it pings <url>/fail with
       the reasons. A dead worker, a missing consumer group and an unready or
       unreachable ingest-api are each reported.
"""

from __future__ import annotations

import fakeredis.aioredis
import httpx

from app.heartbeat import run

STREAM, GROUP = "minus20:alerts", "analyzers"


class Recorder:
    def __init__(self, ready_status: int = 200, ingest_down: bool = False) -> None:
        self.posts: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/readyz"):
                if ingest_down:
                    raise httpx.ConnectError("refused")
                return httpx.Response(ready_status, json={"status": "ready"})
            self.posts.append((str(request.url), request.content.decode()))
            return httpx.Response(200)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _redis_with_worker():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    await r.xreadgroup(GROUP, "m20-analyzer-worker-abc", {STREAM: ">"}, count=1)  # a worker polls
    return r


async def test_healthy_pipeline_pings_the_url():  # CC-66
    rec = Recorder()
    problems = await run(await _redis_with_worker(), rec.client, "http://ingest:8080", "https://hc-ping.com/abc", STREAM, GROUP)
    assert problems == []
    assert rec.posts == [("https://hc-ping.com/abc", "ok")]


async def test_stale_worker_and_unready_ingest_ping_fail_with_reasons():  # CC-66
    rec = Recorder(ready_status=503)
    problems = await run(await _redis_with_worker(), rec.client, "http://ingest:8080", "https://hc-ping.com/abc",
                         STREAM, GROUP, max_idle_ms=-1)  # any idle time counts as stale
    assert any("not ready: HTTP 503" in p for p in problems)
    assert any("no analyzer-worker has polled" in p for p in problems)
    (url, body), = rec.posts
    assert url == "https://hc-ping.com/abc/fail" and "HTTP 503" in body


async def test_missing_group_and_unreachable_ingest_are_failures():  # CC-66
    rec = Recorder(ingest_down=True)
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    problems = await run(r, rec.client, "http://ingest:8080", "https://hc-ping.com/abc", STREAM, GROUP)
    assert any("ingest-api unreachable" in p for p in problems)
    assert any("cannot read consumers" in p for p in problems)
    assert rec.posts[0][0].endswith("/fail")


async def test_without_a_url_it_only_reports():
    rec = Recorder()
    assert await run(await _redis_with_worker(), rec.client, "http://ingest:8080", "", STREAM, GROUP) == []
    assert rec.posts == []
