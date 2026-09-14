"""Redis Streams consumer loop.

Reads the alert stream through a consumer group (at-least-once), hands each alert
to the Analyzer, and acknowledges it. A message that cannot be parsed, or that
fails processing more than `max_delivery_attempts` times, is parked in the
dead-letter stream so it can never wedge the loop (ADR-0003). ingest-api is
completely decoupled from all of this: it only appends to the stream.
"""

from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from .analyzer import Analyzer
from .config import Settings, settings
from .models import StreamAlert

logger = logging.getLogger("analyzer-worker")


class Worker:
    def __init__(self, redis_client, analyzer: Analyzer, cfg: Settings = settings) -> None:
        self._redis = redis_client
        self._analyzer = analyzer
        self._cfg = cfg
        self._stopping = False

    def stop(self) -> None:
        """Finish the message in hand, then leave the loop.

        In a container this process is PID 1, and PID 1 ignores SIGTERM unless
        a handler is installed. Without one, the *old* pod of a rollout kept
        reading the stream for the whole 30 s grace period under the same
        consumer name as the new pod, and on 2026-09-14 analysed an alert with
        the code the rollout was replacing.
        """
        self._stopping = True

    async def ensure_group(self) -> None:
        """Create the consumer group idempotently (tolerate BUSYGROUP), waiting
        for Redis to accept connections first: on a fresh cluster it usually
        comes up after the worker."""
        for i in range(30):
            try:
                await self._redis.ping()
                break
            except (RedisConnectionError, OSError) as exc:  # redis' ConnectionError is not the builtin
                if i == 0:
                    logger.warning("redis not ready (%s); waiting", type(exc).__name__)
                await asyncio.sleep(3)
        try:
            await self._redis.xgroup_create(
                self._cfg.alerts_stream, self._cfg.consumer_group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def prune_consumers(self) -> int:
        """Forget consumers that are gone: no pending entries and idle for longer
        than the reclaim threshold. With one consumer name per pod, every
        rollout leaves a dead name behind otherwise."""
        try:
            consumers = await self._redis.xinfo_consumers(self._cfg.alerts_stream, self._cfg.consumer_group)
        except ResponseError:  # pragma: no cover - group not there yet
            return 0
        pruned = 0
        for c in consumers:
            name = c.get("name")
            if name == self._cfg.consumer_name or int(c.get("pending", 0)) or int(c.get("idle", 0)) < self._cfg.reclaim_idle_ms:
                continue
            await self._redis.xgroup_delconsumer(self._cfg.alerts_stream, self._cfg.consumer_group, name)
            pruned += 1
        if pruned:
            logger.info("pruned %d dead consumer(s) from group %s", pruned, self._cfg.consumer_group)
        return pruned

    async def reclaim(self) -> int:
        """Take over messages delivered to a consumer that never acknowledged them.

        XREADGROUP with ">" only ever returns *new* entries. A message read by a
        pod that was killed mid-analysis (a rollout, an OOM) stays in the pending
        list forever and is never processed by anyone, which is exactly what
        happened on 2026-09-13: an alert queued during a restart sat pending for
        an hour with no log line anywhere. XAUTOCLAIM hands such entries, idle
        for longer than `reclaim_idle_ms`, to this consumer.
        """
        try:
            _next, messages, *_deleted = await self._redis.xautoclaim(
                self._cfg.alerts_stream,
                self._cfg.consumer_group,
                self._cfg.consumer_name,
                min_idle_time=self._cfg.reclaim_idle_ms,
                start_id="0-0",
                count=self._cfg.read_count,
            )
        except ResponseError as exc:  # pragma: no cover - Redis < 6.2
            logger.warning("xautoclaim unsupported, stale messages will not be reclaimed: %s", exc)
            return 0
        handled = 0
        for msg_id, fields in messages:
            if fields is None:  # entry deleted from the stream since delivery
                await self._ack(msg_id)
                continue
            logger.warning("reclaiming %s left pending by a previous consumer", msg_id)
            await self._handle(msg_id, fields, reclaimed=True)
            handled += 1
        return handled

    async def run_once(self) -> int:
        """Read and process one batch. Returns the number of messages handled."""
        handled = await self.reclaim()
        resp = await self._redis.xreadgroup(
            self._cfg.consumer_group,
            self._cfg.consumer_name,
            {self._cfg.alerts_stream: ">"},
            count=self._cfg.read_count,
            block=self._cfg.block_ms,
        )
        for _stream, messages in resp or []:
            for msg_id, fields in messages:
                await self._handle(msg_id, fields)
                handled += 1
        return handled

    async def _handle(self, msg_id: str, fields: dict, *, reclaimed: bool = False) -> None:
        try:
            await self.process(msg_id, fields, reclaimed=reclaimed)
        except Exception:
            logger.exception("unhandled error processing %s (will redeliver)", msg_id)

    async def process(self, msg_id: str, fields: dict, *, reclaimed: bool = False) -> str:
        """Process one message. Returns a status string (used by tests)."""
        try:
            alert = StreamAlert(**json.loads(fields["payload"]))
        except Exception as exc:  # noqa: BLE001 - any malformed payload is a poison message
            logger.warning("dead-lettering unparseable message %s: %s", msg_id, exc)
            await self._dead_letter(fields, reason=f"parse_error: {exc}")
            await self._ack(msg_id)
            return "dead:parse"

        try:
            await self._analyzer.analyze(alert, skip_dedup=reclaimed)
        except Exception as exc:  # noqa: BLE001 - infra failure → bounded retry, then dead-letter
            attempts = int(fields.get("_attempts", "0")) + 1
            if attempts >= self._cfg.max_delivery_attempts:
                logger.error("dead-lettering %s after %d attempts: %s", msg_id, attempts, exc)
                await self._dead_letter(fields, reason=f"max_attempts: {exc}")
                await self._ack(msg_id)
                return "dead:attempts"
            # App-level retry: re-enqueue with an incremented attempt counter,
            # then ack the original so it leaves the pending list.
            await self._redis.xadd(
                self._cfg.alerts_stream,
                {**fields, "_attempts": str(attempts)},
                maxlen=None,
            )
            await self._ack(msg_id)
            return "retried"

        await self._ack(msg_id)
        return "analyzed"

    async def _ack(self, msg_id: str) -> None:
        await self._redis.xack(self._cfg.alerts_stream, self._cfg.consumer_group, msg_id)

    async def _dead_letter(self, fields: dict, reason: str) -> None:
        await self._redis.xadd(
            self._cfg.dead_letter_stream,
            {"payload": fields.get("payload", ""), "reason": reason},
        )

    async def run(self) -> None:  # pragma: no cover - exercised by run_once in tests
        # First, before any await: a SIGTERM that lands during startup (a
        # rollout racing a rollout) must not be ignored by PID 1.
        self._install_signal_handlers()
        await self.ensure_group()
        store = getattr(self._analyzer, "_store", None)
        if hasattr(store, "ensure_schema"):
            await store.ensure_schema()  # migrations up front, not on the first incident
        logger.info(
            "analyzer-worker up: group=%s consumer=%s stream=%s",
            self._cfg.consumer_group,
            self._cfg.consumer_name,
            self._cfg.alerts_stream,
        )
        await self.prune_consumers()
        while not self._stopping:
            await self.run_once()
        logger.info("analyzer-worker stopped cleanly (consumer=%s)", self._cfg.consumer_name)

    def _install_signal_handlers(self) -> None:  # pragma: no cover - process plumbing
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, RuntimeError):  # Windows dev shells
                signal.signal(sig, lambda *_: self.stop())


def build_worker(cfg: Settings = settings) -> Worker:  # pragma: no cover - wiring
    """Wire the production adapters together from configuration."""
    from .backends import get_backend
    from .budget import get_budget
    from .collectors import get_collector
    from .dedup import get_deduplicator
    from .notifiers import get_notifier
    from .stores import get_store
    from .storm import get_storm_tracker

    redis_client = redis.from_url(cfg.redis_url, decode_responses=True)
    analyzer = Analyzer(
        collector=get_collector(cfg),
        backend=get_backend(cfg),
        notifier=get_notifier(cfg),
        store=get_store(cfg),
        budget=get_budget(redis_client, cfg),
        llm_timeout_seconds=cfg.llm_timeout_seconds,
        deduplicator=get_deduplicator(redis_client, cfg),
        storm_tracker=get_storm_tracker(redis_client, cfg),
    )
    return Worker(redis_client, analyzer, cfg)


def main() -> None:  # pragma: no cover - process entrypoint
    from .logsafe import configure_logging

    configure_logging(settings.log_level)
    asyncio.run(build_worker().run())


if __name__ == "__main__":  # pragma: no cover
    main()
