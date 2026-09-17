import pytest

from app.models import StreamAlert


@pytest.fixture
def raw_payload() -> dict:
    """A stream payload shaped exactly as ingest-api enqueues it."""
    return {
        "status": "firing",
        "labels": {
            "alertname": "KubePodCrashLooping",
            "severity": "warning",
            "namespace": "demo",
            "pod": "billing-api-7f9c6d5b8-x2j4q",
            "container": "billing-api",
        },
        "annotations": {
            "description": "Pod demo/billing-api is in CrashLoopBackOff.",
            "summary": "Pod is crash looping.",
        },
        "startsAt": "2026-07-28T03:12:45Z",
        "fingerprint": "b0e0b3cbc9d21c48",
        "groupKey": '{}:{alertname="KubePodCrashLooping"}',
        "receiver": "minus20",
    }


@pytest.fixture
def alert(raw_payload) -> StreamAlert:
    return StreamAlert(**raw_payload)
