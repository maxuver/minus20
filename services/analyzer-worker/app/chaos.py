"""Break something on purpose, then page the reflex about it (demo mode).

An unattended demo cluster needs incidents to happen without a person at the
keyboard. This job runs from a CronJob: it removes the previous chaos pod,
creates a new one that fails in one of a few realistic ways, waits until the
failure is visible in the cluster (restarts, or an image that will not pull),
and posts the Alertmanager-shaped alert to ingest-api. From there everything
is the real path: collectors see a real pod, real events, real logs.

This is the ONE component that writes to the cluster, and it is not the
product: it runs under its own ServiceAccount with create/delete on pods in
its own namespace, exists only behind `demo.chaos.enabled`, and the product's
read-only ServiceAccount never gains a verb because of it.

    python -m app.chaos [--mode NAME] [--once]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("minus20.chaos")

CHAOS_LABEL = "minus20.dev/chaos"

# name -> (alertname, container spec pieces, what a human would say it is)
MODES: dict[str, dict[str, Any]] = {
    "missing-secret": {
        "alertname": "KubePodCrashLooping",
        "image": "busybox:1.36",
        "command": ["sh", "-c", "echo 'FATAL secret db-credentials not found'; sleep 2; exit 1"],
        "story": "the app cannot find a Secret it needs",
    },
    "db-unreachable": {
        "alertname": "KubePodCrashLooping",
        "image": "busybox:1.36",
        "command": ["sh", "-c", "echo 'ERROR could not connect to postgres:5432: connection refused'; sleep 2; exit 1"],
        "story": "the database the app depends on is not answering",
    },
    "wrong-host": {
        "alertname": "KubePodCrashLooping",
        "image": "busybox:1.36",
        "command": ["sh", "-c", "echo 'FATAL could not translate host name \"postgres-v1\" to address: Name does not resolve'; sleep 2; exit 1"],
        "story": "a config points at a host that no longer exists",
    },
    "oom": {
        "alertname": "KubePodCrashLooping",
        "image": "busybox:1.36",
        "command": ["sh", "-c", "echo 'INFO loading 64 MiB working set'; head -c 64m /dev/zero | tail"],
        "memory_limit": "24Mi",
        "story": "the container is OOMKilled under a limit that is too small",
    },
    "bad-image": {
        "alertname": "KubePodNotReady",
        "image": "busybox:this-tag-does-not-exist",
        "command": ["sh", "-c", "sleep 3600"],
        "story": "a rollout references an image tag that was never pushed",
    },
}


def pod_manifest(mode: str, name: str, namespace: str) -> dict[str, Any]:
    spec = MODES[mode]
    container: dict[str, Any] = {"name": "app", "image": spec["image"], "command": spec["command"]}
    if "memory_limit" in spec:
        container["resources"] = {"limits": {"memory": spec["memory_limit"]}, "requests": {"memory": spec["memory_limit"]}}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": {CHAOS_LABEL: "true", "app": name, "chaos-mode": mode}},
        "spec": {"restartPolicy": "Always", "containers": [container]},
    }


def alert_payload(mode: str, pod: str, namespace: str, now: datetime | None = None) -> dict[str, Any]:
    """The webhook Alertmanager would send for this pod (v4 shape)."""
    now = now or datetime.now(timezone.utc)
    alertname = MODES[mode]["alertname"]
    labels = {"alertname": alertname, "namespace": namespace, "pod": pod, "container": "app", "severity": "critical"}
    if alertname == "KubePodCrashLooping":
        description = f"Pod {namespace}/{pod} (app) is in waiting state (reason: CrashLoopBackOff)."
    else:
        description = f"Pod {namespace}/{pod} has been in a non-ready state for longer than 15 minutes."
    return {
        "version": "4", "groupKey": f'{{}}:{{alertname="{alertname}"}}', "status": "firing", "receiver": "minus20",
        "groupLabels": {"alertname": alertname}, "commonLabels": labels,
        "commonAnnotations": {"description": description, "summary": description},
        "externalURL": "http://chaos.minus20.local",
        "alerts": [{
            "status": "firing", "labels": labels, "annotations": {"description": description, "summary": description},
            "startsAt": now.isoformat(), "endsAt": "0001-01-01T00:00:00Z", "generatorURL": "http://chaos.minus20.local",
            "fingerprint": f"chaos-{pod}",
        }],
    }


def failure_visible(pod: dict[str, Any]) -> bool:
    """True once the cluster itself shows the failure: a restart, or a pull that will not succeed."""
    for cs in (pod.get("status") or {}).get("containerStatuses") or []:
        if int(cs.get("restartCount") or 0) >= 1:
            return True
        waiting = (cs.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") in ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff"):
            return True
    return False


class Chaos:
    def __init__(self, api, poster, namespace: str, ingest_url: str, wait_seconds: float = 180.0, poll: float = 5.0) -> None:
        self._api = api  # CoreV1Api-like: list_namespaced_pod, create_namespaced_pod, delete_namespaced_pod, read_namespaced_pod
        self._post = poster  # async callable(url, payload)
        self._ns = namespace
        self._ingest = ingest_url
        self._wait = wait_seconds
        self._poll = poll

    async def cleanup(self) -> int:
        pods = await self._api.list_namespaced_pod(self._ns, label_selector=f"{CHAOS_LABEL}=true")
        names = [p.metadata.name for p in pods.items]
        for name in names:
            await self._api.delete_namespaced_pod(name, self._ns)
        return len(names)

    async def run(self, mode: str | None = None) -> str:
        # which fault to inject, not anything security-relevant
        mode = mode or random.choice(sorted(MODES))  # nosec B311
        removed = await self.cleanup()
        name = f"chaos-{mode}-{datetime.now(timezone.utc).strftime('%H%M')}"
        await self._api.create_namespaced_pod(self._ns, pod_manifest(mode, name, self._ns))
        logger.info("chaos: removed %d old pod(s), created %s (%s)", removed, name, MODES[mode]["story"])
        deadline = time.monotonic() + self._wait
        while time.monotonic() < deadline:
            pod = await self._api.read_namespaced_pod(name, self._ns)
            raw = pod if isinstance(pod, dict) else self._api.api_client.sanitize_for_serialization(pod)
            if failure_visible(raw):
                break
            await asyncio.sleep(self._poll)
        else:
            logger.warning("chaos: %s showed no failure within %.0fs; alerting anyway", name, self._wait)
        await self._post(self._ingest, alert_payload(mode, name, self._ns))
        logger.info("chaos: alert posted for %s to %s", name, self._ingest)
        return name


async def _http_post(url: str, payload: dict[str, Any]) -> None:  # pragma: no cover - real network
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, content=json.dumps(payload), headers={"Content-Type": "application/json"})
        resp.raise_for_status()


async def _main(mode: str | None) -> None:  # pragma: no cover - real cluster path
    from kubernetes_asyncio import client, config

    from .logsafe import configure_logging

    configure_logging(os.environ.get("MINUS20_LOG_LEVEL", "INFO"))
    try:
        config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()
    namespace = os.environ.get("MINUS20_CHAOS_NAMESPACE") or "minus20"
    ingest = os.environ.get("MINUS20_CHAOS_INGEST_URL") or "http://localhost:8080/webhook/alertmanager"
    async with client.ApiClient() as api_client:
        api = client.CoreV1Api(api_client)
        await Chaos(api, _http_post, namespace, ingest).run(mode)


def main() -> None:  # pragma: no cover - CLI entrypoint
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=sorted(MODES), default=None, help="which fault; random when omitted")
    args = ap.parse_args()
    asyncio.run(_main(args.mode))


if __name__ == "__main__":  # pragma: no cover
    main()
