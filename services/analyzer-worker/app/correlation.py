"""Correlate different alerts of one namespace into one incident (ADR-0006).

A storm (storm.py) is the same alert on many pods. Correlation is the other
shape of a 03:00 page: a crash loop, a replicas mismatch and an error-rate
alert fire within a minute in the same namespace, and they are one fault.
Without this, the engineer gets three hypotheses and does the joining.

The first alert of a namespace inside the window is the leader and is
analysed at once, so the first hypothesis is not delayed. A later alert of a
*different* name in that namespace is a member: recorded, attached to the
leader, its context collected and redacted, but not analysed on its own.
After a short settle period one **revision** runs: a single model call over
the leader's context plus every attached member's, asking for the one cause
that explains them all. The leader's hypothesis is replaced and the previous
cause is kept as `revised_from`; the engineer gets one message.

State lives in the tracker (Redis when several replicas share the stream),
so the replica that receives a member can revise even if another replica
analysed the leader. At most one revision per settle period; a member that
arrives after it is attached and listed, not re-analysed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .config import Settings, settings
from .models import StreamAlert

_PREFIX = "minus20:corr"
MAX_MEMBERS = 12  # contexts kept for the revision prompt; the rest are listed by name only


def correlation_key(alert: StreamAlert) -> str:
    return f"{_PREFIX}:{alert.namespace or '-'}"


def alert_line(alert: StreamAlert) -> str:
    """How an attached alert is listed: 'KubePodCrashLooping pod=billing-api-1'."""
    pod = alert.labels.get("pod", "")
    return f"{alert.alertname} pod={pod}" if pod else alert.alertname


@dataclass
class CorrelationState:
    leader: bool
    leader_id: str
    members: list[str] = field(default_factory=list)  # alert_line() of every member so far


@dataclass
class CorrelationBundle:
    """Everything a revision needs, whichever replica runs it."""

    leader_id: str
    leader_alert: StreamAlert
    leader_context: str
    members: list[tuple[StreamAlert, str]]  # (alert, rendered redacted context)


def _dump(alert: StreamAlert) -> str:
    return alert.model_dump_json()


def _load(raw: str) -> StreamAlert:
    return StreamAlert.model_validate_json(raw)


class InMemoryCorrelationTracker:
    def __init__(self, window_seconds: int) -> None:
        self._window = window_seconds
        self._open: dict[str, dict] = {}

    async def track(self, alert: StreamAlert, incident_id: str, context: str) -> CorrelationState:
        key = correlation_key(alert)
        now = time.monotonic()
        cur = self._open.get(key)
        if cur is None or cur["expires"] <= now:
            self._open[key] = {
                "expires": now + self._window, "leader": incident_id, "leader_alert": alert,
                "leader_context": context, "members": [], "claimed_until": 0.0,
            }
            return CorrelationState(leader=True, leader_id=incident_id)
        cur["members"].append((alert, context))
        return CorrelationState(leader=False, leader_id=cur["leader"], members=[alert_line(a) for a, _ in cur["members"]])

    async def bundle(self, alert: StreamAlert) -> CorrelationBundle | None:
        cur = self._open.get(correlation_key(alert))
        if cur is None or cur["expires"] <= time.monotonic():
            return None
        return CorrelationBundle(cur["leader"], cur["leader_alert"], cur["leader_context"], list(cur["members"]))

    async def claim_revision(self, alert: StreamAlert, hold_seconds: float) -> bool:
        cur = self._open.get(correlation_key(alert))
        if cur is None:
            return False
        now = time.monotonic()
        if cur["claimed_until"] > now:
            return False
        cur["claimed_until"] = now + hold_seconds
        return True


class RedisCorrelationTracker:
    """One hash per namespace window: leader id, leader alert + context, a
    list of members (JSON), expiry = window. HSETNX elects the leader."""

    def __init__(self, redis, window_seconds: int) -> None:
        self._redis = redis
        self._window = window_seconds

    async def track(self, alert: StreamAlert, incident_id: str, context: str) -> CorrelationState:
        key = correlation_key(alert)
        if await self._redis.hsetnx(key, "leader", incident_id):
            await self._redis.hset(key, mapping={"leader_alert": _dump(alert), "leader_context": context})
            await self._redis.expire(key, self._window)
            return CorrelationState(leader=True, leader_id=incident_id)
        leader_id = await self._redis.hget(key, "leader") or ""
        await self._redis.rpush(f"{key}:members", json.dumps({"alert": _dump(alert), "context": context}))
        await self._redis.expire(f"{key}:members", self._window)
        raw = await self._redis.lrange(f"{key}:members", 0, -1)
        members = [alert_line(_load(json.loads(r)["alert"])) for r in raw]
        return CorrelationState(leader=False, leader_id=leader_id, members=members)

    async def bundle(self, alert: StreamAlert) -> CorrelationBundle | None:
        key = correlation_key(alert)
        head = await self._redis.hgetall(key)
        if not head or "leader_alert" not in head:
            return None
        raw = await self._redis.lrange(f"{key}:members", 0, -1)
        members = [(_load(m["alert"]), m["context"]) for m in (json.loads(r) for r in raw)]
        return CorrelationBundle(head["leader"], _load(head["leader_alert"]), head.get("leader_context", ""), members)

    async def claim_revision(self, alert: StreamAlert, hold_seconds: float) -> bool:
        # SET NX EX: exactly one replica revises per settle period.
        return bool(await self._redis.set(f"{correlation_key(alert)}:revising", "1", nx=True, ex=max(1, int(hold_seconds))))


def get_correlation_tracker(redis=None, cfg: Settings = settings):
    if redis is not None:
        return RedisCorrelationTracker(redis, cfg.correlation_window_seconds)
    return InMemoryCorrelationTracker(cfg.correlation_window_seconds)
