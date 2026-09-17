"""The closed, read-only tool set the agent can call (ADR-0005).

Every tool here observes; none can change anything. There is no shell, no
kubectl, no filesystem and no HTTP to arbitrary hosts. A prompt injection in a
log line can therefore make the agent say something wrong; it cannot make it
do anything. The set is closed on purpose: adding a tool is a code review, not
a plugin install.

Tool output is redacted (ADR-0002) before the model sees it, exactly as the
worker redacts a context bundle.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..collectors import K8sEventsCollector, LokiCollector, PrometheusCollector
from ..config import Settings, settings
from ..models import StreamAlert
from ..redaction import redact

logger = logging.getLogger("minus20.agent.tools")

MAX_TOOL_OUTPUT_CHARS = 6_000


@dataclass
class ToolContext:
    """Everything a tool may reach. Fakes are injected here in tests."""

    cfg: Settings = field(default_factory=lambda: settings)
    pool: Any = None  # asyncpg pool for the incident history; None = unavailable
    memory: Any = None  # agent.memory.Memory; None = unavailable
    k8s_api: Any = None  # CoreV1Api-like (events, pod logs)
    k8s_apps: Any = None  # AppsV1Api-like (replicasets: what changed)
    http: Any = None  # httpx.AsyncClient for Prometheus/Loki in tests
    _api_client: Any = None  # one kubernetes ApiClient shared by both APIs, for the process lifetime

    async def _k8s(self):  # pragma: no cover - real cluster path
        """One ApiClient for the whole process. Creating one per call leaked an
        aiohttp session each time (seen as 'Unclosed client session' in the
        first live MCP run)."""
        from kubernetes_asyncio import client, config

        if self._api_client is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                await config.load_kube_config()
            self._api_client = client.ApiClient()
        return client, self._api_client

    async def core_api(self):
        if self.k8s_api is None:  # pragma: no cover - real cluster path
            client, api_client = await self._k8s()
            self.k8s_api = client.CoreV1Api(api_client)
        return self.k8s_api

    async def apps_api(self):
        if self.k8s_apps is None:  # pragma: no cover - real cluster path
            client, api_client = await self._k8s()
            self.k8s_apps = client.AppsV1Api(api_client)
        return self.k8s_apps

    async def close(self) -> None:
        """Release the Kubernetes client and the DB pool at shutdown."""
        if self._api_client is not None:  # pragma: no cover - real cluster path
            await self._api_client.close()
            self._api_client = None
        if self.pool is not None and hasattr(self.pool, "close"):
            await self.pool.close()


ToolFn = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    fn: ToolFn

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# --- incident history --------------------------------------------------------


async def recent_incidents(
    ctx: ToolContext, hours: int = 24, namespace: str | None = None, limit: int = 10
) -> str:
    if ctx.pool is None:
        return "incident history unavailable (store is not postgres)"
    hours = max(1, min(int(hours), 24 * 90))
    limit = max(1, min(int(limit), 50))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with ctx.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, alertname, namespace, severity, status, root_cause, confidence, "
            "created_at, verdict, resolution FROM incidents "
            "WHERE created_at > $1 AND ($2::text IS NULL OR namespace = $2) "
            "ORDER BY created_at DESC LIMIT $3",
            since,
            namespace or None,
            limit,
        )
    if not rows:
        return f"no incidents in the last {hours}h" + (f" in {namespace}" if namespace else "")
    lines = []
    for r in rows:
        when = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "?"
        line = (
            f"#{r['id'][:8]} {when} {r['alertname']} ns={r['namespace']} "
            f"sev={r['severity']} status={r['status']}: {r['root_cause'] or '-'}"
        )
        if r["verdict"]:
            line += f" [engineer: {r['verdict']}"
            if r["resolution"]:
                line += f": {r['resolution']}"
            line += "]"
        lines.append(line)
    return "\n".join(lines)


async def incident_details(ctx: ToolContext, incident_id: str) -> str:
    if ctx.pool is None:
        return "incident history unavailable (store is not postgres)"
    prefix = "".join(c for c in str(incident_id).lower() if c in "0123456789abcdef")
    if not prefix:
        return "incident_id must be a hex id (the #xxxxxxxx shown in messages)"
    async with ctx.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, alertname, namespace, severity, status, root_cause, confidence, "
            "blast_radius, evidence, disproof, next_steps, backend, created_at, "
            "verdict, resolution, context FROM incidents WHERE id LIKE $1 "
            "ORDER BY created_at DESC LIMIT 1",
            prefix + "%",
        )
    if row is None:
        return f"no incident with id starting {prefix}"
    d = dict(row)
    d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
    # The audit trail: what the model was shown. Capped so one incident cannot
    # crowd out the conversation; the full text stays in the database.
    ctx = d.pop("context", None) or ""
    if len(ctx) > 2_000:
        ctx = ctx[:2_000] + "\n… (truncated; full context is stored with the incident)"
    d["context_shown_to_model"] = ctx or "(no context recorded)"
    return json.dumps(d, ensure_ascii=False, indent=1)


async def search_memory(ctx: ToolContext, query: str, limit: int = 5) -> str:
    if ctx.memory is None:
        return "memory unavailable (pgvector or the embedding model is not configured)"
    hits = await ctx.memory.search(str(query), k=max(1, min(int(limit), 10)))
    if not hits:
        return "no similar past incidents or documents"
    return "\n\n".join(f"[{h.kind} {h.ref} score={h.score:.2f}] {h.title}\n{h.content}" for h in hits)


# --- live cluster (read-only) -----------------------------------------------


def _synthetic_alert(namespace: str, pod: str | None) -> StreamAlert:
    labels = {"namespace": namespace}
    if pod:
        labels["pod"] = pod
    return StreamAlert(labels=labels, startsAt=datetime.now(timezone.utc))


async def k8s_events(ctx: ToolContext, namespace: str, pod: str | None = None) -> str:
    collector = K8sEventsCollector(api=await ctx.core_api(), max_events=ctx.cfg.k8s_max_events)
    bundle = await collector.collect(_synthetic_alert(namespace, pod))
    return "\n".join(bundle.k8s_events) or f"no events in {namespace}"


async def pod_metrics(ctx: ToolContext, namespace: str, pod: str) -> str:
    collector = PrometheusCollector(ctx.cfg.prometheus_url, client=ctx.http)
    bundle = await collector.collect(_synthetic_alert(namespace, pod))
    return "\n".join(bundle.metrics) or f"no metrics for {namespace}/{pod}"


async def pod_logs(
    ctx: ToolContext, namespace: str, pod: str | None = None, minutes: int = 15
) -> str:
    """Logs from Loki when it is configured; otherwise straight from the
    Kubernetes API, including the previous container of a crash-looping pod,
    which is where the reason for the crash usually is. Small teams often have
    no Loki; the agent must still be able to read a log."""
    if "loki" in ctx.cfg.collectors:
        collector = LokiCollector(
            ctx.cfg.loki_url,
            client=ctx.http,
            max_lines=ctx.cfg.loki_max_lines,
            window_minutes=max(1, min(int(minutes), 240)),
        )
        bundle = await collector.collect(_synthetic_alert(namespace, pod))
        return "\n".join(bundle.log_lines) or "no log lines in the window"
    if not pod:
        return "without Loki I can only read one pod's logs: name the pod"
    api = await ctx.core_api()
    out: list[str] = []
    for previous in (False, True):
        try:
            text = await api.read_namespaced_pod_log(
                pod, namespace, tail_lines=ctx.cfg.loki_max_lines, previous=previous
            )
        except Exception as exc:  # noqa: BLE001 - e.g. no previous container
            if not previous:
                return f"could not read logs of {namespace}/{pod}: {exc}"
            break
        if text and text.strip():
            out.append(("--- previous container (before the last crash)\n" if previous else "") + text.strip())
    return "\n".join(out) or f"{namespace}/{pod} has produced no log output"


async def node_status(ctx: ToolContext) -> str:
    """Node conditions and recent node events, read-only through the API.

    The half of node-level insight that needs no privileged DaemonSet and no
    SSM: MemoryPressure, DiskPressure, PIDPressure, NotReady, and the
    Warning events kubelet writes about a node (eviction, OOM-kills at node
    level, image GC). Needs get/list on nodes; if RBAC is namespace-scoped
    the tool says so instead of failing.
    """
    api = await ctx.core_api()
    try:
        nodes = await api.list_node()
    except Exception as exc:  # noqa: BLE001 - usually RBAC in namespaced mode
        return f"node status unavailable: {exc}"
    lines = []
    for node in nodes.items:
        name = node.metadata.name
        conds = {c.type: c.status for c in (node.status.conditions or [])}
        ready = conds.get("Ready", "Unknown")
        pressure = [t for t in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable") if conds.get(t) == "True"]
        alloc = node.status.allocatable or {}
        info = getattr(node.status, "node_info", None)
        version = getattr(info, "kubelet_version", "") if info else ""
        line = f"{name}: Ready={ready}"
        if pressure:
            line += " PRESSURE=" + ",".join(pressure)
        line += f" allocatable cpu={alloc.get('cpu', '?')} memory={alloc.get('memory', '?')} pods={alloc.get('pods', '?')}"
        if version:
            line += f" kubelet={version}"
        lines.append(line)
    try:
        events = await api.list_event_for_all_namespaces(field_selector="involvedObject.kind=Node")
        warnings = [e for e in events.items if e.type == "Warning"]
        warnings.sort(key=lambda e: (e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        for e in warnings[: ctx.cfg.k8s_max_events]:
            lines.append(f"event {e.reason} node/{getattr(e.involved_object, 'name', '')}: {e.message}")
    except Exception as exc:  # noqa: BLE001 - events are a bonus here
        lines.append(f"(node events unavailable: {exc})")
    return "\n".join(lines) or "no nodes visible"


async def deploy_history(ctx: ToolContext, namespace: str, hours: int = 24) -> str:
    """What changed: every ReplicaSet created in the window is a rollout."""
    hours = max(1, min(int(hours), 24 * 30))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    api = await ctx.apps_api()
    resp = await api.list_namespaced_replica_set(namespace)
    rows = []
    for rs in resp.items:
        created = getattr(rs.metadata, "creation_timestamp", None)
        if created is None or created < since:
            continue
        owners = getattr(rs.metadata, "owner_references", None) or []
        owner = next(
            (f"{o.kind.lower()}/{o.name}" for o in owners if getattr(o, "kind", "")),
            f"replicaset/{rs.metadata.name}",
        )
        containers = rs.spec.template.spec.containers or []
        images = ", ".join(c.image for c in containers if getattr(c, "image", None))
        replicas = getattr(rs.status, "replicas", 0) or 0
        rows.append((created, f"{created:%Y-%m-%d %H:%M}Z {owner} → {images} (replicas now {replicas})"))
    if not rows:
        return f"no rollouts in {namespace} in the last {hours}h"
    rows.sort(key=lambda r: r[0], reverse=True)
    return "\n".join(line for _, line in rows)


# --- registry ----------------------------------------------------------------

_NS = {"type": "string", "description": "Kubernetes namespace"}
_POD = {"type": "string", "description": "pod name; omit for the whole namespace"}

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "recent_incidents",
            "List incidents Minus20 analysed recently, newest first, with the "
            "engineer's verdict when one was given. Start here for 'what happened'.",
            {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "look-back window, default 24"},
                    "namespace": {"type": "string", "description": "filter by namespace"},
                    "limit": {"type": "integer", "description": "max rows, default 10"},
                },
            },
            recent_incidents,
        ),
        Tool(
            "incident_details",
            "Full record of one incident: hypothesis, evidence, disproof, next steps, "
            "and the engineer's resolution if any. Use the #id from a message.",
            {
                "type": "object",
                "properties": {"incident_id": {"type": "string"}},
                "required": ["incident_id"],
            },
            incident_details,
        ),
        Tool(
            "search_memory",
            "Semantic search over past incidents (with their real resolutions) and the "
            "team's runbooks. Always check this before answering 'why': the same "
            "failure has often happened before in this cluster.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "description": "default 5"},
                },
                "required": ["query"],
            },
            search_memory,
        ),
        Tool(
            "k8s_events",
            "Kubernetes events for a namespace or pod (what kubectl describe shows): "
            "BackOff, OOMKilled, FailedScheduling, probe failures.",
            {
                "type": "object",
                "properties": {"namespace": _NS, "pod": _POD},
                "required": ["namespace"],
            },
            k8s_events,
        ),
        Tool(
            "pod_metrics",
            "Current restarts, memory working set and CPU for one pod, from Prometheus.",
            {
                "type": "object",
                "properties": {"namespace": _NS, "pod": {"type": "string"}},
                "required": ["namespace", "pod"],
            },
            pod_metrics,
        ),
        Tool(
            "pod_logs",
            "Recent log lines of a pod (or, with Loki, a namespace). For a crash-looping "
            "pod this includes the previous container's output, where the crash reason is.",
            {
                "type": "object",
                "properties": {
                    "namespace": _NS,
                    "pod": _POD,
                    "minutes": {"type": "integer", "description": "window, default 15"},
                },
                "required": ["namespace"],
            },
            pod_logs,
        ),
        Tool(
            "node_status",
            "Every node's Ready state, memory/disk/PID pressure and allocatable resources, "
            "plus recent Warning events about nodes. Check this when pods are Pending, "
            "evicted, or failing across many namespaces at once.",
            {"type": "object", "properties": {}},
            node_status,
        ),
        Tool(
            "deploy_history",
            "What changed: rollouts (new ReplicaSets, with images) in a namespace during "
            "the window. Check this whenever a failure could be caused by a deploy.",
            {
                "type": "object",
                "properties": {
                    "namespace": _NS,
                    "hours": {"type": "integer", "description": "default 24"},
                },
                "required": ["namespace"],
            },
            deploy_history,
        ),
    )
}


def schemas() -> list[dict]:
    return [t.schema() for t in TOOLS.values()]


async def run(ctx: ToolContext, name: str, arguments: dict) -> str:
    """Execute one tool call; never raise into the loop, never leak PII."""
    tool = TOOLS.get(name)
    if tool is None:
        return f"error: unknown tool {name!r}; available: {', '.join(TOOLS)}"
    try:
        out = await tool.fn(ctx, **arguments)
    except TypeError as exc:  # bad/missing arguments from the model
        return f"error: bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001 - a failed observation is data, not a crash
        logger.warning("tool %s failed: %s", name, exc)
        return f"error: {name} failed: {exc}"
    out = redact(str(out))
    if len(out) > MAX_TOOL_OUTPUT_CHARS:
        out = out[:MAX_TOOL_OUTPUT_CHARS] + "\n… (truncated)"
    return out
