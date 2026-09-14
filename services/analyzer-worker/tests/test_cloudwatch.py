"""CloudWatch collectors with a fake boto3 client (no AWS needed).

CC-48  Logs Insights: the query targets the alerting pod, polling stops on
       Complete, lines come back capped and in order.
CC-49  A query that never completes raises after the timeout and is stopped.
CC-50  Metrics: three ContainerInsights series for the pod, latest value each;
       an alert without a pod yields no metrics and no call.
CC-51  Both are selectable by config and refuse to build without their setting.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.cloudwatch import CloudWatchLogsCollector, CloudWatchMetricsCollector
from app.collectors import get_collector
from app.config import Settings
from app.models import StreamAlert

ALERT = StreamAlert(
    labels={"alertname": "KubePodCrashLooping", "namespace": "payments", "pod": "billing-api-1"},
    startsAt=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
)


class FakeLogs:
    def __init__(self, statuses=("Running", "Complete"), rows=None):
        self.statuses = list(statuses)
        self.rows = rows or []
        self.calls = []

    def start_query(self, **kw):
        self.calls.append(("start", kw))
        return {"queryId": "q1"}

    def get_query_results(self, queryId):
        self.calls.append(("poll", queryId))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status, "results": self.rows if status == "Complete" else []}

    def stop_query(self, queryId):
        self.calls.append(("stop", queryId))
        return {"success": True}


async def test_logs_insights_query_targets_the_pod_and_returns_lines():  # CC-48
    rows = [
        [{"field": "@timestamp", "value": "2026-09-14 12:00:05"}, {"field": "log", "value": "FATAL secret db-credentials not found"}],
        [{"field": "@timestamp", "value": "2026-09-14 12:00:04"}, {"field": "log", "value": "starting"}],
    ]
    fake = FakeLogs(rows=rows)
    c = CloudWatchLogsCollector("/aws/containerinsights/prod/application", client=fake, poll_interval=0)
    bundle = await c.collect(ALERT)
    start_kw = fake.calls[0][1]
    assert start_kw["logGroupName"] == "/aws/containerinsights/prod/application"
    assert 'kubernetes.namespace_name = "payments"' in start_kw["queryString"]
    assert 'kubernetes.pod_name = "billing-api-1"' in start_kw["queryString"]
    assert start_kw["startTime"] < start_kw["endTime"]
    assert bundle.log_lines == ["FATAL secret db-credentials not found", "starting"]
    assert bundle.sources_ok == ["cloudwatch-logs"]
    assert [c for c, _ in fake.calls].count("poll") == 2  # Running, then Complete


async def test_logs_insights_times_out_and_stops_the_query():  # CC-49
    fake = FakeLogs(statuses=("Running",))
    c = CloudWatchLogsCollector("lg", client=fake, poll_interval=0, query_timeout=0.05)
    with pytest.raises(TimeoutError):
        await c.collect(ALERT)
    assert ("stop", "q1") in fake.calls


class FakeCW:
    def __init__(self):
        self.calls = []

    def get_metric_data(self, **kw):
        self.calls.append(kw)
        return {
            "MetricDataResults": [
                {"Id": "m0", "Values": [7.0, 6.0]},
                {"Id": "m1", "Values": [93.5]},
                {"Id": "m2", "Values": []},
            ]
        }


async def test_container_insights_metrics_for_the_pod():  # CC-50
    fake = FakeCW()
    c = CloudWatchMetricsCollector("prod", client=fake)
    bundle = await c.collect(ALERT)
    dims = {d["Name"]: d["Value"] for d in fake.calls[0]["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"]}
    assert dims == {"ClusterName": "prod", "Namespace": "payments", "PodName": "billing-api-1"}
    assert bundle.metrics == [
        'pod_number_of_container_restarts{pod="billing-api-1"} = 7',
        'pod_cpu_utilization{pod="billing-api-1"} = 93.5',
    ]
    no_pod = await c.collect(StreamAlert(labels={"alertname": "X", "namespace": "payments"}))
    assert no_pod.metrics == [] and len(fake.calls) == 1


def test_cloudwatch_collectors_are_selectable_and_need_their_settings():  # CC-51
    with pytest.raises(ValueError):
        get_collector(Settings(collectors="cloudwatch-logs"))
    agg = get_collector(Settings(collectors="k8s-events,cloudwatch-logs,cloudwatch-metrics",
                                 cloudwatch_log_group="/aws/containerinsights/prod/application",
                                 cloudwatch_cluster_name="prod"))
    names = {c.name for c in agg._collectors}
    assert names == {"k8s-events", "cloudwatch-logs", "cloudwatch-metrics"}
