"""Domain models for the analysis pipeline.

The alert shape mirrors what ingest-api enqueues: one Alertmanager alert plus the
groupKey/receiver it stamps on before publishing (see ingest-api app/queue.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


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
    # Exactly what the model was shown, after redaction: the audit trail for
    # "what did it see when it said that". Empty when analysis never ran.
    context: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
