"""Suppress repeat analyses of the same alert.

Alertmanager re-sends a firing alert every `repeat_interval`, and an alert storm
can deliver the same fingerprint many times. Without suppression each repeat
would pay for another LLM call and send the engineer another message about an
incident they already know about.

The first occurrence of a fingerprint inside the window wins; the rest are
suppressed. Implemented as an atomic `SET key value NX EX window`, so several
worker replicas cannot each decide they are the first.
"""

from __future__ import annotations

import time

from .config import Settings, settings
from .models import StreamAlert

_PREFIX = "minus20:seen"


def alert_key(alert: StreamAlert) -> str:
    """Stable identity for an alert.

    Prefers Alertmanager's own fingerprint. Falls back to the identifying labels
    when it is absent, so a hand-crafted or partial payload is still deduplicated
    rather than silently re-analysed every time.
    """
    if alert.fingerprint:
        return f"{_PREFIX}:{alert.fingerprint}"
    parts = [
        alert.alertname,
        alert.namespace,
        alert.labels.get("pod", ""),
        alert.labels.get("container", ""),
    ]
    return f"{_PREFIX}:fallback:" + "|".join(parts)


class InMemoryDeduplicator:
    """Process-local suppression, for tests and single-replica runs."""

    def __init__(self, window_seconds: int) -> None:
        self._window = window_seconds
        self._seen: dict[str, float] = {}

    async def is_duplicate(self, alert: StreamAlert) -> bool:
        key = alert_key(alert)
        now = time.monotonic()
        expires = self._seen.get(key)
        if expires is not None and expires > now:
            return True
        self._seen[key] = now + self._window
        return False


class RedisDeduplicator:
    """Suppression shared across worker replicas, backed by Redis."""

    def __init__(self, redis, window_seconds: int) -> None:
        self._redis = redis
        self._window = window_seconds

    async def is_duplicate(self, alert: StreamAlert) -> bool:
        # SET NX is atomic: exactly one replica gets True and analyses the alert.
        was_set = await self._redis.set(alert_key(alert), "1", nx=True, ex=self._window)
        return not was_set


def get_deduplicator(redis=None, cfg: Settings = settings):
    if redis is not None:
        return RedisDeduplicator(redis, cfg.dedup_window_seconds)
    return InMemoryDeduplicator(cfg.dedup_window_seconds)
