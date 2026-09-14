"""CloudWatch as a context source, for EKS teams with Container Insights.

Many teams on EKS have no Loki and no Prometheus: their pod logs go to
CloudWatch Logs through Fluent Bit (Container Insights), and their pod
metrics are in the ContainerInsights namespace. Without this collector the
reflex would see events only on exactly the clusters where AWS DevOps Agent
competes hardest.

Two read-only calls per alert:
- Logs Insights: a query over the application log group for the alerting
  pod's lines in the window. Logs Insights is asynchronous (start, poll),
  bounded here by a hard timeout.
- GetMetricData: restarts, CPU and memory utilisation for the pod from the
  ContainerInsights metrics namespace.

Credentials come from the pod's identity (EKS Pod Identity or IRSA), never
from configuration; the IAM policy is logs:StartQuery, logs:GetQueryResults
and cloudwatch:GetMetricData, all read. boto3 is synchronous, so each call
runs in a worker thread and never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import ContextBundle, StreamAlert

logger = logging.getLogger("analyzer-worker.cloudwatch")


def _alert_time(alert: StreamAlert) -> datetime:
    t = alert.startsAt or datetime.now(timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class CloudWatchLogsCollector:
    name = "cloudwatch-logs"

    def __init__(
        self,
        log_group: str,
        client=None,
        region: str = "",
        max_lines: int = 50,
        window_minutes: int = 15,
        query_timeout: float = 20.0,
        poll_interval: float = 1.0,
    ) -> None:
        self._log_group = log_group
        self._client = client  # inject a fake in tests
        self._region = region
        self._max = max_lines
        self._window = window_minutes
        self._timeout = query_timeout
        self._poll = poll_interval

    def _get_client(self):
        if self._client is None:  # pragma: no cover - real AWS path
            import boto3

            self._client = boto3.client("logs", region_name=self._region or None)
        return self._client

    @staticmethod
    def query_for(alert: StreamAlert) -> str:
        """Container Insights (Fluent Bit) puts pod metadata under `kubernetes.*`."""
        ns = alert.namespace or "default"
        pod = alert.labels.get("pod", "")
        where = f'kubernetes.namespace_name = "{ns}"'
        if pod:
            where += f' and kubernetes.pod_name = "{pod}"'
        return f"fields @timestamp, log | filter {where} | sort @timestamp desc"

    async def collect(self, alert: StreamAlert) -> ContextBundle:
        client = self._get_client()
        t = _alert_time(alert)
        start = int((t - timedelta(minutes=self._window)).timestamp())
        end = int((t + timedelta(minutes=5)).timestamp())
        started = await asyncio.to_thread(
            client.start_query,
            logGroupName=self._log_group,
            startTime=start,
            endTime=end,
            queryString=self.query_for(alert),
            limit=self._max,
        )
        query_id = started["queryId"]
        deadline = asyncio.get_running_loop().time() + self._timeout
        while True:
            result = await asyncio.to_thread(client.get_query_results, queryId=query_id)
            status = result.get("status")
            if status in ("Complete", "Failed", "Cancelled", "Timeout"):
                break
            if asyncio.get_running_loop().time() > deadline:
                try:
                    await asyncio.to_thread(client.stop_query, queryId=query_id)
                except Exception as exc:  # noqa: BLE001 - best effort; the timeout is the real error
                    logger.debug("stop_query failed: %s", exc)
                raise TimeoutError(f"Logs Insights query did not finish in {self._timeout:.0f}s")
            await asyncio.sleep(self._poll)
        if status != "Complete":
            raise RuntimeError(f"Logs Insights query ended with status {status}")
        lines = []
        for row in result.get("results", []):
            fields = {f["field"]: f["value"] for f in row}
            line = fields.get("log") or fields.get("@message") or ""
            if line:
                lines.append(line.rstrip())
        return ContextBundle(log_lines=lines[: self._max], sources_ok=[self.name])


class CloudWatchMetricsCollector:
    name = "cloudwatch-metrics"

    # ContainerInsights metric name → label, for the pod's ClusterName/Namespace/PodName.
    METRICS = (
        ("pod_number_of_container_restarts", "Sum"),
        ("pod_cpu_utilization", "Average"),
        ("pod_memory_utilization", "Average"),
    )

    def __init__(self, cluster_name: str, client=None, region: str = "", window_minutes: int = 15) -> None:
        self._cluster = cluster_name
        self._client = client
        self._region = region
        self._window = window_minutes

    def _get_client(self):
        if self._client is None:  # pragma: no cover - real AWS path
            import boto3

            self._client = boto3.client("cloudwatch", region_name=self._region or None)
        return self._client

    def queries_for(self, alert: StreamAlert) -> list[dict[str, Any]]:
        dims = [
            {"Name": "ClusterName", "Value": self._cluster},
            {"Name": "Namespace", "Value": alert.namespace or "default"},
            {"Name": "PodName", "Value": alert.labels.get("pod", "")},
        ]
        return [
            {
                "Id": f"m{i}",
                "MetricStat": {
                    "Metric": {"Namespace": "ContainerInsights", "MetricName": name, "Dimensions": dims},
                    "Period": 60,
                    "Stat": stat,
                },
            }
            for i, (name, stat) in enumerate(self.METRICS)
        ]

    async def collect(self, alert: StreamAlert) -> ContextBundle:
        if not alert.labels.get("pod"):
            return ContextBundle(sources_ok=[self.name])
        client = self._get_client()
        t = _alert_time(alert)
        resp = await asyncio.to_thread(
            client.get_metric_data,
            MetricDataQueries=self.queries_for(alert),
            StartTime=t - timedelta(minutes=self._window),
            EndTime=t + timedelta(minutes=5),
            ScanBy="TimestampDescending",
        )
        metrics = []
        names = dict(zip((f"m{i}" for i in range(len(self.METRICS))), (n for n, _ in self.METRICS)))
        for series in resp.get("MetricDataResults", []):
            values = series.get("Values") or []
            if not values:
                continue
            name = names.get(series.get("Id"), series.get("Id"))
            latest = values[0]
            metrics.append(f'{name}{{pod="{alert.labels.get("pod")}"}} = {latest:g}')
        return ContextBundle(metrics=metrics, sources_ok=[self.name])
