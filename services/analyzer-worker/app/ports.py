"""Ports — the interfaces the pipeline depends on.

Everything the Analyzer touches from the outside world (context collection, the
LLM, delivery, persistence, the budget) is a Protocol. Real adapters and test
fakes both satisfy these, which is what makes ADR-0002's promise literal:
swapping the LLM vendor, the notifier or the datastore is configuration, not a
change to the core pipeline.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ContextBundle, Incident, LLMResult, StreamAlert


@runtime_checkable
class Collector(Protocol):
    async def collect(self, alert: StreamAlert) -> ContextBundle: ...


@runtime_checkable
class LLMBackend(Protocol):
    name: str

    async def analyze(self, prompt: str) -> LLMResult: ...


@runtime_checkable
class Notifier(Protocol):
    async def notify(self, incident: Incident) -> None: ...


@runtime_checkable
class IncidentStore(Protocol):
    async def save(self, incident: Incident) -> None: ...


@runtime_checkable
class Budget(Protocol):
    async def has_budget(self) -> bool: ...

    async def add(self, usd: float) -> None: ...


@runtime_checkable
class Deduplicator(Protocol):
    async def is_duplicate(self, alert: StreamAlert) -> bool: ...


@runtime_checkable
class StormTracker(Protocol):
    async def track(self, alert: StreamAlert, incident_id: str): ...


@runtime_checkable
class CorrelationTracker(Protocol):
    async def track(self, alert: StreamAlert, incident_id: str, context: str): ...

    async def bundle(self, alert: StreamAlert): ...

    async def claim_revision(self, alert: StreamAlert, hold_seconds: float) -> bool: ...


class BackendError(RuntimeError):
    """Raised by an LLM backend when it cannot produce a hypothesis.

    The Analyzer treats this (and any other exception, and timeouts) as a
    best-effort miss: the incident is still recorded and delivered, marked
    analysis_failed, and never retried into a pile-up (ADR-0003).
    """
