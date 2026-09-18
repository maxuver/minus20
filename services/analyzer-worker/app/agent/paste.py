"""Paste mode: `kubectl describe` / `kubectl logs` output in, a hypothesis out.

The fastest way for someone to see what this does is not `helm install`; it
is pasting the output they already have on screen at 03:00. The pasted text
is turned into the same alert + context shape the reflex pipeline works on,
redacted the same way, and answered by the same one-call backend, so the
answer they get is exactly what the installed product would have sent to
the chat forty seconds after the alert. Nothing is stored and no tool runs:
paste mode never touches a cluster.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from ..models import ContextBundle, StreamAlert

MAX_LINES = 120
MIN_LINES = 3

_EVENT = re.compile(r"^\s*(Warning|Normal)\s+\S+", re.IGNORECASE)
_METRIC = re.compile(r"^\s*[a-zA-Z_:][\w:]*(\{[^}]*\})?\s*=\s*-?\d")
_MARKERS = (
    "crashloopbackoff", "back-off", "oomkilled", "imagepullbackoff", "errimagepull", "failedscheduling",
    "failedmount", "exit code", "reason:", "state:", "events:", "restart", "warning", "error", "fatal",
    "panic", "traceback", "exception", "denied", "timeout", "refused", "unavailable", "pending",
    "liveness", "readiness", "not found", "failed",
)
_REASON_TO_ALERT = {
    "crashloopbackoff": "KubePodCrashLooping",
    "oomkilled": "KubePodCrashLooping",
    "error": "KubePodCrashLooping",
    "imagepullbackoff": "KubePodNotReady",
    "errimagepull": "KubePodNotReady",
    "failedscheduling": "KubePodNotReady",
    "pending": "KubePodNotReady",
    "containercreating": "KubePodNotReady",
    "failedmount": "KubePodNotReady",
}


def looks_like_paste(text: str) -> bool:
    """Multi-line operational output, not a question. Conservative on purpose:
    a two-line question with the word 'error' in it is still a question."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < MIN_LINES or text.lstrip().startswith("/"):
        return False
    low = text.lower()
    hits = sum(1 for m in _MARKERS if m in low)
    return hits >= 2 or len(lines) >= 12


def _first(pattern: str, text: str, flags: int = re.IGNORECASE | re.MULTILINE) -> str:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else ""


def parse_paste(text: str) -> tuple[StreamAlert, ContextBundle]:
    """Best-effort shaping of pasted text into what the pipeline expects.

    `kubectl describe pod` gives Name/Namespace/Reason and an Events section;
    `kubectl get events` gives Warning/Normal lines; `kubectl logs` gives
    everything else. Lines are capped; unknown lines count as logs, which is
    the safe default: the model reads them as untrusted context either way.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()][:MAX_LINES]
    joined = "\n".join(lines)
    pod = _first(r"^\s*Name:\s+(\S+)", joined) or _first(r"\bpod/([a-z0-9][a-z0-9.-]*)", joined)
    namespace = _first(r"^\s*Namespace:\s+(\S+)", joined)
    container = _first(r"^\s*Container(?:s)?:\s*\n?\s*([a-z0-9][a-z0-9-]*):", joined)
    low = joined.lower()
    reason = ""
    for key in _REASON_TO_ALERT:
        if key in low:
            reason = key
            break
    alertname = _REASON_TO_ALERT.get(reason, "PastedIncident")
    labels = {"alertname": alertname, "severity": "warning", "source": "paste"}
    if namespace:
        labels["namespace"] = namespace
    if pod:
        labels["pod"] = pod
    if container:
        labels["container"] = container
    summary = f"Pasted by the engineer: {alertname}" + (f" on {pod}" if pod else "")
    alert = StreamAlert(
        labels=labels,
        annotations={"summary": summary, "description": "Context pasted into the chat, not collected from a cluster."},
        startsAt=datetime.now(timezone.utc),
        fingerprint="paste-" + hashlib.sha1(joined.encode("utf-8"), usedforsecurity=False).hexdigest()[:12],
    )
    events, metrics, logs = [], [], []
    in_events = False
    for ln in lines:
        if re.match(r"^\s*Events:", ln, re.IGNORECASE):
            in_events = True
            continue
        if _EVENT.match(ln) or (in_events and re.match(r"^\s*\S+\s+\S+\s+\d+", ln)):
            events.append(ln.strip())
        elif _METRIC.match(ln):
            metrics.append(ln.strip())
        else:
            logs.append(ln)
    return alert, ContextBundle(k8s_events=events, metrics=metrics, log_lines=logs, sources_ok=["paste"])
