"""Fold an alert storm into one incident.

A node dies, a bad rollout lands, a dependency goes away: thirty pods in one
namespace fire the same alert within a minute. Deduplication (dedup.py)
catches repeats of the *same* alert; it does nothing for thirty *different*
pods with the same alertname. Without this, the storm costs thirty model
calls and sends the engineer thirty messages that each describe one tree.

The first alert of a `alertname + namespace` key inside the window is the
leader and is analysed as usual. Later alerts with the same key are members:
recorded, counted, never analysed. The engineer hears about the storm at a
few thresholds ("now 5 pods", "now 10"), each message naming the leader's
incident so the one hypothesis that exists is one click away.

Redis-backed so several workers agree on who the leader is; the in-memory
version serves tests and single-replica runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Settings, settings
from .models import StreamAlert

_PREFIX = "minus20:storm"
NOTIFY_AT = (2, 5, 10, 25, 50, 100, 250, 500)
MAX_PODS_KEPT = 20


def storm_key(alert: StreamAlert) -> str:
    return f"{_PREFIX}:{alert.alertname}:{alert.namespace or '-'}"


@dataclass
class StormState:
    leader: bool
    count: int  # alerts in this storm so far, leader included
    leader_id: str
    pods: list[str] = field(default_factory=list)

    @property
    def notify(self) -> bool:
        return not self.leader and self.count in NOTIFY_AT


class InMemoryStormTracker:
    def __init__(self, window_seconds: int) -> None:
        self._window = window_seconds
        self._storms: dict[str, tuple[float, str, int, list[str]]] = {}

    async def track(self, alert: StreamAlert, incident_id: str) -> StormState:
        key = storm_key(alert)
        now = time.monotonic()
        pod = alert.labels.get("pod", "")
        open_ = self._storms.get(key)
        if open_ is None or open_[0] <= now:
            self._storms[key] = (now + self._window, incident_id, 1, [pod] if pod else [])
            return StormState(leader=True, count=1, leader_id=incident_id, pods=[pod] if pod else [])
        expires, leader_id, count, pods = open_
        count += 1
        if pod and pod not in pods and len(pods) < MAX_PODS_KEPT:
            pods.append(pod)
        self._storms[key] = (expires, leader_id, count, pods)
        return StormState(leader=False, count=count, leader_id=leader_id, pods=list(pods))


class RedisStormTracker:
    """Shared across replicas. One key per storm: a hash with the leader's
    incident id, a counter and the first pods; expiry is the window."""

    def __init__(self, redis, window_seconds: int) -> None:
        self._redis = redis
        self._window = window_seconds

    async def track(self, alert: StreamAlert, incident_id: str) -> StormState:
        key = storm_key(alert)
        pod = alert.labels.get("pod", "")
        # HSETNX is atomic: exactly one worker becomes the leader of a key.
        became_leader = await self._redis.hsetnx(key, "leader", incident_id)
        if became_leader:
            await self._redis.hset(key, mapping={"count": 1, "pods": pod})
            await self._redis.expire(key, self._window)
            return StormState(leader=True, count=1, leader_id=incident_id, pods=[pod] if pod else [])
        count = int(await self._redis.hincrby(key, "count", 1))
        leader_id = await self._redis.hget(key, "leader") or ""
        raw = await self._redis.hget(key, "pods") or ""
        pods = [p for p in raw.split(",") if p]
        if pod and pod not in pods and len(pods) < MAX_PODS_KEPT:
            pods.append(pod)
            await self._redis.hset(key, "pods", ",".join(pods))
        return StormState(leader=False, count=count, leader_id=leader_id, pods=pods)


def get_storm_tracker(redis=None, cfg: Settings = settings):
    if redis is not None:
        return RedisStormTracker(redis, cfg.storm_window_seconds)
    return InMemoryStormTracker(cfg.storm_window_seconds)
