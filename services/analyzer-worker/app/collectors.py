"""Context collectors — gather what an engineer would gather by hand.

The set of collectors is fixed in code (ADR-0001): the model does not choose what
to fetch at runtime, which is what keeps cost and latency per alert predictable.

This iteration ships:
- StubCollector — deterministic synthetic context, used for tests, replay and the
  offline demo.
- AggregateCollector — runs several collectors, tolerating per-source failure so
  one dead datasource degrades context rather than failing the whole analysis.

Real Loki / Prometheus / Kubernetes-events collectors implement the same
`collect()` port and drop in without touching the pipeline (see ports.Collector).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .config import Settings, settings
from .models import ContextBundle, StreamAlert
from .ports import Collector

logger = logging.getLogger("analyzer-worker.collectors")


def _alert_time(alert: StreamAlert) -> datetime:
    """The timestamp to center context collection on."""
    return alert.startsAt or datetime.now(timezone.utc)


class StubCollector:
    """Deterministic context derived from the alert itself."""

    name = "stub"

    async def collect(self, alert: StreamAlert) -> ContextBundle:
        pod = alert.labels.get("pod", "unknown-pod")
        ns = alert.namespace or "default"
        reason = alert.annotations.get("description", alert.alertname)
        return ContextBundle(
            k8s_events=[
                f"{ns}/{pod}: {reason}",
                f"{ns}/{pod}: Back-off restarting failed container",
            ],
            metrics=[f'container_restarts{{pod="{pod}"}} = 7 over last 10m'],
            log_lines=[
                "ERROR unable to connect to upstream dependency",
                "traceback (most recent call last): ...",
            ],
            sources_ok=[self.name],
        )


class K8sEventsCollector:
    """Read Kubernetes events for the alerting object — what `kubectl describe`
    would show an engineer: BackOff, OOMKilled, FailedScheduling, probe failures.

    Read-only by design (ADR-0004 thread): the ServiceAccount needs only `get`/
    `list` on events. The API client is created lazily so tests inject a fake and
    offline runs never touch a cluster.
    """

    name = "k8s-events"

    def __init__(self, api=None, max_events: int = 20) -> None:
        self._api = api  # inject a CoreV1Api-like object in tests
        self._max = max_events
        self._api_client = None
        self._owns_client = False

    async def _get_api(self):
        if self._api is None:  # pragma: no cover - real cluster path
            from kubernetes_asyncio import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                await config.load_kube_config()
            self._api_client = client.ApiClient()
            self._api = client.CoreV1Api(self._api_client)
            self._owns_client = True
        return self._api

    async def collect(self, alert: StreamAlert) -> ContextBundle:
        ns = alert.namespace or "default"
        api = await self._get_api()
        resp = await api.list_namespaced_event(ns)
        items = list(resp.items)

        # If we know the involved pod, keep only its events (data minimization,
        # ADR-0002). Fall back to namespace-wide if that leaves nothing.
        pod = alert.labels.get("pod")
        if pod:
            scoped = [e for e in items if getattr(e.involved_object, "name", None) == pod]
            items = scoped or items

        # Warnings are the useful signal — surface them first, then cap.
        warnings = [e for e in items if e.type == "Warning"]
        others = [e for e in items if e.type != "Warning"]
        chosen = (warnings + others)[: self._max]
        return ContextBundle(
            k8s_events=[self._format(e) for e in chosen],
            sources_ok=[self.name],
        )

    @staticmethod
    def _format(event) -> str:
        io = event.involved_object
        kind = (getattr(io, "kind", "") or "").lower()
        name = getattr(io, "name", "") or ""
        obj = f"{kind}/{name}".strip("/")
        count = getattr(event, "count", None)
        cnt = f" x{count}" if count and count > 1 else ""
        return f"{event.type} {event.reason} {obj}{cnt}: {event.message}".strip()

    async def aclose(self) -> None:
        if self._owns_client and self._api_client is not None:
            await self._api_client.close()


class K8sPodLogsCollector:
    """The alerting pod's own output through the Kubernetes API, no Loki needed.

    For a crash-looping pod the reason is in the *previous* container's
    output, so both the current and the previous container are read. On a
    cluster with only events, the 7B model guessed "image pull issue" for a
    pod whose last line was "could not connect to postgres:5432". Read-only:
    needs get on pods/log, which the chart grants.
    """

    name = "k8s-logs"

    def __init__(self, api=None, tail_lines: int = 50) -> None:
        self._api = api
        self._tail = tail_lines
        self._api_client = None

    async def _get_api(self):
        if self._api is None:  # pragma: no cover - real cluster path
            from kubernetes_asyncio import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                await config.load_kube_config()
            self._api_client = client.ApiClient()
            self._api = client.CoreV1Api(self._api_client)
        return self._api

    async def collect(self, alert: StreamAlert) -> ContextBundle:
        pod = alert.labels.get("pod")
        if not pod:
            return ContextBundle(sources_ok=[self.name])  # nothing to read for a non-pod alert
        ns = alert.namespace or "default"
        api = await self._get_api()
        lines: list[str] = []
        for previous in (True, False):
            try:
                text = await api.read_namespaced_pod_log(pod, ns, tail_lines=self._tail, previous=previous)
            except Exception as exc:
                if not previous:
                    raise
                logger.debug("no previous container log for %s/%s: %s", ns, pod, exc)
                continue
            if text and text.strip():
                label = "previous container" if previous else "current container"
                lines.append(f"--- {label} ({pod})")
                lines.extend(text.strip().splitlines()[-self._tail :])
        return ContextBundle(log_lines=lines, sources_ok=[self.name])


class PrometheusCollector:
    """Query Prometheus for the metrics around the alert — restarts, memory and
    CPU pressure for the alerting pod. Instant queries at the alert timestamp.

    Read-only HTTP. The query templates are fixed in code (ADR-0001), substituted
    with the alert's namespace/pod; override the set per deployment if needed.
    """

    name = "prometheus"

    DEFAULT_QUERIES = (
        'kube_pod_container_status_restarts_total{namespace="%(namespace)s",pod="%(pod)s"}',
        'container_memory_working_set_bytes{namespace="%(namespace)s",pod="%(pod)s"}',
        (
            'sum(rate(container_cpu_usage_seconds_total'
            '{namespace="%(namespace)s",pod="%(pod)s"}[5m]))'
        ),
    )

    def __init__(self, url: str, client=None, queries: tuple[str, ...] | None = None) -> None:
        self._url = url
        self._client = client  # inject httpx.AsyncClient in tests
        self._queries = queries or self.DEFAULT_QUERIES

    async def collect(self, alert: StreamAlert) -> ContextBundle:
        import httpx

        ctx = {"namespace": alert.namespace or "default", "pod": alert.labels.get("pod", "")}
        when = str(_alert_time(alert).timestamp())
        client = self._client or httpx.AsyncClient(base_url=self._url, timeout=10.0)
        metrics: list[str] = []
        try:
            for template in self._queries:
                query = template % ctx
                resp = await client.get("/api/v1/query", params={"query": query, "time": when})
                resp.raise_for_status()
                for series in resp.json().get("data", {}).get("result", []):
                    metrics.append(self._format(query, series))
        finally:
            if self._client is None:
                await client.aclose()
        return ContextBundle(metrics=metrics, sources_ok=[self.name])

    @staticmethod
    def _format(query: str, series: dict) -> str:
        metric = series.get("metric", {})
        name = metric.get("__name__", "")
        value = (series.get("value") or [None, ""])[1]
        labels = ",".join(f'{k}="{v}"' for k, v in metric.items() if k != "__name__")
        head = f"{name}{{{labels}}}" if name else query
        return f"{head} = {value}"


class LokiCollector:
    """Query Loki for the logs of the alerting object in a window around the
    alert. Read-only HTTP; line count and time window are capped (ADR-0002).
    """

    name = "loki"

    def __init__(
        self, url: str, client=None, max_lines: int = 50, window_minutes: int = 15
    ) -> None:
        self._url = url
        self._client = client  # inject httpx.AsyncClient in tests
        self._max = max_lines
        self._window = window_minutes

    def _selector(self, alert: StreamAlert) -> str:
        parts = [f'namespace="{alert.namespace or "default"}"']
        pod = alert.labels.get("pod")
        if pod:
            parts.append(f'pod="{pod}"')
        return "{" + ", ".join(parts) + "}"

    async def collect(self, alert: StreamAlert) -> ContextBundle:
        import httpx

        t = _alert_time(alert)
        start = int((t - timedelta(minutes=self._window)).timestamp() * 1e9)
        end = int((t + timedelta(minutes=5)).timestamp() * 1e9)
        client = self._client or httpx.AsyncClient(base_url=self._url, timeout=10.0)
        lines: list[str] = []
        try:
            resp = await client.get(
                "/loki/api/v1/query_range",
                params={
                    "query": self._selector(alert),
                    "start": str(start),
                    "end": str(end),
                    "limit": self._max,
                    "direction": "backward",
                },
            )
            resp.raise_for_status()
            for stream in resp.json().get("data", {}).get("result", []):
                for _ts, line in stream.get("values", []):
                    lines.append(line)
        finally:
            if self._client is None:
                await client.aclose()
        return ContextBundle(log_lines=lines[: self._max], sources_ok=[self.name])


class AggregateCollector:
    """Fan out to several collectors; never let one failure sink the rest."""

    def __init__(self, collectors: list[Collector]) -> None:
        self._collectors = collectors

    async def collect(self, alert: StreamAlert) -> ContextBundle:
        merged = ContextBundle()
        for collector in self._collectors:
            name = getattr(collector, "name", collector.__class__.__name__)
            try:
                part = await collector.collect(alert)
            except Exception as exc:  # noqa: BLE001 - one bad source must not fail the analysis
                logger.warning("collector %s failed: %s", name, exc)
                merged.sources_failed.append(name)
                continue
            merged.log_lines.extend(part.log_lines)
            merged.metrics.extend(part.metrics)
            merged.k8s_events.extend(part.k8s_events)
            merged.sources_ok.extend(part.sources_ok or [name])
            merged.sources_failed.extend(part.sources_failed)
        return merged


def _cloudwatch_logs(cfg):
    from .cloudwatch import CloudWatchLogsCollector

    if not cfg.cloudwatch_log_group:
        raise ValueError("cloudwatch-logs needs SENTINELOPS_CLOUDWATCH_LOG_GROUP")
    return CloudWatchLogsCollector(
        cfg.cloudwatch_log_group, region=cfg.aws_region,
        max_lines=cfg.loki_max_lines, window_minutes=cfg.loki_window_minutes,
    )


def _cloudwatch_metrics(cfg):
    from .cloudwatch import CloudWatchMetricsCollector

    if not cfg.cloudwatch_cluster_name:
        raise ValueError("cloudwatch-metrics needs SENTINELOPS_CLOUDWATCH_CLUSTER_NAME")
    return CloudWatchMetricsCollector(
        cfg.cloudwatch_cluster_name, region=cfg.aws_region, window_minutes=cfg.loki_window_minutes
    )


_REGISTRY = {
    "stub": lambda cfg: StubCollector(),
    "k8s-events": lambda cfg: K8sEventsCollector(max_events=cfg.k8s_max_events),
    "k8s-logs": lambda cfg: K8sPodLogsCollector(tail_lines=cfg.loki_max_lines),
    "prometheus": lambda cfg: PrometheusCollector(cfg.prometheus_url),
    "loki": lambda cfg: LokiCollector(
        cfg.loki_url, max_lines=cfg.loki_max_lines, window_minutes=cfg.loki_window_minutes
    ),
    "cloudwatch-logs": _cloudwatch_logs,
    "cloudwatch-metrics": _cloudwatch_metrics,
}


def get_collector(cfg: Settings = settings) -> Collector:
    """Build the configured collectors (comma-separated `SENTINELOPS_COLLECTORS`).

    Wrapped in AggregateCollector so one dead datasource degrades context rather
    than failing the analysis, and so new collectors are added by config, not code.
    """
    names = [n.strip() for n in cfg.collectors.split(",") if n.strip()]
    built = []
    for name in names:
        factory = _REGISTRY.get(name)
        if factory is None:
            raise ValueError(f"unknown collector: {name!r} (known: {sorted(_REGISTRY)})")
        built.append(factory(cfg))
    return AggregateCollector(built or [StubCollector()])
