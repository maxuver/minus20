"""Turn a real incident back into a replay scenario.

Every production incident should become a regression test for the triage
that handled it (the "resilience debt" rule: an incident without a
corresponding experiment is debt). The incident row already holds the alert
and the exact redacted context the model saw, and the replay harness reads
the same shape, so the export is a reshaping, not a reconstruction.

The engineer's verdict, when there is one, becomes the expected answer:
`/wrong <id> NetworkPolicy blocked egress` turns into keywords the grader
checks the next hypothesis against. Redacted data only, like everything else
that leaves the database.
"""

from __future__ import annotations

import json
import re
from typing import Any

_STOP = {
    "the", "a", "an", "is", "was", "were", "it", "its", "to", "of", "in", "on", "and", "or", "for",
    "by", "with", "that", "this", "not", "no", "be", "as", "at", "from", "because", "due", "which",
    # function words that carry no cause
    "after", "before", "could", "would", "should", "can", "will", "then", "when", "while", "into",
    "over", "under", "right", "just", "also", "very", "more", "than", "but", "only", "same", "each",
    "all", "any", "some", "has", "have", "had", "does", "did", "are", "been", "being", "there",
    "their", "them", "they", "you", "your", "our", "still", "again", "never", "without", "actually",
    # words every hypothesis contains, so they must not count as a match
    "log", "logs", "line", "lines", "error", "errors", "pod", "pods", "container", "containers",
    "issue", "problem", "cause", "caused", "fails", "failed", "failure", "failing", "involved",
    "real", "actual", "kubernetes", "cluster", "namespace", "restart", "restarts", "restarting",
}


def parse_context(rendered: str) -> dict[str, list[str]]:
    """Inverse of ContextBundle.render(): '## Kubernetes events' etc. back to lists."""
    sections = {"k8s_events": [], "metrics": [], "log_lines": []}
    names = {"## Kubernetes events": "k8s_events", "## Metrics": "metrics", "## Logs": "log_lines"}
    current: str | None = None
    for line in (rendered or "").splitlines():
        if line.strip() in names:
            current = names[line.strip()]
            continue
        if current and line.strip():
            sections[current].append(line.rstrip())
    return sections


def keywords_from(text: str, limit: int = 6) -> list[str]:
    """Distinctive words of a resolution, for the keyword grader.

    Only the first clause counts: engineers name the cause first and explain
    after a comma or semicolon, and the explanation tends to name the wrong
    hypothesis it corrects ("postgres unreachable; no secret involved, the
    image pulled fine"). Grading on the whole text scored a PASS for a repeat
    of the wrong answer because "secret" and "image" appeared in the verdict.
    """
    clause = re.split(r"[;.]|,\s", (text or "").lower(), maxsplit=1)[0]
    words = re.findall(r"[a-z0-9][a-z0-9_./:-]{2,}", clause)
    out: list[str] = []
    for w in words:
        if w in _STOP or w in out:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def incident_to_scenario(row: dict[str, Any]) -> dict[str, Any]:
    """Build the scenario JSON the replay harness loads (see scenarios/hard/)."""
    ctx = parse_context(row.get("context") or "")
    verdict, resolution = row.get("verdict"), (row.get("resolution") or "").strip()
    alertname = row.get("alertname") or "Alert"
    namespace = row.get("namespace") or ""
    labels = {"alertname": alertname, "severity": row.get("severity") or "warning"}
    if namespace:
        labels["namespace"] = namespace
    # The pod is not stored on the incident; it is usually named in the first
    # event line ("Warning BackOff pod/<name> ...").
    for line in ctx["k8s_events"]:
        m = re.search(r"\bpod/([a-z0-9][a-z0-9.-]*)", line)
        if m:
            labels["pod"] = m.group(1)
            break
    scenario: dict[str, Any] = {
        "name": f"{alertname.lower()}-{(row.get('id') or '')[:8]}",
        "source": "exported from a real incident by /scenario; redacted context, verbatim",
        "alert": {
            "status": "firing",
            "labels": labels,
            "annotations": {"summary": row.get("alert_summary") or alertname},
            "startsAt": (row.get("created_at").isoformat() if row.get("created_at") else None),
            "fingerprint": row.get("fingerprint") or "",
        },
        "context": ctx,
    }
    if verdict == "wrong" and resolution:
        scenario["why_hard"] = (
            f"The first hypothesis was '{row.get('root_cause')}'. The engineer recorded the real cause: {resolution}"
        )
        scenario["expected_keywords"] = keywords_from(resolution)
    elif verdict == "correct":
        scenario["expected_keywords"] = keywords_from(row.get("root_cause") or "")
        scenario["why_hard"] = f"Confirmed by the engineer{': ' + resolution if resolution else ''}."
    else:
        scenario["expected_keywords"] = []
        scenario["why_hard"] = "No engineer verdict yet; add expected_keywords by hand before grading."
    return scenario


def scenario_json(row: dict[str, Any]) -> str:
    return json.dumps(incident_to_scenario(row), ensure_ascii=False, indent=2)
