"""Correctness checks for the demo chaos job.

CC-60  Every mode yields a pod that fails on its own and an Alertmanager-shaped
       alert naming that pod; the job removes the previous chaos pod, waits for
       the cluster to show the failure, then posts the alert. Chaos pods are
       labelled so nothing else is ever deleted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.chaos import CHAOS_LABEL, MODES, Chaos, alert_payload, failure_visible, pod_manifest
from app.models import StreamAlert


@pytest.mark.parametrize("mode", sorted(MODES))
def test_every_mode_builds_a_failing_pod_and_a_valid_alert(mode):  # CC-60
    pod = pod_manifest(mode, f"chaos-{mode}-0300", "demo")
    assert pod["metadata"]["labels"][CHAOS_LABEL] == "true"
    c = pod["spec"]["containers"][0]
    assert c["image"] and c["command"]
    if mode == "oom":
        assert c["resources"]["limits"]["memory"] == "24Mi"
    payload = alert_payload(mode, f"chaos-{mode}-0300", "demo")
    assert payload["version"] == "4" and payload["status"] == "firing"  # the v4 webhook shape ingest-api accepts
    alert = StreamAlert.model_validate(payload["alerts"][0])  # and what the worker reads back
    assert alert.startsAt is not None and alert.fingerprint == f"chaos-chaos-{mode}-0300"
    assert alert.labels["pod"] == f"chaos-{mode}-0300" and alert.labels["namespace"] == "demo"
    assert alert.labels["alertname"] == MODES[mode]["alertname"]


def test_failure_is_visible_on_restart_or_pull_error():
    assert not failure_visible({"status": {"containerStatuses": [{"restartCount": 0, "state": {"running": {}}}]}})
    assert failure_visible({"status": {"containerStatuses": [{"restartCount": 2, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}})
    assert failure_visible({"status": {"containerStatuses": [{"restartCount": 0, "state": {"waiting": {"reason": "ImagePullBackOff"}}}]}})


class FakeApi:
    def __init__(self) -> None:
        self.pods = {"chaos-old-0200": {CHAOS_LABEL: "true"}, "billing-api": {}}
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.reads = 0

    async def list_namespaced_pod(self, ns, label_selector=""):
        key, _, value = label_selector.partition("=")
        items = [SimpleNamespace(metadata=SimpleNamespace(name=n)) for n, labels in self.pods.items() if labels.get(key) == value]
        return SimpleNamespace(items=items)

    async def delete_namespaced_pod(self, name, ns):
        self.deleted.append(name)
        self.pods.pop(name, None)

    async def create_namespaced_pod(self, ns, body):
        self.created.append(body)
        self.pods[body["metadata"]["name"]] = body["metadata"]["labels"]

    async def read_namespaced_pod(self, name, ns):
        self.reads += 1
        restarts = 0 if self.reads < 3 else 2  # the third look shows the crash
        return {"metadata": {"name": name}, "status": {"containerStatuses": [{"restartCount": restarts, "state": {}}]}}


async def test_job_replaces_the_previous_pod_waits_then_alerts():  # CC-60
    api, posted = FakeApi(), []

    async def poster(url, payload):
        posted.append((url, payload))

    job = Chaos(api, poster, "demo", "http://ingest:8080/webhook/alertmanager", wait_seconds=5, poll=0)
    name = await job.run("db-unreachable")
    assert api.deleted == ["chaos-old-0200"]  # only the labelled pod, never billing-api
    assert api.created[0]["metadata"]["name"] == name and name.startswith("chaos-db-unreachable-")
    assert api.reads == 3  # waited until the failure was visible
    (url, payload), = posted
    assert url.endswith("/webhook/alertmanager")
    assert payload["alerts"][0]["labels"]["pod"] == name and payload["alerts"][0]["labels"]["alertname"] == "KubePodCrashLooping"
