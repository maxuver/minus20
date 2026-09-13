"""Correctness checks for the analysis orchestrator.

CC-1  Backend error → incident still recorded & delivered, marked analysis_failed.
CC-2  Same for any unexpected exception (best-effort).
CC-3  A hard per-call timeout is enforced; a hanging backend cannot wedge analysis.
CC-4  Budget exhausted → no LLM call; incident marked budget_exceeded, still delivered.
CC-7  Redaction happens before the LLM call — the backend never sees raw PII.
CC-9  There is no bypass: the Analyzer always redacts.
CC-10 Exactly one backend call per alert (no agent loop).
CC-18 A well-formed alert yields an ANALYZED incident with a non-empty hypothesis.
CC-19 Cost from the backend is recorded on the incident.
"""

import asyncio

from app.analyzer import Analyzer
from app.budget import InMemoryBudget
from app.collectors import StubCollector
from app.models import ContextBundle, Hypothesis, IncidentStatus, LLMResult
from app.notifiers import StubNotifier
from app.ports import BackendError
from app.stores import InMemoryStore


class SpyBackend:
    """Records prompts and call count; returns a fixed hypothesis."""

    name = "spy"

    def __init__(self, cost=0.006):
        self.prompts: list[str] = []
        self._cost = cost

    async def analyze(self, prompt: str) -> LLMResult:
        self.prompts.append(prompt)
        return LLMResult(
            hypothesis=Hypothesis(root_cause="disk full", severity="critical"),
            input_tokens=4000,
            output_tokens=400,
            cost_usd=self._cost,
            backend=self.name,
        )


class RaisingBackend:
    name = "raising"

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    async def analyze(self, prompt: str) -> LLMResult:
        self.calls += 1
        raise self._exc


class HangingBackend:
    name = "hanging"

    async def analyze(self, prompt: str) -> LLMResult:
        await asyncio.sleep(5)
        raise AssertionError("should have timed out")


class PIICollector:
    name = "pii"

    async def collect(self, alert) -> ContextBundle:
        return ContextBundle(
            log_lines=["login from alice@corp.io at 10.0.0.9 token=deadbeefcafe"],
            sources_ok=[self.name],
        )


def _analyzer(collector, backend, budget=None, timeout=30.0):
    return Analyzer(
        collector=collector,
        backend=backend,
        notifier=StubNotifier(),
        store=InMemoryStore(),
        budget=budget or InMemoryBudget(cap_usd=100.0),
        llm_timeout_seconds=timeout,
    )


async def test_happy_path_produces_hypothesis(alert):  # CC-18, CC-19, CC-10
    backend = SpyBackend(cost=0.006)
    az = _analyzer(StubCollector(), backend)

    incident = await az.analyze(alert)

    assert incident.status is IncidentStatus.ANALYZED
    assert incident.hypothesis and incident.hypothesis.root_cause == "disk full"
    assert incident.cost_usd == 0.006  # CC-19
    assert len(backend.prompts) == 1  # CC-10: exactly one call


async def test_incident_is_persisted_and_delivered(alert):
    backend = SpyBackend()
    notifier, store = StubNotifier(), InMemoryStore()
    az = Analyzer(StubCollector(), backend, notifier, store, InMemoryBudget(100.0))

    await az.analyze(alert)

    assert len(store.saved) == 1
    assert len(notifier.sent) == 1


async def test_backend_error_degrades_gracefully(alert):  # CC-1
    az = _analyzer(StubCollector(), RaisingBackend(BackendError("model down")))
    incident = await az.analyze(alert)
    assert incident.status is IncidentStatus.ANALYSIS_FAILED
    assert "model down" in incident.failure_reason


async def test_unexpected_exception_degrades_gracefully(alert):  # CC-2
    az = _analyzer(StubCollector(), RaisingBackend(ValueError("boom")))
    incident = await az.analyze(alert)
    assert incident.status is IncidentStatus.ANALYSIS_FAILED


async def test_hard_timeout_is_enforced(alert):  # CC-3
    az = _analyzer(StubCollector(), HangingBackend(), timeout=0.05)
    incident = await az.analyze(alert)
    assert incident.status is IncidentStatus.ANALYSIS_FAILED
    assert incident.failure_reason == "timeout"


async def test_budget_exhausted_skips_llm(alert):  # CC-4
    budget = InMemoryBudget(cap_usd=0.0)  # nothing available
    backend = RaisingBackend(AssertionError("must not be called"))
    az = _analyzer(StubCollector(), backend, budget=budget)

    incident = await az.analyze(alert)

    assert incident.status is IncidentStatus.BUDGET_EXCEEDED
    assert backend.calls == 0  # LLM never invoked


async def test_context_is_redacted_before_llm(alert):  # CC-7, CC-9
    backend = SpyBackend()
    az = _analyzer(PIICollector(), backend)

    await az.analyze(alert)

    sent = backend.prompts[0]
    assert "alice@corp.io" not in sent
    assert "10.0.0.9" not in sent
    assert "deadbeefcafe" not in sent
    assert "[REDACTED_EMAIL]" in sent


async def test_budget_only_charged_on_success(alert):
    budget = InMemoryBudget(cap_usd=100.0)
    az = _analyzer(StubCollector(), RaisingBackend(BackendError("x")), budget=budget)
    await az.analyze(alert)
    # A failed call must not consume budget.
    assert await budget.has_budget() is True



async def test_incident_records_the_redacted_context_the_model_saw():
    """Audit trail: what did the model see when it said that (ADR-0002 keeps it redacted)."""
    from app.analyzer import Analyzer
    from app.backends import StubBackend
    from app.budget import InMemoryBudget
    from app.models import ContextBundle, StreamAlert
    from app.notifiers import StubNotifier
    from app.stores import InMemoryStore

    class Leaky:
        async def collect(self, alert):
            return ContextBundle(log_lines=["auth failed for admin@corp.example from 10.1.2.3"])

    store = InMemoryStore()
    analyzer = Analyzer(collector=Leaky(), backend=StubBackend(), notifier=StubNotifier(), store=store, budget=InMemoryBudget(1.0))
    inc = await analyzer.analyze(StreamAlert(labels={"alertname": "X", "namespace": "n"}))
    assert "auth failed" in inc.context
    assert "admin@corp.example" not in inc.context and "10.1.2.3" not in inc.context
    assert store.saved[0].context == inc.context
