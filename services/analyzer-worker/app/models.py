"""Domain models for the analysis pipeline.

The alert shape mirrors what ingest-api enqueues: one Alertmanager alert plus the
groupKey/receiver it stamps on before publishing (see ingest-api app/queue.py).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

APPROVAL_PREFIX = "REQUIRES APPROVAL: "

# Commands and plain-language verbs that change the cluster or its workloads.
# Read-only checks (get, describe, logs, top, rollout status/history, PromQL)
# deliberately do not match.
_DISRUPTIVE = re.compile(
    r"\b(?:"
    # global flags may sit between the binary and the verb: kubectl -n payments delete ...
    r"kubectl(?:\s+-\S+(?:\s+(?!-)\S+)?)*?\s+(?:delete|scale|rollout\s+(?:restart|undo|pause|resume)|apply|patch"
    r"|edit|drain|cordon|uncordon|taint|replace|set|exec|label|annotate|create|run|cp)"
    r"|helm(?:\s+-\S+(?:\s+(?!-)\S+)?)*?\s+(?:upgrade|install|uninstall|rollback|delete)"
    r"|terraform\s+(?:apply|destroy)|systemctl\s+(?:restart|stop|reload)|reboot|rm\s+-rf"
    r"|drop\s+(?:table|database)|truncate\s+table|delete\s+from"
    r")\b"
    r"|^\s*(?:restart|delete|remove|scale|roll\s*back|redeploy|re-deploy|kill|drain|cordon|evict|recreate"
    r"|increase|decrease|raise|lower|bump|change|update|set|patch|edit|apply|upgrade|downgrade|revert"
    r"|restore|rotate|recreate|disable|enable)\b",
    re.IGNORECASE,
)


def mark_if_disruptive(step: str) -> str:
    """Prefix a next step that would change something with REQUIRES APPROVAL."""
    if step.strip().upper().startswith(APPROVAL_PREFIX.strip()):
        return step
    return APPROVAL_PREFIX + step if _DISRUPTIVE.search(step) else step


class StreamAlert(BaseModel):
    """One alert as read back from the Redis stream payload."""

    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime | None = None
    endsAt: datetime | None = None
    generatorURL: str = ""
    fingerprint: str = ""
    groupKey: str = ""
    receiver: str = ""

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "UnknownAlert")

    @property
    def namespace(self) -> str:
        return self.labels.get("namespace", "")

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "unknown")

    def summary(self) -> str:
        """A compact human line describing the alert — always safe to deliver."""
        parts = [self.alertname]
        if self.namespace:
            parts.append(f"ns={self.namespace}")
        pod = self.labels.get("pod")
        if pod:
            parts.append(f"pod={pod}")
        parts.append(f"severity={self.severity}")
        return " · ".join(parts)


class ContextBundle(BaseModel):
    """The context an engineer would gather by hand, collected automatically.

    Kept deliberately small (ADR-0002 data minimization): only the window around
    the alert, with caps applied by the collectors.
    """

    log_lines: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    k8s_events: list[str] = Field(default_factory=list)
    sources_ok: list[str] = Field(default_factory=list)
    sources_failed: list[str] = Field(default_factory=list)

    def render(self) -> str:
        """Flatten to the text block handed to the model."""
        sections = []
        if self.k8s_events:
            sections.append("## Kubernetes events\n" + "\n".join(self.k8s_events))
        if self.metrics:
            sections.append("## Metrics\n" + "\n".join(self.metrics))
        if self.log_lines:
            sections.append("## Logs\n" + "\n".join(self.log_lines))
        if not sections:
            return "(no context could be collected)"
        return "\n\n".join(sections)


class Hypothesis(BaseModel):
    """The structured output of a single LLM call (ADR-0001).

    A hypothesis is cheap; knowing how to disprove it is the useful part
    (ADR-0004). Every hypothesis therefore carries the evidence that supports it,
    the cheapest observation that would prove it wrong, and its blast radius,
    which is a separate guardrail, not a ranking multiplier.
    """

    root_cause: str
    severity: str = "unknown"
    confidence: str = "medium"
    evidence: list[str] = Field(default_factory=list)
    disproof: str = ""
    blast_radius: str = "unknown"
    next_steps: list[str] = Field(default_factory=list)

    @field_validator("next_steps")
    @classmethod
    def _mark_disruptive_steps(cls, steps: list[str]) -> list[str]:
        """A step that changes the cluster is never presented as a casual check.

        The prompt asks the model to mark such steps itself; this is the
        deterministic net under it, so the property holds for every backend,
        for paste mode and for correlation revisions alike. At 03:00 an
        unmarked "restart the pod" reads like a diagnostic and gets run.
        """
        return [mark_if_disruptive(s) for s in steps]


class LLMResult(BaseModel):
    hypothesis: Hypothesis
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    backend: str = "unknown"


class IncidentStatus(str, Enum):
    ANALYZED = "analyzed"
    ANALYSIS_FAILED = "analysis_failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    # A member of an alert storm: same alertname+namespace as an incident
    # analysed moments ago (the leader). Recorded and counted, not analysed.
    GROUPED = "grouped"
    # A different alert in the namespace of an incident analysed moments ago:
    # attached to that leader; one revision over all of them follows (ADR-0006).
    CORRELATED = "correlated"


class Incident(BaseModel):
    """The persisted, deliverable result of processing one alert."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    fingerprint: str = ""
    alertname: str = ""
    namespace: str = ""
    severity: str = "unknown"
    alert_summary: str = ""
    status: IncidentStatus = IncidentStatus.ANALYZED
    hypothesis: Hypothesis | None = None
    failure_reason: str | None = None
    backend: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    # Wall clock from the alert firing in Alertmanager (startsAt) to the
    # hypothesis being ready. latency_ms is only the model call; this is the
    # number an engineering manager asks for. 0 when the alert has no startsAt.
    time_to_hypothesis_ms: int = 0
    # Storm bookkeeping: the leader incident this one was folded into, and how
    # many alerts the storm holds at the moment this record was written.
    grouped_into: str = ""
    storm_size: int = 0
    storm_pods: list[str] = Field(default_factory=list)
    # Correlation (ADR-0006): on a leader after the revision, the alerts the
    # revised hypothesis explains; on a member, the alerts attached so far.
    # revised_from keeps the cause the leader had before the revision.
    correlated_alerts: list[str] = Field(default_factory=list)
    revised_from: str = ""
    # Exactly what the model was shown, after redaction: the audit trail for
    # "what did it see when it said that". Empty when analysis never ran.
    context: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
